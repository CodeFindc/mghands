import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_serializer, field_validator

SESSION_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')
SENSITIVE_KEYS = {'api_key', 'apikey', 'authorization', 'cookie', 'token', 'secret'}


def utc_now() -> datetime:
    return datetime.now(UTC)


def validate_session_id(session_id: str) -> str:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError('session_id may only contain letters, digits, underscore, and hyphen')
    return session_id


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return '**********'
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_lower = str(key).lower()
            if any(sensitive in key_lower for sensitive in SENSITIVE_KEYS):
                redacted[key] = '**********'
            else:
                redacted[key] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


class SessionStatus(StrEnum):
    CREATED = 'created'
    RUNNING = 'running'
    COMPLETED = 'completed'
    ERROR = 'error'
    DELETED = 'deleted'


class SandboxType(StrEnum):
    DOCKER = 'docker'
    PROCESS = 'process'
    REMOTE = 'remote'


class WorkspacePolicy(StrEnum):
    ISOLATED = 'isolated'
    DEFAULT = 'default'


class LLMOverride(BaseModel):
    model: str | None = None
    base_url: str | None = None
    api_key: SecretStr | None = None

    @field_serializer('api_key')
    def serialize_api_key(self, api_key: SecretStr | None) -> str | None:
        if api_key is None:
            return None
        return '**********'


class SkillSpec(BaseModel):
    name: str
    content: str
    type: str = 'knowledge'
    triggers: list[str] = Field(default_factory=list)


class MCPConfigSpec(BaseModel):
    mcpServers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class CreateSessionRequest(BaseModel):
    session_id: str
    sandbox_type: SandboxType = SandboxType.DOCKER
    workspace_policy: WorkspacePolicy = WorkspacePolicy.ISOLATED

    @field_validator('session_id')
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_session_id(value)


class ExecuteRequest(BaseModel):
    task: str = Field(min_length=1)
    llm: LLMOverride | None = None
    skills: list[SkillSpec] = Field(default_factory=list)
    mcp_config: MCPConfigSpec | None = None
    stream: bool = False


class RuntimeUpdateRequest(BaseModel):
    skills: list[SkillSpec] = Field(default_factory=list)
    mcp_config: MCPConfigSpec | None = None


class SessionRecord(BaseModel):
    session_id: str
    sandbox_id: str | None = None
    sandbox_url: str | None = None
    sandbox_api_key: SecretStr | None = None
    container_name: str | None = None
    conversation_id: str | None = None
    created_by_user_id: str | None = None
    sandbox_type: SandboxType = SandboxType.DOCKER
    workspace_policy: WorkspacePolicy = WorkspacePolicy.ISOLATED
    workspace_dir: str | None = None
    status: SessionStatus = SessionStatus.CREATED
    last_event_id: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_serializer('sandbox_api_key')
    def serialize_sandbox_api_key(
        self, api_key: SecretStr | None, info
    ) -> str | None:
        if api_key is None:
            return None
        if info.context and info.context.get('expose_secrets'):
            return api_key.get_secret_value()
        return '**********'


class SessionResponse(BaseModel):
    session_id: str
    sandbox_id: str | None
    sandbox_url: str | None = None
    conversation_id: str | None
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    last_event_id: str | None = None
    error: str | None = None

    @classmethod
    def from_record(cls, record: SessionRecord) -> 'SessionResponse':
        return cls(**record.model_dump())


class ExecuteResponse(BaseModel):
    session_id: str
    sandbox_id: str | None
    conversation_id: str | None
    status: SessionStatus
    start_task_id: str | None = None


class HistoryResponse(BaseModel):
    session_id: str
    conversation_id: str
    events: list[dict[str, Any]]
    next_page_id: str | None = None


class TerminalEvent(BaseModel):
    type: Literal['completed', 'error', 'cancelled']
    session_id: str
    conversation_id: str | None = None
    detail: str | None = None
