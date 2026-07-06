from fastapi.testclient import TestClient

from mghands_gateway.app import app, get_sandbox_backend, get_settings, get_store
from mghands_gateway.config import Settings
from mghands_gateway.sandbox_backend import SandboxHandle
from mghands_gateway.session_store import SessionStore


class FakeSandboxBackend:
    async def create(self, request) -> SandboxHandle:
        return SandboxHandle(
            sandbox_id='sandbox-1',
            sandbox_url='http://127.0.0.1:3000',
            sandbox_api_key='sk-test',
            container_name='container-1',
            workspace_dir='workspace-1',
        )


def test_create_session_rejects_process_sandbox_by_default(tmp_path) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_path=tmp_path / 'sessions.sqlite3'
    )
    app.dependency_overrides[get_store] = lambda: SessionStore(
        tmp_path / 'sessions.sqlite3'
    )
    app.dependency_overrides[get_sandbox_backend] = lambda: FakeSandboxBackend()
    try:
        client = TestClient(app)
        response = client.post(
            '/api/v1/sessions',
            json={'session_id': 'tenant-a', 'sandbox_type': 'process'},
        )
        assert response.status_code == 400
        assert 'Only docker sandbox sessions are allowed' in response.json()['detail']
    finally:
        app.dependency_overrides.clear()


def test_create_session_allows_docker_sandbox(tmp_path) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_path=tmp_path / 'sessions.sqlite3'
    )
    app.dependency_overrides[get_store] = lambda: SessionStore(
        tmp_path / 'sessions.sqlite3'
    )
    app.dependency_overrides[get_sandbox_backend] = lambda: FakeSandboxBackend()
    try:
        client = TestClient(app)
        response = client.post(
            '/api/v1/sessions',
            json={'session_id': 'tenant-a', 'sandbox_type': 'docker'},
        )
        assert response.status_code == 201
        assert response.json()['sandbox_id'] == 'sandbox-1'
    finally:
        app.dependency_overrides.clear()
