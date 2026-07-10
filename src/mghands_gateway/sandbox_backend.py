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
        self,
        request: CreateSessionRequest,
        workspace_dir: Path | None = None,
        store: object = None,
    ) -> SandboxHandle:
        sandbox_id = f'mghands-{request.session_id}'
        container_name = sandbox_id
        session_api_key = 'sk-mghands-' + secrets.token_urlsafe(24)
        workspace_dir = workspace_dir.resolve() if workspace_dir else self._prepare_workspace(request.session_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)

        sandbox_image = self.settings.sandbox_image
        sandbox_memory_limit = self.settings.sandbox_memory_limit
        sandbox_cpus = self.settings.sandbox_cpus
        sandbox_pids_limit = self.settings.sandbox_pids_limit

        if store and hasattr(store, 'get_all_settings'):
            try:
                overrides = await store.get_all_settings()
                if 'sandbox_image' in overrides:
                    sandbox_image = overrides['sandbox_image']
                if 'sandbox_memory_limit' in overrides:
                    sandbox_memory_limit = overrides['sandbox_memory_limit']
                if 'sandbox_cpus' in overrides:
                    sandbox_cpus = overrides['sandbox_cpus']
                if 'sandbox_pids_limit' in overrides:
                    sandbox_pids_limit = int(overrides['sandbox_pids_limit'])
            except Exception:
                pass

        import inspect
        sig = inspect.signature(self._run_container)
        if 'sandbox_image' in sig.parameters:
            await asyncio.to_thread(
                self._run_container,
                container_name,
                session_api_key,
                workspace_dir,
                sandbox_image=sandbox_image,
                sandbox_memory_limit=sandbox_memory_limit,
                sandbox_cpus=sandbox_cpus,
                sandbox_pids_limit=sandbox_pids_limit,
            )
        else:
            await asyncio.to_thread(
                self._run_container,
                container_name,
                session_api_key,
                workspace_dir,
            )
        if self.settings.sandbox_use_internal_network:
            sandbox_url = f'http://{container_name}:{self.settings.sandbox_internal_port}'
        else:
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

    async def ensure_user_sandbox(
        self,
        *,
        user_id: str,
        generation: int,
        user_root: Path,
        session_api_key: str,
        container_name: str,
        store: object = None,
    ) -> SandboxHandle:
        user_root = user_root.resolve()
        user_root.mkdir(parents=True, exist_ok=True)
        if await asyncio.to_thread(self._container_running, container_name):
            sandbox_url = await self._sandbox_url(container_name)
            if await self.agent_client.alive(sandbox_url, session_api_key):
                return SandboxHandle(
                    sandbox_id=container_name,
                    sandbox_url=sandbox_url,
                    sandbox_api_key=session_api_key,
                    container_name=container_name,
                    workspace_dir=str(user_root),
                )

        sandbox_image = self.settings.sandbox_image
        if store and hasattr(store, 'get_all_settings'):
            overrides = await store.get_all_settings()
            sandbox_image = overrides.get('sandbox_image', sandbox_image)
        await asyncio.to_thread(
            self._run_container,
            container_name,
            session_api_key,
            user_root,
            sandbox_image=sandbox_image,
            mount_path=self.settings.user_sandbox_mount_path,
            labels={
                'mghands.scope': 'user',
                'mghands.user_id': user_id,
                'mghands.generation': str(generation),
            },
            userspace_root=self.settings.user_sandbox_mount_path,
        )
        sandbox_url = await self._sandbox_url(container_name)
        try:
            await self._wait_until_ready(sandbox_url, session_api_key)
        except Exception:
            await self.delete(container_name)
            raise
        return SandboxHandle(
            sandbox_id=container_name,
            sandbox_url=sandbox_url,
            sandbox_api_key=session_api_key,
            container_name=container_name,
            workspace_dir=str(user_root),
        )

    async def _sandbox_url(self, container_name: str) -> str:
        if self.settings.sandbox_use_internal_network:
            return f'http://{container_name}:{self.settings.sandbox_internal_port}'
        host_port = await asyncio.to_thread(self._published_port, container_name)
        return f'http://{self.settings.sandbox_host}:{host_port}'

    def _container_running(self, container_name: str) -> bool:
        result = self._docker(
            ['inspect', '-f', '{{.State.Running}}', container_name], check=False
        )
        return result.returncode == 0 and result.stdout.strip().lower() == 'true'

    def _prepare_workspace(self, session_id: str) -> Path:
        workspace_dir = self.settings.sandbox_workspace_root / session_id
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return workspace_dir.resolve()

    def _run_container(
        self,
        container_name: str,
        session_api_key: str,
        workspace_dir: Path,
        *args,
        sandbox_image: str | None = None,
        sandbox_memory_limit: str | None = None,
        sandbox_cpus: str | None = None,
        sandbox_pids_limit: int | None = None,
        mount_path: str | None = None,
        labels: dict[str, str] | None = None,
        userspace_root: str | None = None,
        **kwargs,
    ) -> None:
        self._docker(['rm', '-f', container_name], check=False)
        sandbox_image = sandbox_image or self.settings.sandbox_image
        sandbox_memory_limit = sandbox_memory_limit or self.settings.sandbox_memory_limit
        sandbox_cpus = sandbox_cpus or self.settings.sandbox_cpus
        sandbox_pids_limit = sandbox_pids_limit if sandbox_pids_limit is not None else self.settings.sandbox_pids_limit

        # Determine volume mount configuration
        volume_mount = ''
        shared_env = []
        if self.settings.host_data_root:
            # Option A: host directory bind mount
            host_workspace_dir = workspace_dir
            try:
                rel_path = workspace_dir.relative_to(self.settings.data_root.resolve())
                host_workspace_dir = self.settings.host_data_root / rel_path
            except ValueError:
                pass

            host_workspace_path_str = str(host_workspace_dir)
            if len(host_workspace_path_str) >= 2 and host_workspace_path_str[1] == ':':
                drive = host_workspace_path_str[0].lower()
                rest = host_workspace_path_str[2:].replace('\\', '/')
                if not rest.startswith('/'):
                    rest = '/' + rest
                host_workspace_path_str = f'/{drive}{rest}'

            mount_path = mount_path or self.settings.sandbox_workspace_mount_path
            volume_mount = f'{host_workspace_path_str}:{mount_path}'
        else:
            # Option B: Named volume mghands-data
            volume_mount = 'mghands-data:/data/mghands'
            # Determine target scope (user vs session) and pass to sandbox to create symlink
            actual_mount_path = mount_path or self.settings.sandbox_workspace_mount_path
            if actual_mount_path == '/userspace':
                shared_env = ['-e', f'MGHANDS_SANDBOX_SHARED_VOLUME_USERSPACE={workspace_dir}']
            else:
                shared_env = ['-e', f'MGHANDS_SANDBOX_SHARED_VOLUME_WORKSPACE={workspace_dir}']
            userspace_root = None

        container_args = [
            'run',
            '-d',
            '--name',
            container_name,
            '-p',
            f'127.0.0.1::{self.settings.sandbox_internal_port}',
            '-e',
            f'OH_SESSION_API_KEYS_0={session_api_key}',
            '-v',
            volume_mount,
            '--memory',
            sandbox_memory_limit,
            '--cpus',
            sandbox_cpus,
            '--pids-limit',
            str(sandbox_pids_limit),
            '--security-opt',
            'no-new-privileges',
            '--cap-drop',
            'ALL',
        ]
        if shared_env:
            container_args.extend(shared_env)
        if userspace_root:
            container_args.extend(['-e', f'MGHANDS_SANDBOX_USERSPACE_ROOT={userspace_root}'])
        for key, value in (labels or {}).items():
            container_args.extend(['--label', f'{key}={value}'])
        if self.settings.sandbox_network:
            container_args.extend(['--network', self.settings.sandbox_network])
        container_args.append(sandbox_image)
        if self.settings.sandbox_command:
            container_args.extend(self.settings.sandbox_command.split())
        self._docker(container_args)

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

    def list_user_containers(self) -> list[str]:
        result = self._docker(
            ['ps', '-a', '--filter', 'label=mghands.scope=user', '--format', '{{.Names}}'],
            check=False,
        )
        if result.returncode != 0:
            return []
        return [name.strip() for name in result.stdout.splitlines() if name.strip()]


def _redact_command_text(value: str) -> str:
    return value.replace('OH_SESSION_API_KEYS_0=', 'OH_SESSION_API_KEYS_0=********')
