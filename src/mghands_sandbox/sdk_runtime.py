import asyncio
import importlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

from mghands_sandbox.models import (
    ConversationInfo,
    ConversationRuntimeState,
    ConversationStatus,
    EventRecord,
    LLMConfig,
    MCPInjection,
    MessageRequest,
    SkillInjection,
    StartConversationRequest,
    SandboxRuntimeInfo,
    RuntimeStatus,
    utc_now,
)


class SDKUnavailableError(RuntimeError):
    pass


class SDKBuildError(RuntimeError):
    pass


class SDKRunError(RuntimeError):
    pass


@dataclass
class RuntimeConversation:
    info: ConversationInfo
    llm: LLMConfig | None = None
    skills: list[SkillInjection] = field(default_factory=list)
    mcp_config: MCPInjection | None = None
    sdk_conversation: Any = None
    events: list[EventRecord] = field(default_factory=list)


class SDKRuntime:
    """Container-side OpenHands SDK runtime adapter.

    The adapter imports OpenHands SDK lazily so the gateway project remains
    testable without installing the heavy SDK locally. In the sandbox image,
    fixed OpenHands dependencies are installed and this class uses them to
    create/run conversations.
    """

    def __init__(self):
        self._conversations: dict[str, RuntimeConversation] = {}
        self._lock = asyncio.Lock()
        self._status = RuntimeStatus.READY
        self._last_error: str | None = None

    def sdk_available(self) -> tuple[bool, str | None]:
        try:
            self._ensure_sdk_available()
            return True, None
        except SDKUnavailableError as exc:
            return False, str(exc)

    def runtime_info(
        self, *, workspace_dir: str = '/workspace', session_auth_enabled: bool = False
    ) -> SandboxRuntimeInfo:
        sdk_available, sdk_error = self.sdk_available()
        status = self._status
        if status != RuntimeStatus.SHUTTING_DOWN:
            if any(c.info.status == ConversationStatus.RUNNING for c in self._conversations.values()):
                status = RuntimeStatus.BUSY
            elif self._last_error:
                status = RuntimeStatus.ERROR
            else:
                status = RuntimeStatus.READY
        return SandboxRuntimeInfo(
            status=status,
            sdk_available=sdk_available,
            sdk_error=sdk_error or self._last_error,
            workspace_dir=workspace_dir,
            conversation_count=len(self._conversations),
            active_conversation_ids=list(self._conversations),
            session_auth_enabled=session_auth_enabled,
            default_coding_tools_enabled=True,
            browser_tools_enabled=_browser_tools_enabled(),
        )

    def mark_shutting_down(self) -> None:
        self._status = RuntimeStatus.SHUTTING_DOWN

    async def create_conversation(
        self, request: StartConversationRequest
    ) -> ConversationInfo:
        self._ensure_sdk_available()
        self._last_error = None
        conversation_id = request.conversation_id or ConversationInfo().id
        info = ConversationInfo(id=conversation_id, working_dir=request.working_dir)
        runtime = RuntimeConversation(
            info=info,
            llm=request.llm,
            skills=list(request.skills),
            mcp_config=request.mcp_config,
        )
        try:
            runtime.sdk_conversation = await asyncio.to_thread(
                self._build_sdk_conversation, request, runtime
            )
        except Exception as exc:
            self._last_error = str(exc)
            raise SDKBuildError(str(exc)) from exc
        async with self._lock:
            self._conversations[conversation_id] = runtime
        await self._append_event(
            conversation_id,
            'conversation.created',
            {
                'conversation_id': conversation_id,
                'skills': [skill.model_dump() for skill in runtime.skills],
                'mcp_config': _redact(runtime.mcp_config.model_dump())
                if runtime.mcp_config
                else None,
            },
        )
        if request.initial_message:
            await self.send_message(conversation_id, request.initial_message)
        return info

    async def get_conversation(self, conversation_id: str) -> ConversationInfo | None:
        runtime = self._conversations.get(conversation_id)
        return runtime.info if runtime else None

    async def delete_conversation(self, conversation_id: str) -> bool:
        async with self._lock:
            runtime = self._conversations.pop(conversation_id, None)
        if runtime is None:
            return False
        runtime.info.status = ConversationStatus.DELETED
        await self._append_event(conversation_id, 'conversation.deleted', {})
        return True

    async def send_message(
        self, conversation_id: str, message: MessageRequest
    ) -> ConversationInfo:
        runtime = self._require(conversation_id)
        await self._append_event(
            conversation_id,
            'message',
            message.model_dump(mode='json'),
        )
        if message.run:
            runtime.info.status = ConversationStatus.RUNNING
            runtime.info.updated_at = utc_now()
            
            async def _run_bg() -> None:
                try:
                    result = await asyncio.to_thread(
                        self._run_sdk_conversation, runtime, message
                    )
                    runtime.info.status = ConversationStatus.COMPLETED
                    runtime.info.updated_at = utc_now()
                    await self._append_event(
                        conversation_id,
                        'agent.result',
                        {'result': _jsonable(result)},
                    )
                except Exception as exc:
                    self._last_error = str(exc)
                    runtime.info.status = ConversationStatus.ERROR
                    runtime.info.error = str(exc)
                    runtime.info.updated_at = utc_now()
                    await self._append_event(
                        conversation_id,
                        'agent.error',
                        {'error': str(exc)},
                    )
            
            asyncio.create_task(_run_bg())
        return runtime.info

    async def get_runtime_state(self, conversation_id: str) -> ConversationRuntimeState:
        runtime = self._require(conversation_id)
        return ConversationRuntimeState(
            conversation=runtime.info,
            llm_model=runtime.llm.model if runtime.llm else None,
            skills=runtime.skills,
            mcp_config=runtime.mcp_config,
            event_count=len(runtime.events),
        )

    async def update_runtime(
        self,
        conversation_id: str,
        *,
        skills: list[SkillInjection] | None,
        mcp_config: MCPInjection | None,
    ) -> ConversationInfo:
        runtime = self._require(conversation_id)
        if skills is not None:
            runtime.skills = skills
        if mcp_config is not None:
            runtime.mcp_config = mcp_config
        runtime.sdk_conversation = await asyncio.to_thread(
            self._rebuild_sdk_conversation, runtime
        )
        runtime.info.updated_at = utc_now()
        await self._append_event(
            conversation_id,
            'runtime.updated',
            {
                'skills': [skill.model_dump() for skill in runtime.skills],
                'mcp_config': _redact(runtime.mcp_config.model_dump())
                if runtime.mcp_config
                else None,
            },
        )
        return runtime.info

    async def search_events(
        self, conversation_id: str, page_id: str | None, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        runtime = self._require(conversation_id)
        offset = int(page_id) if page_id else 0
        events = runtime.events[offset : offset + limit]
        next_page_id = None
        if offset + limit < len(runtime.events):
            next_page_id = str(offset + limit)
        return [event.model_dump(mode='json') for event in events], next_page_id

    async def list_skills(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        if conversation_id:
            runtime = self._require(conversation_id)
            return [skill.model_dump() for skill in runtime.skills]
        skills: list[dict[str, Any]] = []
        for runtime in self._conversations.values():
            skills.extend(skill.model_dump() for skill in runtime.skills)
        return skills

    def _require(self, conversation_id: str) -> RuntimeConversation:
        runtime = self._conversations.get(conversation_id)
        if runtime is None:
            raise KeyError(conversation_id)
        return runtime

    async def _append_event(
        self, conversation_id: str, kind: str, data: dict[str, Any]
    ) -> None:
        runtime = self._conversations.get(conversation_id)
        if runtime is None:
            return
        runtime.events.append(EventRecord(kind=kind, data=_redact(data)))

    def _ensure_sdk_available(self) -> None:
        try:
            spec = importlib.util.find_spec('openhands.sdk')
        except ModuleNotFoundError:
            spec = None
        if spec is None:
            raise SDKUnavailableError(
                'openhands-sdk is not installed in this container. Build the sandbox image first.'
            )

    def _build_sdk_conversation(
        self, request: StartConversationRequest, runtime: RuntimeConversation
    ) -> Any:
        return _OfficialSDKAdapter().build(request, runtime)

    def _rebuild_sdk_conversation(self, runtime: RuntimeConversation) -> Any:
        return _OfficialSDKAdapter().rebuild(runtime)

    def _run_sdk_conversation(
        self, runtime: RuntimeConversation, message: MessageRequest
    ) -> Any:
        return _OfficialSDKAdapter().run(runtime, message)


class _OfficialSDKAdapter:
    """Small compatibility layer around the official OpenHands SDK.

    SDK constructors have changed across releases, so this adapter uses a
    conservative reflection-based path and keeps all version-specific code in
    one place.
    """

    def build(self, request: StartConversationRequest, runtime: RuntimeConversation) -> Any:
        official = self._build_official_conversation(request, runtime)
        if official is not None:
            return official
        sdk = importlib.import_module('openhands.sdk')
        conversation_cls = getattr(sdk, 'Conversation', None)
        if conversation_cls is None:
            conversation_mod = importlib.import_module('openhands.sdk.conversation')
            conversation_cls = getattr(conversation_mod, 'Conversation')
        agent = self._build_default_agent(request, runtime)
        return self._instantiate_conversation(conversation_cls, agent)

    def _build_official_conversation(
        self, request: StartConversationRequest, runtime: RuntimeConversation
    ) -> Any | None:
        """Build with OpenHands' default coding agent path when available.

        This mirrors the app-server construction style: default tools are loaded
        through openhands-tools, skills/secrets live on AgentContext, MCP config is
        attached to OpenHandsAgentSettings, and ConversationSettings creates the
        SDK start request. If an SDK version lacks any piece, the caller falls
        back to the narrower Conversation constructor.
        """
        try:
            sdk = importlib.import_module('openhands.sdk')
            settings_mod = importlib.import_module('openhands.sdk.settings')
            tools_mod = self._import_first(
                'openhands.tools',
                'openhands.sdk.tools',
            )
            subagent_mod = self._import_first(
                'openhands.sdk.subagent',
                required=False,
            )
            conversation_cls = getattr(sdk, 'Conversation', None)
            if conversation_cls is None:
                conversation_cls = getattr(
                    importlib.import_module('openhands.sdk.conversation'),
                    'Conversation',
                )
            agent_context_cls = getattr(sdk, 'AgentContext')
            agent_settings_cls = getattr(settings_mod, 'OpenHandsAgentSettings')
            conversation_settings_cls = getattr(settings_mod, 'ConversationSettings')

            tools = self._build_default_tools()

            agent_definitions = []
            if subagent_mod is not None:
                get_defs = getattr(subagent_mod, 'get_registered_agent_definitions', None)
                if get_defs is not None:
                    agent_definitions = list(get_defs())

            agent_context = agent_context_cls(skills=self._build_skills(runtime.skills))
            llm = self._build_llm(request.llm)
            workspace = self._build_workspace(request.working_dir)
            mcp_config = self._build_mcp_config(runtime.mcp_config)
            callbacks = [self._build_event_callback(runtime)]

            agent_kwargs: dict[str, Any] = {
                'tools': tools,
                'agent_context': agent_context,
                'enable_sub_agents': True,
            }
            if llm is not None:
                agent_kwargs['llm'] = llm
            if mcp_config is not None:
                agent_kwargs['mcp_config'] = mcp_config
            agent_settings = agent_settings_cls(**agent_kwargs)
            agent = agent_settings.create_agent()

            direct_kwargs: dict[str, Any] = {
                'conversation_id': request.conversation_id or runtime.info.id,
                'callbacks': callbacks,
            }
            if workspace is not None:
                direct_kwargs['workspace'] = workspace

            conv_kwargs: dict[str, Any] = {
                'agent_settings': agent_settings,
                'conversation_id': request.conversation_id or runtime.info.id,
                'agent_definitions': agent_definitions,
            }
            if workspace is not None:
                conv_kwargs['workspace'] = workspace
            conversation_settings = conversation_settings_cls(**conv_kwargs)

            start_request_cls = getattr(
                importlib.import_module('openhands.agent_server.models'),
                'StartConversationRequest',
                None,
            )
            if start_request_cls is not None and hasattr(conversation_settings, 'create_request'):
                start_request = conversation_settings.create_request(
                    start_request_cls, agent=agent
                )
                return self._instantiate_conversation(
                    conversation_cls,
                    agent,
                    direct_kwargs=direct_kwargs,
                    conversation_settings=conversation_settings,
                    start_request=start_request,
                )
            return self._instantiate_conversation(
                conversation_cls,
                agent,
                direct_kwargs=direct_kwargs,
                conversation_settings=conversation_settings,
            )
        except Exception:
            return None

    def _instantiate_conversation(
        self,
        conversation_cls: Any,
        agent: Any,
        *,
        direct_kwargs: dict[str, Any] | None = None,
        conversation_settings: Any = None,
        start_request: Any = None,
    ) -> Any:
        if direct_kwargs is not None:
            attempts = [lambda: conversation_cls(agent=agent, **direct_kwargs)]
        else:
            attempts = []
        if start_request is not None:
            attempts.extend(
                [
                    lambda: conversation_cls(agent=agent, start_request=start_request),
                    lambda: conversation_cls(agent, start_request),
                ]
            )
        if conversation_settings is not None:
            attempts.extend(
                [
                    lambda: conversation_cls(agent=agent, settings=conversation_settings),
                    lambda: conversation_cls(agent, conversation_settings),
                ]
            )
        attempts.extend(
            [
                lambda: conversation_cls(agent=agent),
                lambda: conversation_cls(agent),
            ]
        )
        last_error: Exception | None = None
        for attempt in attempts:
            try:
                return attempt()
            except TypeError as exc:
                last_error = exc
        raise SDKBuildError(f'Failed to instantiate SDK Conversation: {last_error}')

    def _build_default_agent(
        self, request: StartConversationRequest, runtime: RuntimeConversation
    ) -> Any:
        sdk = importlib.import_module('openhands.sdk')
        settings_mod = importlib.import_module('openhands.sdk.settings')
        agent_context_cls = getattr(sdk, 'AgentContext')
        agent_settings_cls = getattr(settings_mod, 'OpenHandsAgentSettings')
        agent_context = agent_context_cls(skills=self._build_skills(runtime.skills))
        agent_kwargs: dict[str, Any] = {
            'tools': self._build_default_tools(),
            'agent_context': agent_context,
            'enable_sub_agents': True,
        }
        llm = self._build_llm(request.llm)
        if llm is not None:
            agent_kwargs['llm'] = llm
        mcp_config = self._build_mcp_config(runtime.mcp_config)
        if mcp_config is not None:
            agent_kwargs['mcp_config'] = mcp_config
        return agent_settings_cls(**agent_kwargs).create_agent()

    def _build_event_callback(self, runtime: RuntimeConversation):
        def on_event(event: Any) -> None:
            runtime.events.append(
                EventRecord(
                    kind=f'openhands.{event.__class__.__name__}',
                    data=_redact(_sdk_event_payload(event)),
                )
            )

        return on_event

    def _build_default_tools(self) -> Any:
        tools_mod = self._import_first('openhands.tools', 'openhands.sdk.tools')
        register_builtins = getattr(tools_mod, 'register_builtins_agents', None)
        enable_browser = _browser_tools_enabled()
        if register_builtins is not None:
            register_builtins(enable_browser=enable_browser)
        get_default_tools = getattr(tools_mod, 'get_default_tools')
        return get_default_tools(enable_browser=enable_browser, enable_sub_agents=True)

    def rebuild(self, runtime: RuntimeConversation) -> Any:
        request = StartConversationRequest(
            conversation_id=runtime.info.id,
            llm=runtime.llm,
            skills=runtime.skills,
            mcp_config=runtime.mcp_config,
            working_dir=runtime.info.working_dir,
        )
        return self.build(request, runtime)

    def run(self, runtime: RuntimeConversation, message: MessageRequest) -> Any:
        conversation = runtime.sdk_conversation
        if conversation is None:
            raise RuntimeError('SDK conversation is not initialized')
        
        # Reset terminal status in OpenHands SDK state if present to allow reuse
        state = getattr(conversation, 'state', None)
        if state is not None and hasattr(state, 'status'):
            status_attr = getattr(state, 'status')
            if status_attr is not None:
                status_class = status_attr.__class__
                if hasattr(status_class, '__members__'):
                    members = status_class.__members__
                    non_terminal = None
                    for name, member in members.items():
                        name_upper = name.upper()
                        if not any(t in name_upper for t in ('STOP', 'FINISH', 'ERR', 'COMPLET')):
                            non_terminal = member
                            break
                    idle_val = non_terminal if non_terminal is not None else list(members.values())[0]
                else:
                    idle_val = getattr(status_class, 'IDLE', None) or getattr(status_class, 'idle', None) or getattr(status_class, 'RUNNING', None) or 'idle'
                
                status_str = str(status_attr).upper()
                is_terminal = any(t in status_str for t in ('STOP', 'FINISH', 'ERR', 'COMPLET'))
                if is_terminal:
                    try:
                        state.status = idle_val
                    except Exception as exc:
                        print(f"WARNING: failed to reset OpenHands conversation status: {exc}", flush=True)

        run = getattr(conversation, 'run', None)
        if run is None:
            raise RuntimeError('SDK conversation does not expose run()')
        prompt = '\n'.join(item.text for item in message.content)
        send_message = getattr(conversation, 'send_message', None)
        if send_message is not None:
            send_message(prompt)
            return run()
        try:
            return run(prompt)
        except TypeError:
            return run()

    def _conversation_kwargs(
        self, request: StartConversationRequest, runtime: RuntimeConversation
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        llm = self._build_llm(request.llm)
        if llm is not None:
            kwargs['llm'] = llm
        workspace = self._build_workspace(request.working_dir)
        if workspace is not None:
            kwargs['workspace'] = workspace
        skills = self._build_skills(runtime.skills)
        if skills:
            kwargs['skills'] = skills
        mcp_config = self._build_mcp_config(runtime.mcp_config)
        if mcp_config is not None:
            kwargs['mcp_config'] = mcp_config
        return kwargs

    def _build_llm(self, llm: LLMConfig | None) -> Any:
        if llm is None:
            return None
        llm_mod = importlib.import_module('openhands.sdk.llm')
        llm_cls = getattr(llm_mod, 'LLM')
        payload = llm.model_dump(exclude_none=True, context={'expose_secrets': True})
        provider = payload.pop('provider', None)
        model = payload.get('model')
        if model and '/' not in model and payload.get('base_url'):
            payload['model'] = f'{provider or "openai"}/{model}'
        return llm_cls(**payload)

    def _build_workspace(self, working_dir: str) -> Any:
        candidates = [
            'openhands.sdk.workspace.local',
            'openhands.sdk.workspace',
        ]
        for module_name in candidates:
            try:
                module = importlib.import_module(module_name)
            except ModuleNotFoundError:
                continue
            workspace_cls = getattr(module, 'LocalWorkspace', None)
            if workspace_cls is not None:
                return workspace_cls(working_dir=working_dir)
        return None

    def _build_sdk_message(self, message: MessageRequest | None) -> Any:
        if message is None:
            return None
        try:
            models = importlib.import_module('openhands.agent_server.models')
            send_cls = getattr(models, 'SendMessageRequest')
            text_cls = getattr(models, 'TextContent')
            return send_cls(
                role=message.role,
                content=[text_cls(text=item.text) for item in message.content],
                run=message.run,
            )
        except Exception:
            return message.model_dump(mode='json')

    def _import_first(self, *module_names: str, required: bool = True) -> Any:
        for module_name in module_names:
            try:
                return importlib.import_module(module_name)
            except ModuleNotFoundError:
                continue
        if required:
            raise ModuleNotFoundError(module_names[0])
        return None

    def _build_skills(self, skills: list[SkillInjection]) -> list[Any]:
        if not skills:
            return []
        try:
            skills_mod = importlib.import_module('openhands.sdk.skills')
        except ModuleNotFoundError:
            return [skill.model_dump() for skill in skills]
        skill_cls = getattr(skills_mod, 'Skill', None)
        if skill_cls is None:
            return [skill.model_dump() for skill in skills]
        result = []
        for skill in skills:
            try:
                result.append(skill_cls(**skill.model_dump()))
            except TypeError:
                result.append(skill.model_dump())
        return result

    def _build_mcp_config(self, mcp_config: MCPInjection | None) -> Any:
        if mcp_config is None:
            return None
        try:
            fastmcp = importlib.import_module('fastmcp.mcp_config')
            mcp_cls = getattr(fastmcp, 'MCPConfig')
            return mcp_cls(**mcp_config.model_dump())
        except Exception:
            return mcp_config.model_dump()


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
        return value
    except TypeError:
        return str(value)


def _sdk_event_payload(event: Any) -> dict[str, Any]:
    event_type = event.__class__.__name__
    if hasattr(event, 'model_dump'):
        raw = event.model_dump(mode='json')
    else:
        raw = str(event)
    payload = {
        'event_type': event_type,
        'source': getattr(event, 'source', None),
        'sdk_event_id': getattr(event, 'id', None),
        'sdk_timestamp': getattr(event, 'timestamp', None),
        'raw': raw,
    }
    try:
        visualize = getattr(event, 'visualize', None)
        if visualize is not None:
            payload['preview'] = getattr(visualize, 'plain', str(visualize))
    except Exception:
        pass
    return payload


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lower = str(key).lower()
            if any(token in lower for token in ('api_key', 'authorization', 'token', 'secret')):
                result[key] = '**********'
            else:
                result[key] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _browser_tools_enabled() -> bool:
    return os.getenv('MGHANDS_ENABLE_BROWSER_TOOLS', '').lower() in {'1', 'true', 'yes'}
