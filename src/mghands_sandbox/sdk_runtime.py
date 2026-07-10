import asyncio
import hashlib
import importlib
import json
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
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


def _create_symlink(link_path_str: str, target_path_str: str) -> None:
    try:
        link_path = Path(link_path_str)
        if link_path.exists() or link_path.is_symlink():
            if link_path.is_symlink() or link_path.is_file():
                link_path.unlink()
            else:
                import shutil
                shutil.rmtree(link_path)
        
        Path(target_path_str).mkdir(parents=True, exist_ok=True)
        os.symlink(target_path_str, link_path_str)
        print(f"Created symlink {link_path_str} -> {target_path_str}")
    except Exception as e:
        print(f"Failed to create symlink {link_path_str}: {e}")


class SDKBuildError(RuntimeError):
    pass


class SDKRunError(RuntimeError):
    pass


class ConversationBusyError(RuntimeError):
    pass


class ConversationConflictError(RuntimeError):
    pass


@dataclass
class RuntimeConversation:
    info: ConversationInfo
    llm: LLMConfig | None = None
    skills: list[SkillInjection] = field(default_factory=list)
    persistence_dir: str | None = None
    mcp_config: MCPInjection | None = None
    sdk_conversation: Any = None
    events: list[EventRecord] = field(default_factory=list)
    event_ids: set[str] = field(default_factory=set)
    execution_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    execution_task: asyncio.Task[None] | None = None
    deleted: bool = False


