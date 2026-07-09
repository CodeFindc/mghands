import asyncio
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


def test_admin_settings_requires_admin_role(tmp_path) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    app.dependency_overrides[get_store] = lambda: _store(tmp_path)
    try:
        client = TestClient(app)
        user_headers = _auth_headers(tmp_path, client, username='user1', role=UserRole.USER)
        admin_headers = _auth_headers(tmp_path, client, username='admin1', role=UserRole.ADMIN)

        # Non-admin access should be forbidden/unauthorized
        resp = client.get('/api/v1/admin/settings', headers=user_headers)
        assert resp.status_code == 403

        # Admin access should succeed and be empty initially
        resp = client.get('/api/v1/admin/settings', headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == {}

        # Save overrides
        resp = client.post(
            '/api/v1/admin/settings',
            json={'sandbox_image': 'custom-image:1.0', 'sandbox_memory_limit': '1g'},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['sandbox_image'] == 'custom-image:1.0'
        assert resp.json()['sandbox_memory_limit'] == '1g'

        # Get settings again
        resp = client.get('/api/v1/admin/settings', headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()['sandbox_image'] == 'custom-image:1.0'
    finally:
        app.dependency_overrides.clear()


def test_admin_models_crud(tmp_path) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(tmp_path)
    app.dependency_overrides[get_store] = lambda: _store(tmp_path)
    try:
        client = TestClient(app)
        admin_headers = _auth_headers(tmp_path, client, username='admin2', role=UserRole.ADMIN)

        # List initially empty
        resp = client.get('/api/v1/admin/models', headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == []

        # Create model config
        resp = client.post(
            '/api/v1/admin/models',
            json={
                'name': 'Ollama-Llama3',
                'provider': 'ollama',
                'model': 'llama3',
                'base_url': 'http://127.0.0.1:11434',
                'api_key': 'super-secret-key',
                'is_default': True,
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data['name'] == 'Ollama-Llama3'
        assert data['api_key'] == '**********'  # Redacted in response
        assert data['is_default'] is True
        model_id = data['model_id']

        # Update model name
        resp = client.patch(
            f'/api/v1/admin/models/{model_id}',
            json={'name': 'Ollama-Llama3-New'},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['name'] == 'Ollama-Llama3-New'

        # Delete model config
        resp = client.delete(f'/api/v1/admin/models/{model_id}', headers=admin_headers)
        assert resp.status_code == 200

        # List models should be empty again
        resp = client.get('/api/v1/admin/models', headers=admin_headers)
        assert resp.json() == []
    finally:
        app.dependency_overrides.clear()
