from mghands_gateway.agent_client import _start_conversation_payload
from mghands_gateway.models import LLMOverride, MCPConfigSpec, SkillSpec


def test_start_conversation_payload_includes_dynamic_runtime_config() -> None:
    payload = _start_conversation_payload(
        'write tests',
        LLMOverride(model='gpt-4o-mini', base_url='https://example.test'),
        [SkillSpec(name='testing', content='Always run pytest', triggers=['test'])],
        MCPConfigSpec(mcpServers={'local': {'command': 'echo'}}),
    )

    assert payload['initial_message']['content'][0]['text'] == 'write tests'
    assert payload['llm']['model'] == 'gpt-4o-mini'
    assert payload['skills'][0]['name'] == 'testing'
    assert payload['mcp_config']['mcpServers']['local']['command'] == 'echo'


def test_start_conversation_payload_forwards_llm_provider() -> None:
    payload = _start_conversation_payload(
        'hello',
        LLMOverride(provider='openai', model='custom-model'),
        None,
        None,
    )

    assert payload['llm']['provider'] == 'openai'
    assert payload['llm']['model'] == 'custom-model'
