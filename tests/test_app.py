import asyncio
from io import BytesIO
import zipfile
from cryptography.fernet import Fernet

from fastapi.testclient import TestClient

from mghands_gateway.app import (
    app,
    get_agent_client,
    get_sandbox_backend,
    get_settings,
    get_store,
)
from mghands_gateway.auth import hash_password
from mghands_gateway.config import Settings
from mghands_gateway.models import UserRecord, UserRole, SessionStatus, SandboxLeaseKind, utc_now
from mghands_gateway.sandbox_backend import SandboxHandle
from mghands_gateway.session_store import SessionStore


class FakeSandboxBackend:
    def __init__(self):
        self.ensure_calls = 0
        self.delete_calls = 0
        self.running_containers = set()

    async def create(self, request, workspace_dir=None) -> SandboxHandle:
        return SandboxHandle(
            sandbox_id='sandbox-1',
            sandbox_url='http://127.0.0.1:3000',
            sandbox_api_key='sk-test',
            container_name='container-1',
            workspace_dir=str(workspace_dir or 'workspace-1'),
        )

    async def ensure_user_sandbox(self, **kwargs) -> SandboxHandle:
        self.ensure_calls += 1
        self.running_containers.add(kwargs['container_name'])
        return SandboxHandle(
            sandbox_id=kwargs['container_name'],
            sandbox_url='http://127.0.0.1:3000',
            sandbox_api_key=kwargs['session_api_key'],
            container_name=kwargs['container_name'],
            workspace_dir=str(kwargs['user_root']),
        )

    async def delete(self, container_name) -> None:
        self.delete_calls += 1
        self.running_containers.discard(container_name)

    def _container_running(self, container_name) -> bool:
        return container_name in self.running_containers

    async def _sandbox_url(self, container_name) -> str:
        return f'http://{container_name}:3000'

    def list_user_containers(self) -> list[str]:
        return list(self.running_containers)


class FakeAgentClient:
    def __init__(self):
        self.started = []
        self.send_calls = []

    async def start_conversation(self, *args, **kwargs):
        self.started.append((args, kwargs))
        return {'id': kwargs.get('conversation_id') or 'fake-conv-id', 'status': 'ready'}

    async def search_events(self, *args, **kwargs):
        return {'items': []}

    async def delete_conversation(self, *args, **kwargs):
        return None

    async def alive(self, base_url, session_api_key) -> bool:
        return True

    async def send_message(self, base_url, session_api_key, conversation_id, task):
        self.send_calls.append((base_url, session_api_key, conversation_id, task))
        return {}


def _settings(tmp_path) -> Settings:
    return Settings(
        data_root=tmp_path / 'data',
        database_path=tmp_path / 'sessions.sqlite3',
        shared_skills_root=tmp_path / 'shared_skills',
    )


