import pytest
from pydantic import SecretStr, ValidationError

from mghands_gateway.models import CreateSessionRequest, LLMOverride, redact_sensitive


def test_session_id_validation_accepts_safe_ids() -> None:
    request = CreateSessionRequest(session_id='tenant-a_task-001')
    assert request.session_id == 'tenant-a_task-001'


@pytest.mark.parametrize('session_id', ['../x', 'a/b', 'a b', ''])
def test_session_id_validation_rejects_unsafe_ids(session_id: str) -> None:
    with pytest.raises(ValidationError):
        CreateSessionRequest(session_id=session_id)


def test_llm_api_key_serializes_redacted() -> None:
    llm = LLMOverride(model='deepseek-chat', api_key=SecretStr('sk-test'))
    assert llm.model_dump(mode='json')['api_key'] == '**********'


def test_redact_sensitive_nested_values() -> None:
    value = {'headers': {'Authorization': 'Bearer token'}, 'api_key': 'sk-test'}
    assert redact_sensitive(value) == {
        'headers': {'Authorization': '**********'},
        'api_key': '**********',
    }
