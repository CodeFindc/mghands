import asyncio
import threading
import time
from functools import wraps

import pytest
from fastapi.testclient import TestClient

from mghands_sandbox.app import app
from mghands_sandbox.models import (
    ConversationInfo,
    LLMConfig,
    MessageRequest,
    StartConversationRequest,
    EventRecord,
    TextContent,
)
from mghands_sandbox.sdk_runtime import (
    ConversationBusyError,
    ConversationConflictError,
    RuntimeConversation,
    SDKRuntime,
    _OfficialSDKAdapter,
    _sdk_event_payload,
)


def async_test(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return wrapper



def test_sandbox_alive() -> None:
    client = TestClient(app)
    response = client.get('/alive')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


def test_sandbox_ready_and_server_info() -> None:
    client = TestClient(app)

    ready = client.get('/ready')
    assert ready.status_code == 200
    assert ready.json()['status'] == 'ready'
    assert ready.json()['conversation_count'] >= 0

    info = client.get('/server_info')
    assert info.status_code == 200
    assert info.json()['supports_dynamic_skills'] is True
    assert info.json()['default_coding_tools_enabled'] is True
    assert info.json()['browser_tools_enabled'] is False
    assert any('get_default_tools' in item for item in info.json()['default_tool_sources'])
    assert 'POST /api/conversations/{conversation_id}/runtime' in info.json()['standard_endpoints']


def test_sandbox_runtime_info() -> None:
    client = TestClient(app)
    response = client.get('/api/runtime')
    assert response.status_code == 200
    assert response.json()['active_conversation_ids'] == []
    assert response.json()['default_coding_tools_enabled'] is True
    assert response.json()['browser_tools_enabled'] is False


def test_sandbox_start_requires_sdk_when_not_installed() -> None:
    client = TestClient(app)
    response = client.post(
        '/api/conversations',
        json={
            'initial_message': {
                'role': 'user',
                'content': [{'type': 'text', 'text': 'hello'}],
                'run': True,
            }
        },
    )
    assert response.status_code == 503
    assert 'openhands-sdk is not installed' in response.json()['detail']


def test_sandbox_runtime_payload_models_validate() -> None:
    client = TestClient(app)
    response = client.post(
        '/api/conversations/missing/runtime',
        json={
            'skills': [{'name': 'repo-skill', 'content': 'Use pytest', 'triggers': ['test']}],
            'mcp_config': {'mcpServers': {'local': {'command': 'echo'}}},
        },
    )
    assert response.status_code == 404


def test_sdk_adapter_prefixes_custom_openai_model(monkeypatch) -> None:
    captured = {}

    class FakeLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeModule:
        LLM = FakeLLM

    def fake_import_module(name):
        assert name == 'openhands.sdk.llm'
        return FakeModule

    monkeypatch.setattr('mghands_sandbox.sdk_runtime.importlib.import_module', fake_import_module)

    _OfficialSDKAdapter()._build_llm(
        LLMConfig(
            model='DeepSeek-V4-Flash-w8a8-mtp',
            base_url='http://192.168.110.209:3000/v1',
            api_key='sk-test',
        )
    )

    assert captured['model'] == 'openai/DeepSeek-V4-Flash-w8a8-mtp'
    assert captured['api_key'] == 'sk-test'


def test_sdk_adapter_prefers_start_request_constructor() -> None:
    class FakeConversation:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    conversation = _OfficialSDKAdapter()._instantiate_conversation(
        FakeConversation,
        agent='agent',
        conversation_settings='settings',
        start_request='start',
    )

    assert conversation.args == ()
    assert conversation.kwargs == {'agent': 'agent', 'start_request': 'start'}


def test_sdk_adapter_prefers_direct_constructor_kwargs() -> None:
    class FakeConversation:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    conversation = _OfficialSDKAdapter()._instantiate_conversation(
        FakeConversation,
        agent='agent',
        direct_kwargs={'workspace': '/workspace', 'callbacks': ['callback']},
        conversation_settings='settings',
        start_request='start',
    )

    assert conversation.args == ()
    assert conversation.kwargs == {
        'agent': 'agent',
        'workspace': '/workspace',
        'callbacks': ['callback'],
    }


def test_sdk_adapter_prefers_settings_constructor_before_bare() -> None:
    class FakeConversation:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    conversation = _OfficialSDKAdapter()._instantiate_conversation(
        FakeConversation,
        agent='agent',
        conversation_settings='settings',
    )

    assert conversation.args == ()
    assert conversation.kwargs == {'agent': 'agent', 'settings': 'settings'}


def test_sdk_adapter_keeps_bare_constructor_fallback() -> None:
    calls = []

    class FakeConversation:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            if (
                'callbacks' in kwargs
                or 'start_request' in kwargs
                or 'settings' in kwargs
                or len(args) == 2
            ):
                raise TypeError('unsupported constructor')
            self.args = args
            self.kwargs = kwargs

    conversation = _OfficialSDKAdapter()._instantiate_conversation(
        FakeConversation,
        agent='agent',
        direct_kwargs={'callbacks': ['callback']},
        conversation_settings='settings',
        start_request='start',
    )

    assert len(calls) == 6
    assert conversation.args == ()
    assert conversation.kwargs == {'agent': 'agent'}


def test_sdk_adapter_does_not_pass_initial_message_to_conversation_settings(monkeypatch) -> None:
    captured = {}

    class FakeConversation:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class FakeAgentContext:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeAgentSettings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def create_agent(self):
            return 'agent'

    class FakeConversationSettings:
        def __init__(self, **kwargs):
            captured['conversation_settings'] = kwargs

        def create_request(self, request_cls, agent):
            return request_cls(agent=agent)

    class FakeStartConversationRequest:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeSDKModule:
        Conversation = FakeConversation
        AgentContext = FakeAgentContext

    class FakeSettingsModule:
        OpenHandsAgentSettings = FakeAgentSettings
        ConversationSettings = FakeConversationSettings

    class FakeToolsModule:
        @staticmethod
        def register_builtins_agents(enable_browser=False):
            return None

        @staticmethod
        def get_default_tools(enable_browser=False, enable_sub_agents=True):
            return ['tool']

    class FakeAgentServerModelsModule:
        StartConversationRequest = FakeStartConversationRequest

    def fake_import_module(name):
        modules = {
            'openhands.sdk': FakeSDKModule,
            'openhands.sdk.settings': FakeSettingsModule,
            'openhands.tools': FakeToolsModule,
            'openhands.agent_server.models': FakeAgentServerModelsModule,
        }
        if name in modules:
            return modules[name]
        raise ModuleNotFoundError(name)

    monkeypatch.setattr('mghands_sandbox.sdk_runtime.importlib.import_module', fake_import_module)

    request = StartConversationRequest(
        initial_message={
            'role': 'user',
            'content': [{'type': 'text', 'text': 'create hello.txt'}],
            'run': True,
        }
    )
    runtime = RuntimeConversation(info=ConversationInfo())

    conversation = _OfficialSDKAdapter()._build_official_conversation(request, runtime)

    assert conversation.kwargs['agent'] == 'agent'
    assert callable(conversation.kwargs['callbacks'][0])
    assert conversation.kwargs['conversation_id'] == runtime.info.id
    assert 'initial_message' not in captured['conversation_settings']


def test_sdk_adapter_event_callback_records_openhands_events() -> None:
    class FakeVisualize:
        plain = 'Tool: file_editor\nResult: created file'

    class FakeEvent:
        id = 'sdk-event-1'
        timestamp = '2026-07-07T00:00:00'
        source = 'agent'
        visualize = FakeVisualize()

        def model_dump(self, mode='json'):
            return {'id': self.id, 'source': self.source, 'tool_name': 'file_editor'}

    runtime = RuntimeConversation(info=ConversationInfo())
    callback = _OfficialSDKAdapter()._build_event_callback(runtime)

    callback(FakeEvent())

    assert runtime.events[0].kind == 'openhands.FakeEvent'
    assert runtime.events[0].data == {
        'event_type': 'FakeEvent',
        'source': 'agent',
        'sdk_event_id': 'sdk-event-1',
        'sdk_timestamp': '2026-07-07T00:00:00',
        'raw': {'id': 'sdk-event-1', 'source': 'agent', 'tool_name': 'file_editor'},
        'preview': 'Tool: file_editor\nResult: created file',
    }


def test_sdk_event_payload_handles_non_pydantic_events() -> None:
    class FakeEvent:
        id = 'sdk-event-2'
        timestamp = '2026-07-07T00:00:01'
        source = 'environment'

        def __str__(self):
            return 'plain event'

    assert _sdk_event_payload(FakeEvent()) == {
        'event_type': 'FakeEvent',
        'source': 'environment',
        'sdk_event_id': 'sdk-event-2',
        'sdk_timestamp': '2026-07-07T00:00:01',
        'raw': 'plain event',
    }


def test_sdk_adapter_sends_prompt_before_running_conversation() -> None:
    calls = []

    class FakeConversation:
        def send_message(self, message):
            calls.append(('send_message', message))

        def run(self):
            calls.append(('run', None))
            return 'done'

    runtime = RuntimeConversation(info=ConversationInfo(), sdk_conversation=FakeConversation())
    message = MessageRequest(
        content=[{'type': 'text', 'text': 'create hello.txt'}],
        run=True,
    )

    result = _OfficialSDKAdapter().run(runtime, message)

    assert result == 'done'
    assert calls == [('send_message', 'create hello.txt'), ('run', None)]


def test_sdk_adapter_falls_back_to_prompt_run_without_send_message() -> None:
    calls = []

    class FakeConversation:
        def run(self, prompt):
            calls.append(('run', prompt))
            return 'done'

    runtime = RuntimeConversation(info=ConversationInfo(), sdk_conversation=FakeConversation())
    message = MessageRequest(
        content=[{'type': 'text', 'text': 'create hello.txt'}],
        run=True,
    )

    result = _OfficialSDKAdapter().run(runtime, message)

    assert result == 'done'
    assert calls == [('run', 'create hello.txt')]


def test_sdk_adapter_resets_conversation_terminal_status() -> None:
    class FakeState:
        def __init__(self, status):
            self.status = status

    class FakeConversation:
        def __init__(self, status):
            self.state = FakeState(status)

        def run(self):
            return 'done'

    runtime = RuntimeConversation(info=ConversationInfo(), sdk_conversation=FakeConversation('completed'))
    message = MessageRequest(
        content=[{'type': 'text', 'text': 'next task'}],
        run=True,
    )

    result = _OfficialSDKAdapter().run(runtime, message)

    assert result == 'done'
    assert runtime.sdk_conversation.state.status == 'idle'


def test_sdk_adapter_passes_persistence_dir_to_direct_constructor(tmp_path, monkeypatch) -> None:
    class FakeConversation:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

    class FakeSDKModule:
        Conversation = FakeConversation

    request = StartConversationRequest(
        conversation_id='stable-id',
        working_dir=str(tmp_path),
        persistence_dir=str(tmp_path / 'conversations'),
        restore=True,
    )
    runtime = RuntimeConversation(info=ConversationInfo(id='stable-id'))
    adapter = _OfficialSDKAdapter()
    monkeypatch.setattr(adapter, '_build_official_conversation', lambda request, runtime: None)
    monkeypatch.setattr(adapter, '_build_default_agent', lambda request, runtime: 'agent')
    monkeypatch.setattr(
        'mghands_sandbox.sdk_runtime.importlib.import_module',
        lambda name: FakeSDKModule,
    )

    conversation = adapter.build(request, runtime)

    assert conversation.kwargs['conversation_id'] == 'stable-id'
    assert conversation.kwargs['persistence_dir'] == str(tmp_path / 'conversations')
    assert callable(conversation.kwargs['callbacks'][0])


def test_sdk_event_callback_uses_stable_sdk_id_and_deduplicates() -> None:
    class FakeEvent:
        id = 'stable-sdk-event'
        timestamp = '2026-07-10T00:00:00'
        source = 'agent'

        def model_dump(self, mode='json'):
            return {'id': self.id}

    runtime = RuntimeConversation(info=ConversationInfo())
    callback = _OfficialSDKAdapter()._build_event_callback(runtime)

    callback(FakeEvent())
    callback(FakeEvent())

    assert [event.id for event in runtime.events] == ['stable-sdk-event']


@async_test
async def test_runtime_validates_containment_and_rejects_duplicate_id(
    tmp_path, monkeypatch
) -> None:
    userspace = tmp_path / 'userspace'
    userspace.mkdir()
    runtime = SDKRuntime(userspace_root=str(userspace))
    monkeypatch.setattr(runtime, '_ensure_sdk_available', lambda: None)
    captured = {}

    def build(request, runtime_conversation):
        captured['request'] = request
        return object()

    monkeypatch.setattr(runtime, '_build_sdk_conversation', build)
    request = StartConversationRequest(
        conversation_id='stable-id',
        working_dir='projects/project-1/workspace',
        restore=True,
    )

    info = await runtime.create_conversation(request)
    assert info.working_dir == str(userspace / 'projects/project-1/workspace')
    assert captured['request'].persistence_dir == str(userspace / '.mghands/conversations')
    with pytest.raises(ConversationConflictError):
        await runtime.create_conversation(request)
    with pytest.raises(ValueError, match='working_dir must be within'):
        await runtime.create_conversation(
            StartConversationRequest(working_dir=str(tmp_path / 'outside'))
        )
    with pytest.raises(ValueError, match='persistence_dir must be within'):
        await runtime.create_conversation(
            StartConversationRequest(persistence_dir=str(tmp_path / 'outside'))
        )


def test_runtime_uses_configured_userspace_root(tmp_path, monkeypatch) -> None:
    userspace = tmp_path / 'configured-userspace'
    userspace.mkdir()
    monkeypatch.setenv('MGHANDS_SANDBOX_USERSPACE_ROOT', str(userspace))

    runtime = SDKRuntime()

    assert runtime.runtime_info().workspace_dir == str(userspace)


def test_sdk_adapter_restores_durable_events_with_stable_ids() -> None:
    class FakeEvent:
        id = 'persisted-event'
        source = 'agent'
        timestamp = '2026-07-10T00:00:00'

        def model_dump(self, mode='json'):
            return {'id': self.id}

    class FakeState:
        events = [FakeEvent()]

    class FakeConversation:
        state = FakeState()

    runtime = RuntimeConversation(info=ConversationInfo())

    _OfficialSDKAdapter().restore_events(FakeConversation(), runtime)

    assert [event.id for event in runtime.events] == ['persisted-event']


def test_sdk_event_callback_generates_stable_id_when_sdk_id_is_missing() -> None:
    class FakeEvent:
        source = 'agent'
        timestamp = '2026-07-10T00:00:00'

        def model_dump(self, mode='json'):
            return {'source': self.source, 'message': 'same event'}

    first_runtime = RuntimeConversation(info=ConversationInfo())
    second_runtime = RuntimeConversation(info=ConversationInfo())

    _OfficialSDKAdapter()._build_event_callback(first_runtime)(FakeEvent())
    _OfficialSDKAdapter()._build_event_callback(second_runtime)(FakeEvent())

    assert first_runtime.events[0].id.startswith('sdk-')
    assert first_runtime.events[0].id == second_runtime.events[0].id


@async_test
async def test_runtime_rejects_parallel_run_for_same_conversation(tmp_path) -> None:
    runtime = SDKRuntime(userspace_root=str(tmp_path))
    conversation = RuntimeConversation(info=ConversationInfo(id='one'))
    runtime._conversations['one'] = conversation
    release = threading.Event()
    runtime._run_sdk_conversation = lambda runtime, message: release.wait(1)
    message = MessageRequest(content=[{'type': 'text', 'text': 'run'}])

    await runtime.send_message('one', message)
    with pytest.raises(ConversationBusyError):
        await runtime.send_message('one', message)
    release.set()
    await conversation.execution_task


@async_test
async def test_runtime_serializes_runs_across_conversations(tmp_path) -> None:
    runtime = SDKRuntime(userspace_root=str(tmp_path))
    conversations = [
        RuntimeConversation(info=ConversationInfo(id=conversation_id))
        for conversation_id in ('one', 'two')
    ]
    runtime._conversations.update({item.info.id: item for item in conversations})
    state = {'active': 0, 'maximum': 0}
    state_lock = threading.Lock()

    def run(runtime_conversation, message):
        with state_lock:
            state['active'] += 1
            state['maximum'] = max(state['maximum'], state['active'])
        time.sleep(0.05)
        with state_lock:
            state['active'] -= 1

    runtime._run_sdk_conversation = run
    message = MessageRequest(content=[{'type': 'text', 'text': 'run'}])

    await asyncio.gather(
        runtime.send_message('one', message),
        runtime.send_message('two', message),
    )
    await asyncio.gather(*(item.execution_task for item in conversations))

    assert state['maximum'] == 1


@async_test
async def test_delete_cancels_registered_execution_task(tmp_path) -> None:
    runtime = SDKRuntime(userspace_root=str(tmp_path))
    conversation = RuntimeConversation(info=ConversationInfo(id='one'))
    runtime._conversations['one'] = conversation
    release = threading.Event()
    runtime._run_sdk_conversation = lambda runtime, message: release.wait(1)
    message = MessageRequest(content=[{'type': 'text', 'text': 'run'}])

    await runtime.send_message('one', message)
    assert await runtime.delete_conversation('one') is True
    release.set()

    assert conversation.execution_task.cancelled()
    assert 'one' not in runtime._tasks
    assert await runtime.get_conversation('one') is None


def test_unmatched_action_detection(tmp_path, monkeypatch) -> None:
    events = [
        EventRecord(id='act-1', kind='openhands.CmdRunAction', data={}),
    ]
    from mghands_sandbox.sdk_runtime import _find_unmatched_action, _is_action_read_only
    has_unmatched, unmatched = _find_unmatched_action(events)
    assert has_unmatched is True
    assert unmatched.id == 'act-1'
    assert _is_action_read_only(unmatched) is False

    events_ro = [
        EventRecord(id='act-2', kind='openhands.FileReadAction', data={}),
    ]
    has_unmatched_ro, unmatched_ro = _find_unmatched_action(events_ro)
    assert has_unmatched_ro is True
    assert unmatched_ro.id == 'act-2'
    assert _is_action_read_only(unmatched_ro) is True


@async_test
async def test_resume_empty_message_sdk_run(tmp_path, monkeypatch) -> None:
    class FakeConversation:
        def __init__(self):
            self.run_called = False
            self.send_message_called = False
            self.state = None
        def run(self, *args, **kwargs):
            self.run_called = True
            return "done"
        def send_message(self, *args, **kwargs):
            self.send_message_called = True

    conv = FakeConversation()
    runtime = RuntimeConversation(info=ConversationInfo(id='test'), sdk_conversation=conv)
    adapter = _OfficialSDKAdapter()
    
    msg_non_empty = MessageRequest(content=[TextContent(text="hello")], run=True)
    await asyncio.to_thread(adapter.run, runtime, msg_non_empty)
    assert conv.send_message_called is True
    assert conv.run_called is True

    conv_resume = FakeConversation()
    runtime_resume = RuntimeConversation(info=ConversationInfo(id='test'), sdk_conversation=conv_resume)
    msg_empty = MessageRequest(content=[TextContent(text=" ")], run=True)
    await asyncio.to_thread(adapter.run, runtime_resume, msg_empty)
    assert conv_resume.send_message_called is False
    assert conv_resume.run_called is True