def _user_settings(tmp_path) -> Settings:
    return Settings(
        data_root=tmp_path / 'data',
        database_path=tmp_path / 'sessions.sqlite3',
        shared_skills_root=tmp_path / 'shared_skills',
        sandbox_scope='user',
        gateway_active_secret_key_id='test-v1',
        gateway_secret_keys={'test-v1': Fernet.generate_key().decode('ascii')},
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


def _zip(entries: dict[str, str]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


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


def test_upload_project_skill_is_project_local_and_listed(tmp_path) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    app.dependency_overrides[get_store] = lambda: _store(tmp_path)
    app.dependency_overrides[get_sandbox_backend] = lambda: FakeSandboxBackend()
    try:
        client = TestClient(app)
        headers = _auth_headers(tmp_path, client)
        project = client.post('/api/v1/projects', json={'name': 'Demo'}, headers=headers)
        assert project.status_code == 201
        project_id = project.json()['project_id']

        response = client.post(
            f'/api/v1/projects/{project_id}/skills/upload',
            data={'skill_name': 'uploaded-skill'},
            files={'file': ('uploaded.zip', _zip({'SKILL.md': 'Use upload.'}), 'application/zip')},
            headers=headers,
        )

        assert response.status_code == 201
        body = response.json()
        assert body['skill_name'] == 'uploaded-skill'
        assert body['metadata']['source_type'] == 'uploaded'
        assert body['metadata']['source_name'] == 'uploaded.zip'
        listed = client.get(f'/api/v1/projects/{project_id}/skills', headers=headers)
        assert listed.status_code == 200
        assert [item['skill_name'] for item in listed.json()] == ['uploaded-skill']
        catalog = client.get('/api/v1/skills/catalog', headers=headers)
        assert catalog.status_code == 200
        assert catalog.json()['items'] == []
    finally:
        app.dependency_overrides.clear()


def test_upload_project_skill_rejects_invalid_archive(tmp_path) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    app.dependency_overrides[get_store] = lambda: _store(tmp_path)
    app.dependency_overrides[get_sandbox_backend] = lambda: FakeSandboxBackend()
    try:
        client = TestClient(app)
        headers = _auth_headers(tmp_path, client)
        project = client.post('/api/v1/projects', json={'name': 'Demo'}, headers=headers)
        assert project.status_code == 201
        project_id = project.json()['project_id']

        response = client.post(
            f'/api/v1/projects/{project_id}/skills/upload',
            data={'skill_name': 'uploaded-skill'},
            files={'file': ('uploaded.zip', b'not a zip', 'application/zip')},
            headers=headers,
        )

        assert response.status_code == 400
        assert 'valid zip' in response.json()['detail']
    finally:
        app.dependency_overrides.clear()


def test_user_scoped_sandbox_is_lazy_shared_and_not_deleted_with_session(tmp_path) -> None:
    settings = _user_settings(tmp_path)
    backend = FakeSandboxBackend()
    agent = FakeAgentClient()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_store] = lambda: _store(tmp_path)
    app.dependency_overrides[get_sandbox_backend] = lambda: backend
    app.dependency_overrides[get_agent_client] = lambda: agent
    try:
        client = TestClient(app)
        headers = _auth_headers(tmp_path, client)
        first_project = client.post('/api/v1/projects', json={'name': 'One'}, headers=headers).json()
        second_project = client.post('/api/v1/projects', json={'name': 'Two'}, headers=headers).json()
        first = client.post(
            f"/api/v1/projects/{first_project['project_id']}/sessions",
            json={'session_id': 'user-session-a'},
            headers=headers,
        )
        second = client.post(
            f"/api/v1/projects/{second_project['project_id']}/sessions",
            json={'session_id': 'user-session-b'},
            headers=headers,
        )
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()['sandbox_scope'] == 'user'
        assert backend.ensure_calls == 0

        executed = client.post(
            '/api/v1/sessions/user-session-a/execute',
            json={'task': 'write a file'},
            headers=headers,
        )
        assert executed.status_code == 200
        assert backend.ensure_calls == 1
        assert agent.started[0][1]['working_dir'].replace('\\', '/').endswith(
            "projects/One/workspace"
        )
        assert agent.started[0][1]['persistence_dir'] == '/userspace/.mghands/conversations'

        busy = client.post(
            '/api/v1/sessions/user-session-b/execute',
            json={'task': 'run concurrently'},
            headers=headers,
        )
        assert busy.status_code == 409

        deleted = client.delete('/api/v1/sessions/user-session-a', headers=headers)
        assert deleted.status_code == 200
        assert backend.delete_calls == 0
    finally:
        app.dependency_overrides.clear()


def test_gateway_confirm_recovery(tmp_path) -> None:
    settings = _user_settings(tmp_path)
    backend = FakeSandboxBackend()
    agent = FakeAgentClient()
    store = _store(tmp_path)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_sandbox_backend] = lambda: backend
    app.dependency_overrides[get_agent_client] = lambda: agent
    try:
        client = TestClient(app)
        headers = _auth_headers(tmp_path, client)
        project = client.post('/api/v1/projects', json={'name': 'One'}, headers=headers).json()
        client.post(
            f"/api/v1/projects/{project['project_id']}/sessions",
            json={'session_id': 'sess-confirm'},
            headers=headers,
        )
        
        sess_record = asyncio.run(store.get('sess-confirm'))
        sess_record.status = SessionStatus.INTERRUPTED
        sess_record.conversation_id = 'stable-conv-id'
        sess_record.sandbox_id = 'mghands-user-usr_a'
        asyncio.run(store.save(sess_record))
        
        resp_reject = client.post(
            '/api/v1/sessions/sess-confirm/confirm',
            json={'confirm': False},
            headers=headers,
        )
        assert resp_reject.status_code == 200
        assert resp_reject.json()['status'] == 'error'
        
        sess_record = asyncio.run(store.get('sess-confirm'))
        sess_record.status = SessionStatus.INTERRUPTED
        asyncio.run(store.save(sess_record))
        
        agent.send_calls = []
        resp_confirm = client.post(
            '/api/v1/sessions/sess-confirm/confirm',
            json={'confirm': True},
            headers=headers,
        )
        assert resp_confirm.status_code == 200
        assert resp_confirm.json()['status'] == 'running'
        assert agent.send_calls[0][3] == ""
    finally:
        app.dependency_overrides.clear()


