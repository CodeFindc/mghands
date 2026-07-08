import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    SerializationInfo,
    field_serializer,
    field_validator,
)

SESSION_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')
NAME_PATTERN = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')
SENSITIVE_KEYS = {'api_key', 'apikey', 'authorization', 'cookie', 'token', 'secret'}


def utc_now() -> datetime:
    return datetime.now(UTC)


def validate_session_id(session_id: str) -> str:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError('session_id may only contain letters, digits, underscore, and hyphen')
    return session_id


def validate_safe_name(value: str, field_name: str = 'name') -> str:
    if not NAME_PATTERN.fullmatch(value):
        raise ValueError(f'{field_name} may only contain letters, digits, underscore, dot, and hyphen')
    return value


def new_id(prefix: str) -> str:
    return f'{prefix}_{uuid4().hex}'


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


class UserRole(StrEnum):
    ADMIN = 'admin'
    USER = 'user'


class ProjectStatus(StrEnum):
    ACTIVE = 'active'
    DELETED = 'deleted'


class SandboxType(StrEnum):
    DOCKER = 'docker'
    PROCESS = 'process'
    REMOTE = 'remote'


class WorkspacePolicy(StrEnum):
    ISOLATED = 'isolated'
    DEFAULT = 'default'


class LLMOverride(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: SecretStr | None = None

    @field_serializer('api_key')
    def serialize_api_key(
        self, api_key: SecretStr | None, info: SerializationInfo
    ) -> str | None:
        if api_key is None:
            return None
        if info.context and info.context.get('expose_secrets'):
            return api_key.get_secret_value()
        return '**********'


class SkillSpec(BaseModel):
    name: str
    content: str
    type: str = 'knowledge'
    triggers: list[str] = Field(default_factory=list)

    @field_validator('name')
    @classmethod
    def validate_name(cls, value: str) -> str:
        return validate_safe_name(value, 'skill name')


class MCPConfigSpec(BaseModel):
    mcpServers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class CreateSessionRequest(BaseModel):
    session_id: str = Field(default_factory=lambda: new_id('sess'))
    project_id: str | None = None
    project_name: str | None = None
    skill_names: list[str] = Field(default_factory=list)
    sandbox_type: SandboxType = SandboxType.DOCKER
    workspace_policy: WorkspacePolicy = WorkspacePolicy.ISOLATED

    @field_validator('session_id')
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_session_id(value)

    @field_validator('skill_names')
    @classmethod
    def validate_skill_names(cls, value: list[str]) -> list[str]:
        return [validate_safe_name(item, 'skill name') for item in value]


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
    project_id: str | None = None
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
    project_id: str | None = None
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
    project_id: str | None = None
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


class UserRecord(BaseModel):
    user_id: str = Field(default_factory=lambda: new_id('usr'))
    username: str
    password_hash: str
    role: UserRole = UserRole.USER
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class UserResponse(BaseModel):
    user_id: str
    username: str
    role: UserRole
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: UserRecord) -> 'UserResponse':
        return cls(**record.model_dump(exclude={'password_hash'}))


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=1024)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal['bearer'] = 'bearer'
    expires_at: datetime


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=1024)
    role: UserRole = UserRole.USER
    enabled: bool = True


class UpdateUserRequest(BaseModel):
    enabled: bool | None = None
    role: UserRole | None = None


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=1024)


class AuthTokenRecord(BaseModel):
    token_id: str = Field(default_factory=lambda: new_id('tok'))
    user_id: str
    token_hash: str
    expires_at: datetime
    revoked_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    last_used_at: datetime | None = None


class ProjectRecord(BaseModel):
    project_id: str = Field(default_factory=lambda: new_id('prj'))
    user_id: str
    name: str
    workspace_dir: str
    status: ProjectStatus = ProjectStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProjectResponse(BaseModel):
    project_id: str
    name: str
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: ProjectRecord) -> 'ProjectResponse':
        return cls(**record.model_dump(exclude={'user_id', 'workspace_dir'}))


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    skill_names: list[str] = Field(default_factory=list)

    @field_validator('skill_names')
    @classmethod
    def validate_skill_names(cls, value: list[str]) -> list[str]:
        return [validate_safe_name(item, 'skill name') for item in value]


class CreateProjectSessionRequest(BaseModel):
    session_id: str = Field(default_factory=lambda: new_id('sess'))
    sandbox_type: SandboxType = SandboxType.DOCKER
    workspace_policy: WorkspacePolicy = WorkspacePolicy.ISOLATED

    @field_validator('session_id')
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_session_id(value)


class InstalledSkillMetadata(BaseModel):
    requires_dependencies: bool = False
    dependency_manifest: str | None = None
    dependency_status: str | None = None
    dependency_note: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    description: str | None = None
    triggers: list[str] = Field(default_factory=list)


class SkillCatalogItem(BaseModel):
    name: str
    valid: bool = True
    error: str | None = None
    metadata: InstalledSkillMetadata = Field(default_factory=InstalledSkillMetadata)


class ProjectSkillRecord(BaseModel):
    project_id: str
    skill_name: str
    source_fingerprint: str | None = None
    metadata: InstalledSkillMetadata = Field(default_factory=InstalledSkillMetadata)
    installed_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProjectSkillResponse(BaseModel):
    skill_name: str
    source_fingerprint: str | None = None
    metadata: InstalledSkillMetadata
    installed_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: ProjectSkillRecord) -> 'ProjectSkillResponse':
        return cls(**record.model_dump(exclude={'project_id'}))


class InstallProjectSkillRequest(BaseModel):
    skill_name: str

    @field_validator('skill_name')
    @classmethod
    def validate_skill_name(cls, value: str) -> str:
        return validate_safe_name(value, 'skill name')
