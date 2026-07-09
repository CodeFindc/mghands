import asyncio
from fastapi.testclient import TestClient
from mghands_gateway.app import app, get_sandbox_backend, get_settings, get_store, get_agent_client
from mghands_gateway.auth import hash_password
from mghands_gateway.config import Settings
from mghands_gateway.models import UserRecord, UserRole, SessionStatus
from mghands_gateway.sandbox_backend import SandboxHandle
from mghands_gateway.session_store import SessionStore
import pytest

class FakeSandboxBackend:
    async def create(self, request, workspace_dir=None) -> SandboxHandle:
        return SandboxHandle(
            sandbox_id='sandbox-1',
            sandbox_url='http://127.0.0.1:3000',
            sandbox_api_key='sk-test',
            container_name='container-1',
            workspace_dir=str(workspace_dir or 'workspace-1'),
        )

class FakeAgentClient:
    def __init__(self):
        self.events = [
            {'id': '1', 'kind': 'openhands.MessageEvent', 'data': {'text': 'hello'}},
        ]
        self.started = False

    async def _add_events_later(self):
        await asyncio.sleep(0.3)
        self.events.append({'id': '2', 'kind': 'openhands.ActionEvent', 'data': {'action': 'run'}})
        await asyncio.sleep(0.3)
        self.events.append({'id': '3', 'kind': 'agent.result', 'data': {'result': 'success'}})

    async def start_conversation(self, *args, **kwargs):
        return {'conversation_id': 'conv-123', 'id': 'conv-123'}

    async def search_events(self, base_url, session_api_key, conversation_id, page_id=None, limit=100):
        if not self.started:
            self.started = True
            asyncio.create_task(self._add_events_later())
        offset = int(page_id) if page_id else 0
        items = self.events[offset:offset+limit]
        return {'items': items, 'next_page_id': None}

def _settings(tmp_path) -> Settings:
    return Settings(
        data_root=tmp_path / 'data',
        database_path=tmp_path / 'sessions.sqlite3',
        shared_skills_root=tmp_path / 'shared_skills',
        sse_poll_seconds=0.1,
        sse_heartbeat_seconds=0.5,
        sse_idle_timeout_seconds=2.0,
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

def test_stream_endpoint(tmp_path) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    app.dependency_overrides[get_store] = lambda: _store(tmp_path)
    app.dependency_overrides[get_sandbox_backend] = lambda: FakeSandboxBackend()
    fake_client = FakeAgentClient()
    app.dependency_overrides[get_agent_client] = lambda: fake_client
    try:
        client = TestClient(app)
        headers = _auth_headers(tmp_path, client)
        
        # 1. Create Session
        resp = client.post(
            '/api/v1/sessions',
            json={'session_id': 'sess-1', 'sandbox_type': 'docker'},
            headers=headers,
        )
        assert resp.status_code == 201
        
        # 2. Execute a task to trigger conversation creation
        resp_exec = client.post(
            '/api/v1/sessions/sess-1/execute',
            json={'task': 'test task', 'skills': []},
            headers=headers,
        )
        assert resp_exec.status_code == 200
        assert resp_exec.json()['conversation_id'] == 'conv-123'
        
        # 3. Request stream and read lines
        with client.stream('GET', '/api/v1/sessions/sess-1/stream', headers=headers) as response:
            assert response.status_code == 200
            lines = []
            for line in response.iter_lines():
                if line:
                    lines.append(line)
                if len(lines) >= 10:  # Break to avoid infinite loop
                    break
            print("Received lines:", lines)
            assert len(lines) > 0
            # Check if all dynamically added events were streamed
            event_ids = [line for line in lines if line.startswith('id:')]
            assert 'id: 1' in event_ids
            assert 'id: 2' in event_ids
            assert 'id: 3' in event_ids
    finally:
        app.dependency_overrides.clear()
