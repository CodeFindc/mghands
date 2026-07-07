from fastapi.testclient import TestClient

from mghands_sandbox.app import app
from mghands_sandbox.models import (
    ConversationInfo,
    LLMConfig,
    MessageRequest,
    StartConversationRequest,
)
from mghands_sandbox.sdk_runtime import RuntimeConversation, _OfficialSDKAdapter, _sdk_event_payload


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