def test_reconciler_cleanup_and_recovery(tmp_path) -> None:
    settings = _user_settings(tmp_path)
    backend = FakeSandboxBackend()
    agent = FakeAgentClient()
    store = _store(tmp_path)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_sandbox_backend] = lambda: backend
    app.dependency_overrides[get_agent_client] = lambda: agent
    try:
        client = TestClient(app)
        headers = _auth_headers(tmp_path, client)
        
        from mghands_gateway.models import UserSandboxRecord, UserSandboxStatus
        from mghands_gateway.secret_store import SecretCipher
        cipher = SecretCipher(settings)
        ciphertext, key_id = cipher.encrypt('sk-sandbox-key')
        
        users = asyncio.run(store.list_users())
        user_id = users[0].user_id
        
        import datetime
        sandbox_rec = UserSandboxRecord(
            user_id=user_id,
            sandbox_id=f'mghands-user-{user_id}',
            container_name=f'mghands-user-{user_id}',
            api_key_ciphertext=ciphertext,
            api_key_key_id=key_id,
            generation=1,
            image_ref='sandbox:test',
            status=UserSandboxStatus.READY,
            last_activity_at=utc_now() - datetime.timedelta(seconds=5000),
            idle_expires_at=utc_now() - datetime.timedelta(seconds=1),
        )
        asyncio.run(store.begin_user_sandbox_generation(sandbox_rec))
        
        backend.running_containers = {f'mghands-user-{user_id}'}
        backend.delete_calls = 0
        
        from mghands_gateway.app import _reconcile_sandboxes
        asyncio.run(_reconcile_sandboxes(settings, store, backend, agent))
        
        sandbox_after = asyncio.run(store.get_user_sandbox(user_id))
        assert sandbox_after.status == UserSandboxStatus.DELETED
        assert backend.delete_calls == 1

        project = client.post('/api/v1/projects', json={'name': 'One'}, headers=headers).json()
        client.post(
            f"/api/v1/projects/{project['project_id']}/sessions",
            json={'session_id': 'sess-reconcile-interrupt'},
            headers=headers,
        )
        sess_record = asyncio.run(store.get('sess-reconcile-interrupt'))
        sess_record.status = SessionStatus.RUNNING
        sess_record.conversation_id = 'stable-conv-id'
        sess_record.sandbox_id = f'mghands-user-{user_id}'
        asyncio.run(store.save(sess_record))
        
        asyncio.run(store.acquire_user_sandbox_lease(user_id, SandboxLeaseKind.EXECUTION, 'sess-reconcile-interrupt', 60))
        
        sandbox_rec.status = UserSandboxStatus.READY
        sandbox_rec.idle_expires_at = utc_now() + datetime.timedelta(seconds=3600)
        asyncio.run(store.save_user_sandbox(sandbox_rec))
        backend.running_containers = set()
        
        asyncio.run(_reconcile_sandboxes(settings, store, backend, agent))
        
        sess_after = asyncio.run(store.get('sess-reconcile-interrupt'))
        assert sess_after.status == SessionStatus.INTERRUPTED
        assert not asyncio.run(store.has_user_sandbox_lease(user_id, SandboxLeaseKind.EXECUTION))
    finally:
        app.dependency_overrides.clear()


def test_project_deletion_safeguards(tmp_path) -> None:
    settings = _user_settings(tmp_path)
    backend = FakeSandboxBackend()
    agent = FakeAgentClient()
    store = _store(tmp_path)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_sandbox_backend] = lambda: backend
    app.dependency_overrides[get_agent_client] = lambda: agent
    try:
        client = TestClient(app)
        headers = _auth_headers(tmp_path, client)
        project = client.post('/api/v1/projects', json={'name': 'One'}, headers=headers).json()
        project_id = project['project_id']
        
        client.post(
            f"/api/v1/projects/{project_id}/sessions",
            json={'session_id': 'sess-deleted-project'},
            headers=headers,
        )
        
        sess_record = asyncio.run(store.get('sess-deleted-project'))
        sess_record.status = SessionStatus.RUNNING
        sess_record.conversation_id = 'stable-conv-id'
        sess_record.sandbox_id = 'mghands-user-usr_a'
        asyncio.run(store.save(sess_record))
        
        resp = client.delete(f'/api/v1/projects/{project_id}', headers=headers)
        assert resp.status_code == 200
        
        sess_after = asyncio.run(store.get('sess-deleted-project'))
        assert sess_after.status == SessionStatus.DELETED
        
        list_resp = client.get(f'/api/v1/projects/{project_id}/files', headers=headers)
        assert list_resp.status_code == 404
        
        read_resp = client.get(f'/api/v1/projects/{project_id}/files/read?path=dummy', headers=headers)
        assert read_resp.status_code == 404
    finally:
        app.dependency_overrides.clear()
