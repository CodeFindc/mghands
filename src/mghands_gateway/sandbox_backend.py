import asyncio
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from mghands_gateway.agent_client import AgentServerClient
from mghands_gateway.config import Settings
from mghands_gateway.models import CreateSessionRequest


@dataclass(frozen=True)
class SandboxHandle:
    sandbox_id: str
    sandbox_url: str
    sandbox_api_key: str
    container_name: str
    workspace_dir: str


class DockerSandboxBackend:
    def __init__(self, settings: Settings, agent_client: AgentServerClient):
        self.settings = settings
        self.agent_client = agent_client

    async def create(
        self, request: CreateSessionRequest, workspace_dir: Path | None = None
    ) -> SandboxHandle:
        sandbox_id = f'mghands-{request.session_id}'
        container_name = sandbox_id
        session_api_key = 'sk-mghands-' + secrets.token_urlsafe(24)
        workspace_dir = workspace_dir.resolve() if workspace_dir else self._prepare_workspace(request.session_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            self._run_container,
            container_name,
            session_api_key,
            workspace_dir,
        )
        host_port = await asyncio.to_thread(self._published_port, container_name)
        sandbox_url = f'http://{self.settings.sandbox_host}:{host_port}'
        await self._wait_until_ready(sandbox_url, session_api_key)
        return SandboxHandle(
            sandbox_id=sandbox_id,
            sandbox_url=sandbox_url,
            sandbox_api_key=session_api_key,
            container_name=container_name,
            workspace_dir=str(workspace_dir),
        )

    async def delete(self, container_name: str | None) -> None:
        if not container_name:
            return
        await asyncio.to_thread(
            self._docker,
            ['rm', '-f', container_name],
            check=False,
        )

    def _prepare_workspace(self, session_id: str) -> Path:
        workspace_dir = self.settings.sandbox_workspace_root / session_id
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return workspace_dir.resolve()

    def _run_container(
        self, container_name: str, session_api_key: str, workspace_dir: Path
    ) -> None:
        self._docker(['rm', '-f', container_name], check=False)
        args = [
            'run',
            '-d',
            '--name',
            container_name,
            '-p',
            f'127.0.0.1::{self.settings.sandbox_internal_port}',
            '-e',
            f'OH_SESSION_API_KEYS_0={session_api_key}',
            '-v',
            f'{workspace_dir}:{self.settings.sandbox_workspace_mount_path}',
            '--memory',
            self.settings.sandbox_memory_limit,
            '--cpus',
            self.settings.sandbox_cpus,
            '--pids-limit',
            str(self.settings.sandbox_pids_limit),
            '--security-opt',
            'no-new-privileges',
        ]
        if self.settings.sandbox_network:
            args.extend(['--network', self.settings.sandbox_network])
        args.append(self.settings.sandbox_image)
        if self.settings.sandbox_command:
            args.extend(self.settings.sandbox_command.split())
        self._docker(args)

    def _published_port(self, container_name: str) -> int:
        result = self._docker(
            ['port', container_name, str(self.settings.sandbox_internal_port)]
        )
        mapping = result.stdout.strip().splitlines()[0]
        return int(mapping.rsplit(':', 1)[1])

    async def _wait_until_ready(self, sandbox_url: str, session_api_key: str) -> None:
        deadline = time.monotonic() + self.settings.sandbox_start_timeout_seconds
        while time.monotonic() < deadline:
            if await self.agent_client.alive(sandbox_url, session_api_key):
                return
            await asyncio.sleep(self.settings.sandbox_start_poll_seconds)
        raise TimeoutError(f'Sandbox did not become ready: {sandbox_url}')

    def _docker(
        self, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        command = ['docker', *args]
        try:
            return subprocess.run(
                command,
                check=check,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = _redact_command_text(exc.stderr or '')
            stdout = _redact_command_text(exc.stdout or '')
            raise RuntimeError(
                f'Docker command failed with exit code {exc.returncode}: {stderr or stdout}'
            ) from exc


def _redact_command_text(value: str) -> str:
    return value.replace('OH_SESSION_API_KEYS_0=', 'OH_SESSION_API_KEYS_0=********')