class SDKRuntime:
    """Container-side OpenHands SDK runtime adapter.

    The adapter imports OpenHands SDK lazily so the gateway project remains
    testable without installing the heavy SDK locally. In the sandbox image,
    fixed OpenHands dependencies are installed and this class uses them to
    create/run conversations.
    """

    def __init__(
        self,
        *,
        userspace_root: str | None = None,
        persistence_dir: str | None = None,
        max_concurrent_runs: int = 1,
    ):
        target_userspace = os.getenv('MGHANDS_SANDBOX_SHARED_VOLUME_USERSPACE')
        if target_userspace:
            _create_symlink('/userspace', target_userspace)

        target_workspace = os.getenv('MGHANDS_SANDBOX_SHARED_VOLUME_WORKSPACE')
        if target_workspace:
            _create_symlink('/workspace', target_workspace)

        self._conversations: dict[str, RuntimeConversation] = {}
        self._lock = asyncio.Lock()
        configured_root = userspace_root or os.getenv(
            'MGHANDS_SANDBOX_USERSPACE_ROOT', '/userspace'
        )
        self._userspace_root = Path(configured_root).expanduser().resolve()
        configured_persistence = persistence_dir or os.getenv(
            'MGHANDS_PERSISTENCE_DIR',
            str(self._userspace_root / '.mghands' / 'conversations'),
        )
        self._persistence_dir = self._contained_path(configured_persistence, 'persistence_dir')
        self._run_semaphore = asyncio.Semaphore(max_concurrent_runs)
        self._tasks: dict[str, asyncio.Task[None]] = {}

        self._status = RuntimeStatus.READY
        self._last_error: str | None = None

    def sdk_available(self) -> tuple[bool, str | None]:
        try:
            self._ensure_sdk_available()
            return True, None
        except SDKUnavailableError as exc:
            return False, str(exc)

    def runtime_info(
        self, *, workspace_dir: str | None = None, session_auth_enabled: bool = False
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
            workspace_dir=workspace_dir or str(self._userspace_root),
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
        working_dir = self._contained_path(request.working_dir or str(self._userspace_root), 'working_dir')
        persistence_dir = self._contained_path(
            request.persistence_dir or str(self._persistence_dir), 'persistence_dir'
        )
        normalized_request = request.model_copy(
            update={
                'conversation_id': conversation_id,
                'working_dir': str(working_dir),
                'persistence_dir': str(persistence_dir),
            }
        )
        async with self._lock:
            if conversation_id in self._conversations:
                raise ConversationConflictError(
                    f'conversation {conversation_id} already exists'
                )
        persistence_dir.mkdir(parents=True, exist_ok=True)
        info = ConversationInfo(id=conversation_id, working_dir=str(working_dir))
        runtime = RuntimeConversation(
            info=info,
            llm=request.llm,
            skills=list(request.skills),
            mcp_config=request.mcp_config,
            persistence_dir=str(persistence_dir),
        )
        try:
            runtime.sdk_conversation = await asyncio.to_thread(
                self._build_sdk_conversation, normalized_request, runtime
            )
        except Exception as exc:
            self._last_error = str(exc)
            raise SDKBuildError(str(exc)) from exc
        if request.restore:
            has_unmatched, unmatched_action = _find_unmatched_action(runtime.events)
            if has_unmatched and unmatched_action:
                if not _is_action_read_only(unmatched_action):
                    runtime.info.status = ConversationStatus.INTERRUPTED
                    runtime.events.append(
                        EventRecord(
                            kind='conversation.interrupted',
                            data=_redact({
                                'detail': 'unmatched side-effect action',
                                'action': unmatched_action.model_dump(mode='json') if hasattr(unmatched_action, 'model_dump') else unmatched_action,
                            }),
                        )
                    )
        async with self._lock:
            if conversation_id in self._conversations:
                raise ConversationConflictError(
                    f'conversation {conversation_id} already exists'
                )
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
        if request.initial_message and runtime.info.status != ConversationStatus.INTERRUPTED:
            await self.send_message(conversation_id, request.initial_message)
        return info

    async def get_conversation(self, conversation_id: str) -> ConversationInfo | None:
        runtime = self._conversations.get(conversation_id)
        return runtime.info if runtime else None

    async def delete_conversation(self, conversation_id: str) -> bool:
        async with self._lock:
            runtime = self._conversations.get(conversation_id)
        if runtime is None:
            return False
        runtime.deleted = True
        with suppress(Exception):
            await asyncio.to_thread(self._stop_sdk_conversation, runtime)
        task = runtime.execution_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        runtime.info.status = ConversationStatus.DELETED
        runtime.info.updated_at = utc_now()
        async with self._lock:
            self._tasks.pop(conversation_id, None)
            self._conversations.pop(conversation_id, None)
        return True

    async def send_message(
        self, conversation_id: str, message: MessageRequest
    ) -> ConversationInfo:
        runtime = self._require(conversation_id)
        if not message.run:
            await self._append_event(
                conversation_id,
                'message',
                message.model_dump(mode='json'),
            )
            return runtime.info

        async with runtime.execution_lock:
            if runtime.execution_task is not None and not runtime.execution_task.done():
                raise ConversationBusyError(
                    f'conversation {conversation_id} is already running'
                )
            await self._append_event(
                conversation_id,
                'message',
                message.model_dump(mode='json'),
            )
            runtime.info.status = ConversationStatus.RUNNING
            runtime.info.error = None
            runtime.info.updated_at = utc_now()

            async def _run_bg() -> None:
                try:
                    async with self._run_semaphore:
                        result = await asyncio.to_thread(
                            self._run_sdk_conversation, runtime, message
                        )
                    if runtime.deleted:
                        return
                    runtime.info.status = ConversationStatus.COMPLETED
                    runtime.info.updated_at = utc_now()
                    await self._append_event(
                        conversation_id,
                        'agent.result',
                        {'result': _jsonable(result)},
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if runtime.deleted:
                        return
                    self._last_error = str(exc)
                    runtime.info.status = ConversationStatus.ERROR
                    runtime.info.error = str(exc)
                    runtime.info.updated_at = utc_now()
                    await self._append_event(
                        conversation_id,
                        'agent.error',
                        {'error': str(exc)},
                    )
                finally:
                    if self._tasks.get(conversation_id) is asyncio.current_task():
                        self._tasks.pop(conversation_id, None)

            task = asyncio.create_task(_run_bg())
            runtime.execution_task = task
            self._tasks[conversation_id] = task
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
        if runtime.execution_task is not None and not runtime.execution_task.done():
            raise ConversationBusyError(f'conversation {conversation_id} is already running')
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

    def _contained_path(self, value: str, name: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self._userspace_root / path
        resolved = path.resolve()
        try:
            resolved.relative_to(self._userspace_root)
        except ValueError as exc:
            raise ValueError(f'{name} must be within {self._userspace_root}') from exc
        return resolved

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

    def _stop_sdk_conversation(self, runtime: RuntimeConversation) -> None:
        conversation = runtime.sdk_conversation
        if conversation is None:
            return
        for method_name in ('pause', 'stop'):
            method = getattr(conversation, method_name, None)
            if callable(method):
                method()
                return



class _OfficialSDKAdapter:
    """Small compatibility layer around the official OpenHands SDK.

    SDK constructors have changed across releases, so this adapter uses a
    conservative reflection-based path and keeps all version-specific code in
    one place.
    """

    def restore_events(self, conversation: Any, runtime: RuntimeConversation) -> None:
        state = getattr(conversation, 'state', None)
        events = getattr(state, 'events', None)
        if events is None:
            return
        callback = self._build_event_callback(runtime)
        try:
            for event in events:
                callback(event)
        except (AttributeError, TypeError):
            return

    def build(self, request: StartConversationRequest, runtime: RuntimeConversation) -> Any:
        import uuid
        conv_id_str = request.conversation_id or runtime.info.id
        try:
            raw_hex = conv_id_str.split('_')[-1].replace('-', '')
            conv_id_uuid = uuid.UUID(raw_hex)
        except ValueError:
            conv_id_uuid = conv_id_str

        official = self._build_official_conversation(request, runtime)
        if official is not None:
            if request.restore:
                self.restore_events(official, runtime)
            return official
        sdk = importlib.import_module('openhands.sdk')
        conversation_cls = getattr(sdk, 'Conversation', None)
        if conversation_cls is None:
            conversation_mod = importlib.import_module('openhands.sdk.conversation')
            conversation_cls = getattr(conversation_mod, 'Conversation')
        agent = self._build_default_agent(request, runtime)
        direct_kwargs = {
            'conversation_id': conv_id_uuid,
            'callbacks': [self._build_event_callback(runtime)],
        }
        if request.persistence_dir:
            direct_kwargs['persistence_dir'] = request.persistence_dir
        conversation = self._instantiate_conversation(
            conversation_cls, agent, direct_kwargs=direct_kwargs
        )
        if request.restore:
            self.restore_events(conversation, runtime)
        return conversation

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
            import uuid
            conv_id_str = request.conversation_id or runtime.info.id
            try:
                raw_hex = conv_id_str.split('_')[-1].replace('-', '')
                conv_id_uuid = uuid.UUID(raw_hex)
            except ValueError:
                conv_id_uuid = conv_id_str

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
                'conversation_id': conv_id_uuid,
                'callbacks': callbacks,
            }
            if request.persistence_dir:
                direct_kwargs['persistence_dir'] = request.persistence_dir
            if workspace is not None:
                direct_kwargs['workspace'] = workspace

            conv_kwargs: dict[str, Any] = {
                'agent_settings': agent_settings,
                'conversation_id': conv_id_uuid,
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
            if runtime.deleted:
                return
            payload = _sdk_event_payload(event)
            event_id = _stable_sdk_event_id(payload)
            if event_id in runtime.event_ids:
                return
            record = EventRecord(
                id=event_id,
                kind=f'openhands.{event.__class__.__name__}',
                data=_redact(payload),
            )
            runtime.events.append(record)
            runtime.event_ids.add(record.id)

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
            persistence_dir=runtime.persistence_dir,
            restore=True,
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
        prompt = '\n'.join(item.text for item in message.content).strip()
        if not prompt:
            return run()
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
    action = getattr(event, 'action', None)
    if action is not None:
        payload['action'] = action
    observation = getattr(event, 'observation', None)
    if observation is not None:
        payload['observation'] = observation
    cause = getattr(event, 'cause', None)
    if cause is not None:
        payload['cause'] = cause
    try:
        visualize = getattr(event, 'visualize', None)
        if visualize is not None:
            payload['preview'] = getattr(visualize, 'plain', str(visualize))
    except Exception:
        pass
    return payload


def _stable_sdk_event_id(payload: dict[str, Any]) -> str:
    sdk_event_id = payload.get('sdk_event_id')
    if sdk_event_id is not None:
        return str(sdk_event_id)
    serialized = json.dumps(payload, sort_keys=True, default=str, separators=(',', ':'))
    return f'sdk-{hashlib.sha256(serialized.encode()).hexdigest()[:32]}'


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


def _find_unmatched_action(events: list[EventRecord]) -> tuple[bool, EventRecord | None]:
    last_action = None
    last_action_idx = -1
    last_observation_idx = -1
    for idx, event in enumerate(events):
        if 'Action' in event.kind:
            last_action = event
            last_action_idx = idx
        elif 'Observation' in event.kind:
            last_observation_idx = idx
    if last_action_idx > last_observation_idx:
        return True, last_action
    return False, None


def _is_action_read_only(event: EventRecord) -> bool:
    kind = event.kind
    kind_lower = kind.lower()
    if any(keyword in kind_lower for keyword in ('read', 'list', 'search', 'view', 'info')):
        return True
    return False
