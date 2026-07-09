from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='MGHANDS_', env_file='.env')

    data_root: Path = Field(
        default=Path('.mghands'),
        description='Gateway-managed data root for database, users, projects, and shared skills.',
    )

    host_data_root: Path | None = Field(
        default=None,
        description='Optional host-side data root path mapping for Docker-out-of-Docker deployments.',
    )

    sandbox_image: str = Field(
        default='docker.all-hands.dev/all-hands-ai/runtime:1.29.0',
        description='Docker image containing OpenHands SDK/agent-server standard APIs.',
    )
    sandbox_internal_port: int = Field(
        default=3000,
        description='Container port that exposes the agent-server standard APIs.',
    )
    sandbox_host: str = Field(
        default='127.0.0.1',
        description='Host used by the gateway to reach published sandbox ports.',
    )
    sandbox_network: str | None = Field(
        default=None,
        description='Optional Docker network for sandbox containers.',
    )
    sandbox_use_internal_network: bool = Field(
        default=False,
        description='Use Docker network DNS instead of published host ports for sandbox URLs.',
    )
    sandbox_command: str | None = Field(
        default=None,
        description='Optional command override for the sandbox container.',
    )
    sandbox_workspace_root: Path = Field(
        default=Path('.mghands') / 'workspaces',
        description='Host directory containing per-session isolated workspaces.',
    )
    sandbox_workspace_mount_path: str = Field(
        default='/workspace',
        description='Workspace path mounted inside each sandbox container.',
    )
    sandbox_memory_limit: str = Field(
        default='2g',
        description='Docker memory limit for each sandbox container.',
    )
    sandbox_cpus: str = Field(
        default='2',
        description='Docker CPU quota for each sandbox container.',
    )
    sandbox_pids_limit: int = Field(
        default=512,
        description='Docker process count limit for each sandbox container.',
    )
    database_path: Path = Field(
        default=Path('.mghands') / 'sessions.sqlite3',
        description='SQLite database path for durable session mapping.',
    )
    shared_skills_root: Path | None = Field(
        default=None,
        description='Admin-managed root containing shared skill directories.',
    )
    default_project_skills: list[str] = Field(
        default_factory=list,
        description='Frontend default checked shared skill names for new projects.',
    )
    auth_access_token_ttl_seconds: int = Field(
        default=2592000,
        description='Opaque access token TTL in seconds.',
    )
    auth_public_registration_enabled: bool = False
    auth_registered_user_default_enabled: bool = True
    bootstrap_admin_username: str | None = None
    bootstrap_admin_password: SecretStr | None = None
    allow_non_docker_sandbox: bool = Field(
        default=False,
        description='Allow non-Docker sandbox requests for local development only.',
    )
    sandbox_start_timeout_seconds: float = 120.0
    sandbox_start_poll_seconds: float = 1.0
    request_timeout_seconds: float = 30.0
    conversation_start_timeout_seconds: float = 180.0
    conversation_start_poll_seconds: float = 2.0
    sse_poll_seconds: float = 1.0
    sse_heartbeat_seconds: float = 15.0
    sse_idle_timeout_seconds: float = 300.0

    @field_validator('default_project_skills', mode='before')
    @classmethod
    def parse_default_project_skills(cls, value: Any) -> list[str]:
        if value is None or value == '':
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(',') if item.strip()]
        return list(value)

    @field_validator('shared_skills_root', mode='after')
    @classmethod
    def default_shared_skills_root(cls, value: Path | None, info) -> Path:
        if value is not None:
            return value
        data_root = info.data.get('data_root') or Path('.mghands')
        return Path(data_root) / 'shared_skills'


@lru_cache
def get_settings() -> Settings:
    return Settings()
