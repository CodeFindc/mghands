import asyncio
from pathlib import Path

from mghands_gateway.config import Settings
from mghands_gateway.models import CreateSessionRequest
from mghands_gateway.sandbox_backend import DockerSandboxBackend


class FakeAgentClient:
    async def alive(self, sandbox_url: str, session_api_key: str) -> bool:
        return True


class RecordingDockerSandboxBackend(DockerSandboxBackend):
    def __init__(self, settings: Settings):
        super().__init__(settings, FakeAgentClient())
        self.ran_container = False
        self.published_port_checked = False

    def _run_container(
        self, container_name: str, session_api_key: str, workspace_dir: Path
    ) -> None:
        self.ran_container = True

    def _published_port(self, container_name: str) -> int:
        self.published_port_checked = True
        return 49152


def test_docker_backend_uses_internal_network_url_when_enabled(tmp_path) -> None:
    settings = Settings(
        sandbox_workspace_root=tmp_path / 'workspaces',
        sandbox_use_internal_network=True,
        sandbox_internal_port=3000,
    )
    backend = RecordingDockerSandboxBackend(settings)

    handle = asyncio.run(backend.create(CreateSessionRequest(session_id='sess-a')))

    assert backend.ran_container is True
    assert backend.published_port_checked is False
    assert handle.sandbox_url == 'http://mghands-sess-a:3000'


def test_docker_backend_keeps_published_port_url_by_default(tmp_path) -> None:
    settings = Settings(
        sandbox_workspace_root=tmp_path / 'workspaces',
        sandbox_host='127.0.0.1',
    )
    backend = RecordingDockerSandboxBackend(settings)

    handle = asyncio.run(backend.create(CreateSessionRequest(session_id='sess-a')))

    assert backend.ran_container is True
    assert backend.published_port_checked is True
    assert handle.sandbox_url == 'http://127.0.0.1:49152'
