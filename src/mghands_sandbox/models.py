from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, SecretStr, field_serializer


def utc_now() -> datetime:
    return datetime.now(UTC)


class ConversationStatus(StrEnum):
    CREATED = 'created'
    RUNNING = 'running'
    COMPLETED = 'completed'
    ERROR = 'error'
    DELETED = 'deleted'


class RuntimeStatus(StrEnum):
    STARTING = 'starting'
    READY = 'ready'
    BUSY = 'busy'
    ERROR = 'error'
    SHUTTING_DOWN = 'shutting_down'


class TextContent(BaseModel):
    type: Literal['text'] = 'text'
    text: str


class MessageRequest(BaseModel):
    role: Literal['user'] = 'user'
    content: list[TextContent]
    run: bool = True


class LLMConfig(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: SecretStr | None = None

    @field_serializer('api_key')
    def serialize_api_key(self, api_key: SecretStr | None) -> str | None:
        if api_key is None:
            return None
        return '**********'


class SkillInjection(BaseModel):
    name: str
    content: str
    type: str = 'knowledge'
    triggers: list[str] = Field(default_factory=list)


class MCPInjection(BaseModel):
    mcpServers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class StartConversationRequest(BaseModel):
    conversation_id: str | None = None
    initial_message: MessageRequest | None = None
    llm: LLMConfig | None = None
    skills: list[SkillInjection] = Field(default_factory=list)
    mcp_config: MCPInjection | None = None
    working_dir: str = '/workspace'


class ConversationInfo(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    status: ConversationStatus = ConversationStatus.CREATED
    working_dir: str = '/workspace'
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    error: str | None = None


class ConversationRuntimeState(BaseModel):
    conversation: ConversationInfo
    llm_model: str | None = None
    skills: list[SkillInjection] = Field(default_factory=list)
    mcp_config: MCPInjection | None = None
    event_count: int = 0


class EventRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    kind: str = 'message'
    timestamp: datetime = Field(default_factory=utc_now)
    data: dict[str, Any]


class EventPage(BaseModel):
    items: list[dict[str, Any]]
    next_page_id: str | None = None


class UpdateRuntimeRequest(BaseModel):
    skills: list[SkillInjection] | None = None
    mcp_config: MCPInjection | None = None


class SandboxRuntimeInfo(BaseModel):
    status: RuntimeStatus
    sdk_available: bool
    sdk_error: str | None = None
    workspace_dir: str = '/workspace'
    conversation_count: int = 0
    active_conversation_ids: list[str] = Field(default_factory=list)
    session_auth_enabled: bool = False
    default_coding_tools_enabled: bool = True
    browser_tools_enabled: bool = False


class ServerInfo(BaseModel):
    name: str = 'mghands-sandbox'
    version: str = '0.1.0'
    api_version: str = '2026-07-06'
    standard_endpoints: list[str] = Field(default_factory=list)
    supports_dynamic_skills: bool = True
    supports_dynamic_mcp: bool = True
    supports_shutdown: bool = True
    default_coding_tools_enabled: bool = True
    browser_tools_enabled: bool = False
    default_tool_sources: list[str] = Field(default_factory=list)


class ShutdownRequest(BaseModel):
    delay_seconds: float = Field(default=0.2, ge=0, le=30)


class Success(BaseModel):
    success: bool = True
