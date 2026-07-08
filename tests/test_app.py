import asyncio

from fastapi.testclient import TestClient

from mghands_gateway.app import app, get_sandbox_backend, get_settings, get_store
from mghands_gateway.auth import hash_password
from mghands_gateway.config import Settings
from mghands_gateway.models import UserRecord, UserRole
from mghands_gateway.sandbox_backend import SandboxHandle
from mghands_gateway.session_store import SessionStore


class FakeSandboxBackend:
    async def create(self, request, workspace_dir=None) -> SandboxHandle:
        return SandboxHandle(
            sandbox_id='sandbox-1',
            sandbox_url='http://127.0.0.1:3000',
            sandbox_api_key='sk-test',
            container_name='container-1',
            workspace_dir=str(workspace_dir or 'workspace-1'),
        )


def _settings(tmp_path) -> Settings:
    return Settings(
        data_root=tmp_path / 'data',
        database_path=tmp_path / 'sessions.sqlite3',
        shared_skills_root=tmp_path / 'shared_skills',
    )


def _store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / 'sessions.sqlite3')


def _auth_headers(tmp_path, client: TestClient, username='admin') -> dict[str, str]:
    store = _store(tmp_path)
    asyncio.run(
        store.create_user(
            UserRecord(
                username=username,
                password_hash=hash_password('password123'),
                role=UserRole.ADMIN,
            )
        )
    )
    response = client.post(
        '/api/v1/auth/login',
        json={'username': username, 'password': 'password123'},
    )
    assert response.status_code == 200
    return {'Authorization': f"Bearer {response.json()['access_token']}"}


def test_create_session_rejects_process_sandbox_by_default(tmp_path) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    app.dependency_overrides[get_store] = lambda: _store(tmp_path)
    app.dependency_overrides[get_sandbox_backend] = lambda: FakeSandboxBackend()
    try:
        client = TestClient(app)
        headers = _auth_headers(tmp_path, client)
        response = client.post(
            '/api/v1/sessions',
            json={'session_id': 'tenant-a', 'sandbox_type': 'process'},
            headers=headers,
        )
        assert response.status_code == 400
        assert 'Only docker sandbox sessions are allowed' in response.json()['detail']
    finally:
        app.dependency_overrides.clear()


def test_create_session_allows_docker_sandbox(tmp_path) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    app.dependency_overrides[get_store] = lambda: _store(tmp_path)
    app.dependency_overrides[get_sandbox_backend] = lambda: FakeSandboxBackend()
    try:
        client = TestClient(app)
        headers = _auth_headers(tmp_path, client)
        response = client.post(
            '/api/v1/sessions',
            json={'session_id': 'tenant-a', 'sandbox_type': 'docker'},
            headers=headers,
        )
        assert response.status_code == 201
        assert response.json()['sandbox_id'] == 'sandbox-1'
        assert response.json()['project_id'].startswith('prj_')
    finally:
        app.dependency_overrides.clear()


def test_session_endpoint_requires_auth(tmp_path) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    app.dependency_overrides[get_store] = lambda: _store(tmp_path)
    app.dependency_overrides[get_sandbox_backend] = lambda: FakeSandboxBackend()
    try:
        client = TestClient(app)
        response = client.post(
            '/api/v1/sessions',
            json={'session_id': 'tenant-a', 'sandbox_type': 'docker'},
        )
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_project_session_conflict_for_active_session(tmp_path) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    app.dependency_overrides[get_store] = lambda: _store(tmp_path)
    app.dependency_overrides[get_sandbox_backend] = lambda: FakeSandboxBackend()
    try:
        client = TestClient(app)
        headers = _auth_headers(tmp_path, client)
        project = client.post('/api/v1/projects', json={'name': 'Demo'}, headers=headers)
        assert project.status_code == 201
        project_id = project.json()['project_id']
        first = client.post(
            f'/api/v1/projects/{project_id}/sessions',
            json={'session_id': 'tenant-a'},
            headers=headers,
        )
        assert first.status_code == 201
        second = client.post(
            f'/api/v1/projects/{project_id}/sessions',
            json={'session_id': 'tenant-b'},
            headers=headers,
        )
        assert second.status_code == 409
        assert second.json()['detail']['running_session_id'] == 'tenant-a'
    finally:
        app.dependency_overrides.clear()
