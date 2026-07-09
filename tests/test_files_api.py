import asyncio
from pathlib import Path
from fastapi.testclient import TestClient

from mghands_gateway.app import app, get_settings, get_store
from mghands_gateway.auth import hash_password
from mghands_gateway.config import Settings
from mghands_gateway.models import UserRecord, UserRole
from mghands_gateway.session_store import SessionStore


def _settings(tmp_path) -> Settings:
    return Settings(
        data_root=tmp_path / 'data',
        database_path=tmp_path / 'sessions.sqlite3',
    )


def _store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / 'sessions.sqlite3')


def _auth_headers(tmp_path, client: TestClient, username='admin', role=UserRole.ADMIN) -> dict[str, str]:
    store = _store(tmp_path)
    asyncio.run(
        store.create_user(
            UserRecord(
                username=username,
                password_hash=hash_password('password123'),
                role=role,
            )
        )
    )
    response = client.post(
        '/api/v1/auth/login',
        json={'username': username, 'password': 'password123'},
    )
    assert response.status_code == 200
    return {'Authorization': f"Bearer {response.json()['access_token']}"}


def test_files_api_crud_and_traversal_check(tmp_path) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    app.dependency_overrides[get_store] = lambda: _store(tmp_path)
    try:
        client = TestClient(app)
        headers = _auth_headers(tmp_path, client, username='user1', role=UserRole.USER)

        # Create project
        resp = client.post('/api/v1/projects', json={'name': 'Demo Proj'}, headers=headers)
        assert resp.status_code == 201
        project_id = resp.json()['project_id']

        # Get workspace directory (same formula as _project_workspace)
        # data_root / 'users' / user_id / 'projects' / project_id / 'workspace'
        store = _store(tmp_path)
        db_user = asyncio.run(store.get_user_by_username('user1'))
        assert db_user is not None
        user_id = db_user.user_id

        workspace_dir = tmp_path / 'data' / 'users' / user_id / 'projects' / project_id / 'workspace'
        workspace_dir.mkdir(parents=True, exist_ok=True)

        # Write dummy files
        (workspace_dir / 'main.py').write_text("print('hello')", encoding='utf-8')
        (workspace_dir / 'subdir').mkdir()
        (workspace_dir / 'subdir' / 'config.json').write_text('{"debug": true}', encoding='utf-8')
        # Ignored files
        (workspace_dir / '.git').mkdir()
        (workspace_dir / '.git' / 'config').write_text('git info', encoding='utf-8')

        # List files
        resp = client.get(f'/api/v1/projects/{project_id}/files', headers=headers)
        assert resp.status_code == 200
        files = resp.json()

        # Should contain main.py and subdir/config.json, but NOT .git/config
        paths = [f['path'] for f in files]
        assert 'main.py' in paths
        assert 'subdir/config.json' in paths
        assert 'subdir' in paths
        assert '.git/config' not in paths

        # Read main.py
        resp = client.get(f'/api/v1/projects/{project_id}/files/read', params={'path': 'main.py'}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()['content'] == "print('hello')"

        # Test Path Traversal security check
        resp = client.get(f'/api/v1/projects/{project_id}/files/read', params={'path': '../../../../etc/passwd'}, headers=headers)
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.clear()
