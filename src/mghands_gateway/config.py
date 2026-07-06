from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='MGHANDS_', env_file='.env')

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
