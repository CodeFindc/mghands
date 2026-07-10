import asyncio
import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4
from pathlib import Path
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from mghands_gateway.auth import (
    generate_access_token,
    hash_password,
    hash_token,
    token_expiry,
    verify_password,
)
from mghands_gateway.config import Settings, get_settings
from mghands_gateway.agent_client import AgentServerClient
from mghands_gateway.models import (
    AuthTokenRecord,
    CreateProjectRequest,
    CreateProjectSessionRequest,
    CreateSessionRequest,
    CreateUserRequest,
    ConfirmRecoveryRequest,
    ExecuteRequest,
    ExecuteResponse,
    HistoryResponse,
    InstallProjectSkillRequest,
    LoginRequest,
    LoginResponse,
    ProjectRecord,
    ProjectResponse,
    ProjectSkillRecord,
    ProjectSkillResponse,
    ProjectStatus,
    RegisterRequest,
    ResetPasswordRequest,
    RuntimeUpdateRequest,
    SessionRecord,
    SessionResponse,
    SessionStatus,
    SandboxType,
    SandboxScope,
    SandboxLeaseKind,
    UserSandboxRecord,
    UserSandboxResponse,
    UserSandboxStatus,
    TerminalEvent,
    UpdateUserRequest,
    UserRecord,
    UserResponse,
    UserRole,
    new_id,
    utc_now,
    validate_session_id,
    LLMModelRecord,
    LLMModelResponse,
    CreateLLMModelRequest,
    UpdateLLMModelRequest,
)
from mghands_gateway.sandbox_backend import DockerSandboxBackend
from mghands_gateway.session_store import SessionStore
from mghands_gateway.secret_store import SecretCipher
from mghands_gateway.skills import MAX_ZIP_UPLOAD_BYTES, SkillManager


def get_store(settings: Annotated[Settings, Depends(get_settings)]) -> SessionStore:
    return SessionStore(settings.database_path)


def get_agent_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AgentServerClient:
    return AgentServerClient(settings)


def get_sandbox_backend(
    settings: Annotated[Settings, Depends(get_settings)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
) -> DockerSandboxBackend:
    return DockerSandboxBackend(settings, agent_client)


def get_skill_manager(settings: Annotated[Settings, Depends(get_settings)]) -> SkillManager:
    return SkillManager(settings.shared_skills_root or settings.data_root / 'shared_skills', settings.sandbox_workspace_mount_path)


@dataclass(frozen=True)
class ResolvedSandbox:
    sandbox_id: str
    sandbox_url: str
    sandbox_api_key: str | None
    container_name: str
    generation: int | None
    working_dir: str
    persistence_dir: str | None = None


async def _require_auth_with_token(
    store: Annotated[SessionStore, Depends(get_store)],
    authorization: Annotated[str | None, Header()] = None,
) -> tuple[UserRecord, AuthTokenRecord]:
    if not authorization or not authorization.lower().startswith('bearer '):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'missing bearer token')
    token_value = authorization.split(' ', 1)[1].strip()
    token = await store.get_token_by_hash(hash_token(token_value))
    if token is None or token.revoked_at is not None or token.expires_at <= utc_now():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'invalid or expired token')
    user = await store.get_user(token.user_id)
    if user is None or not user.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'invalid or disabled user')
    await store.touch_token(token.token_id)
    return user, token


async def _require_auth(
    auth: Annotated[tuple[UserRecord, AuthTokenRecord], Depends(_require_auth_with_token)],
) -> UserRecord:
    user, _token = auth
    return user


async def _require_admin(
    current_user: Annotated[UserRecord, Depends(_require_auth)],
) -> UserRecord:
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'admin role required')
    return current_user


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    store = SessionStore(settings.database_path)
    await store.init()
    await _bootstrap_admin(settings, store)
    reconciler_task = asyncio.create_task(_reconcile_loop(settings, store))
    try:
        yield
    finally:
        reconciler_task.cancel()
        try:
            await reconciler_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title='Mghands OpenHands Gateway',
    version='0.1.0',
    lifespan=lifespan,
)


@app.get('/health')
async def health() -> dict[str, str]:
    return {'status': 'ok'}


@app.post('/api/v1/auth/register', response_model=UserResponse, status_code=201)
async def register(
    request: RegisterRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> UserResponse:
    if not settings.auth_public_registration_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'public registration is disabled')
    user = UserRecord(
        username=request.username,
        password_hash=hash_password(request.password),
        role=UserRole.USER,
        enabled=settings.auth_registered_user_default_enabled,
    )
    try:
        return UserResponse.from_record(await store.create_user(user))
    except KeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, 'username already exists') from exc


@app.post('/api/v1/auth/login', response_model=LoginResponse)
async def login(
    request: LoginRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> LoginResponse:
    user = await store.get_user_by_username(request.username)
    if user is None or not user.enabled or not verify_password(request.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'invalid username or password')
    access_token = generate_access_token()
    expires_at = token_expiry(settings.auth_access_token_ttl_seconds)
    await store.create_token(
        AuthTokenRecord(
            user_id=user.user_id,
            token_hash=hash_token(access_token),
            expires_at=expires_at,
        )
    )
    return LoginResponse(access_token=access_token, expires_at=expires_at)


@app.post('/api/v1/auth/logout')
async def logout(
    auth: Annotated[tuple[UserRecord, AuthTokenRecord], Depends(_require_auth_with_token)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> dict[str, str]:
    _user, token = auth
    await store.revoke_token(token.token_id)
    return {'status': 'ok'}


@app.get('/api/v1/me', response_model=UserResponse)
async def me(current_user: Annotated[UserRecord, Depends(_require_auth)]) -> UserResponse:
    return UserResponse.from_record(current_user)


@app.get('/api/v1/admin/users', response_model=list[UserResponse])
async def list_users(
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> list[UserResponse]:
    return [UserResponse.from_record(user) for user in await store.list_users()]


@app.post('/api/v1/admin/users', response_model=UserResponse, status_code=201)
async def create_user(
    request: CreateUserRequest,
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> UserResponse:
    user = UserRecord(
        username=request.username,
        password_hash=hash_password(request.password),
        role=request.role,
        enabled=request.enabled,
    )
    try:
        return UserResponse.from_record(await store.create_user(user))
    except KeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, 'username already exists') from exc


@app.patch('/api/v1/admin/users/{user_id}', response_model=UserResponse)
async def update_user(
    user_id: str,
    request: UpdateUserRequest,
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> UserResponse:
    user = await store.get_user(user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'user not found')
    if request.enabled is not None:
        user.enabled = request.enabled
    if request.role is not None:
        user.role = request.role
    if 'sandbox_scope' in request.model_fields_set:
        user.sandbox_scope = request.sandbox_scope
    return UserResponse.from_record(await store.update_user(user))


@app.get('/api/v1/admin/user-sandboxes', response_model=list[UserSandboxResponse])
async def list_user_sandboxes(
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> list[UserSandboxResponse]:
    return [UserSandboxResponse.from_record(item) for item in await store.list_user_sandboxes()]


@app.delete('/api/v1/admin/users/{user_id}/sandbox', response_model=UserSandboxResponse)
async def recycle_user_sandbox(
    user_id: str,
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    store: Annotated[SessionStore, Depends(get_store)],
    sandbox_backend: Annotated[DockerSandboxBackend, Depends(get_sandbox_backend)],
) -> UserSandboxResponse:
    record = await store.get_user_sandbox(user_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'user sandbox not found')
    if await store.has_user_sandbox_lease(user_id, SandboxLeaseKind.EXECUTION):
        raise HTTPException(status.HTTP_409_CONFLICT, 'user sandbox is busy')
    record.status = UserSandboxStatus.DELETING
    await store.save_user_sandbox(record)
    await sandbox_backend.delete(record.container_name)
    record.status = UserSandboxStatus.DELETED
    record.sandbox_url = None
    return UserSandboxResponse.from_record(await store.save_user_sandbox(record))


@app.post('/api/v1/admin/users/{user_id}/password', response_model=UserResponse)
async def reset_password(
    user_id: str,
    request: ResetPasswordRequest,
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> UserResponse:
    user = await store.get_user(user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'user not found')
    user.password_hash = hash_password(request.password)
    return UserResponse.from_record(await store.update_user(user))


@app.get('/api/v1/admin/settings', response_model=dict[str, str])
async def get_admin_settings(
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> dict[str, str]:
    return await store.get_all_settings()


@app.post('/api/v1/admin/settings', response_model=dict[str, str])
async def update_admin_settings(
    request: dict[str, str],
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> dict[str, str]:
    for key, value in request.items():
        await store.set_setting(key, value)
    return await store.get_all_settings()


@app.get('/api/v1/admin/models', response_model=list[LLMModelResponse])
async def list_admin_models(
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> list[LLMModelResponse]:
    records = await store.list_models()
    return [LLMModelResponse.from_record(r) for r in records]


@app.post('/api/v1/admin/models', response_model=LLMModelResponse, status_code=201)
async def create_admin_model(
    request: CreateLLMModelRequest,
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> LLMModelResponse:
    record = LLMModelRecord(
        name=request.name,
        provider=request.provider,
        model=request.model,
        base_url=request.base_url,
        api_key=request.api_key,
        is_default=request.is_default,
    )
    for m in await store.list_models():
        if m.name == request.name:
            raise HTTPException(status.HTTP_409_CONFLICT, 'Model name already exists')
    created = await store.create_model(record)
    return LLMModelResponse.from_record(created)


@app.patch('/api/v1/admin/models/{model_id}', response_model=LLMModelResponse)
async def update_admin_model(
    model_id: str,
    request: UpdateLLMModelRequest,
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> LLMModelResponse:
    record = await store.get_model(model_id)
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Model not found')
    if request.name is not None:
        for m in await store.list_models():
            if m.name == request.name and m.model_id != model_id:
                raise HTTPException(status.HTTP_409_CONFLICT, 'Model name already exists')
        record.name = request.name
    if request.provider is not None:
        record.provider = request.provider
    if request.model is not None:
        record.model = request.model
    if request.base_url is not None:
        record.base_url = request.base_url
    if request.api_key is not None:
        record.api_key = request.api_key
    if request.is_default is not None:
        record.is_default = request.is_default

    updated = await store.update_model(record)
    return LLMModelResponse.from_record(updated)


@app.delete('/api/v1/admin/models/{model_id}')
async def delete_admin_model(
    model_id: str,
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> dict[str, str]:
    record = await store.get_model(model_id)
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Model not found')
    await store.delete_model(model_id)
    return {'status': 'ok'}


@app.get('/api/v1/admin/skills', response_model=list[dict[str, Any]])
async def list_admin_skills(
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    manager: Annotated[SkillManager, Depends(get_skill_manager)],
) -> list[dict[str, Any]]:
    return [item.model_dump(mode='json') for item in manager.catalog()]


@app.post('/api/v1/admin/skills/upload', status_code=201)
async def upload_admin_skill(
    skill_name: Annotated[str, Form(min_length=1, max_length=100)],
    file: UploadFile,
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    manager: Annotated[SkillManager, Depends(get_skill_manager)],
) -> dict[str, str]:
    content = await file.read()
    try:
        manager.upload_shared_zip(skill_name, content)
        return {'status': 'ok'}
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@app.delete('/api/v1/admin/skills/{skill_name}')
async def delete_admin_skill(
    skill_name: str,
    _admin: Annotated[UserRecord, Depends(_require_admin)],
    manager: Annotated[SkillManager, Depends(get_skill_manager)],
) -> dict[str, str]:
    try:
        manager.delete_shared(skill_name)
        return {'status': 'ok'}
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@app.get('/api/v1/skills/catalog')
async def skill_catalog(
    _user: Annotated[UserRecord, Depends(_require_auth)],
    manager: Annotated[SkillManager, Depends(get_skill_manager)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    return {
        'default_project_skills': settings.default_project_skills,
        'items': [item.model_dump(mode='json') for item in manager.catalog()],
    }


@app.post('/api/v1/projects', response_model=ProjectResponse, status_code=201)
async def create_project(
    request: CreateProjectRequest,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[SessionStore, Depends(get_store)],
    manager: Annotated[SkillManager, Depends(get_skill_manager)],
) -> ProjectResponse:
    project = await _create_project_record(store, settings, current_user.user_id, request.name)
    for skill_name in request.skill_names:
        installed = _install_skill_or_http(manager, skill_name, project)
        await store.upsert_project_skill(installed)
    return ProjectResponse.from_record(project)


@app.get('/api/v1/projects', response_model=list[ProjectResponse])
async def list_projects(
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> list[ProjectResponse]:
    return [ProjectResponse.from_record(project) for project in await store.list_projects(current_user.user_id)]


@app.get('/api/v1/projects/{project_id}', response_model=ProjectResponse)
async def get_project(
    project_id: str,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> ProjectResponse:
    project = await _get_project_or_404(store, project_id, current_user)
    return ProjectResponse.from_record(project)


@app.delete('/api/v1/projects/{project_id}', response_model=ProjectResponse)
async def delete_project(
    project_id: str,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    sandbox_backend: Annotated[DockerSandboxBackend, Depends(get_sandbox_backend)],
) -> ProjectResponse:
    project = await _get_project_or_404(store, project_id, current_user)
    
    sessions = await store.list_sessions_for_project(project_id)
    for sess in sessions:
        if sess.status in {SessionStatus.CREATED, SessionStatus.QUEUED, SessionStatus.RUNNING, SessionStatus.RECOVERING, SessionStatus.INTERRUPTED}:
            try:
                if sess.conversation_id:
                    resolved = await _resolve_sandbox_for_record(
                        sess, settings, store, sandbox_backend
                    )
                    try:
                        await agent_client.delete_conversation(
                            resolved.sandbox_url,
                            resolved.sandbox_api_key,
                            sess.conversation_id,
                        )
                    except Exception:
                        pass
                if sess.sandbox_scope == SandboxScope.SESSION:
                    await sandbox_backend.delete(sess.container_name)
            except Exception:
                pass
            sess.status = SessionStatus.DELETED
            if sess.sandbox_scope == SandboxScope.USER and sess.created_by_user_id:
                await store.release_user_sandbox_lease(
                    sess.created_by_user_id, SandboxLeaseKind.EXECUTION
                )
                await store.delete_session_secret(sess.session_id)
            await store.save(sess)

    project.status = ProjectStatus.DELETED
    return ProjectResponse.from_record(await store.save_project(project))


@app.get('/api/v1/projects/{project_id}/files')
async def list_project_files(
    project_id: str,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, Any]]:
    project = await _get_project_or_404(store, project_id, current_user)
    workspace_dir = _project_workspace(settings, project.user_id, project_id)
    if not workspace_dir.exists():
        return []

    from datetime import datetime, timezone
    files = []
    ignored_dirs = {'.git', 'node_modules', '.mghands', '__pycache__', '.pytest_cache'}
    for p in workspace_dir.rglob('*'):
        rel_parts = p.relative_to(workspace_dir).parts
        if any(part in ignored_dirs for part in rel_parts):
            continue
        try:
            stat = p.stat()
            is_dir = p.is_dir()
            files.append({
                'path': '/'.join(rel_parts),
                'is_dir': is_dir,
                'size': stat.st_size if not is_dir else 0,
                'updated_at': datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            })
        except Exception:
            pass
    files.sort(key=lambda x: x['path'])
    return files


@app.get('/api/v1/projects/{project_id}/files/read')
async def read_project_file(
    project_id: str,
    path: str,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    project = await _get_project_or_404(store, project_id, current_user)

    workspace_dir = _project_workspace(settings, project.user_id, project_id)
    safe_path = (workspace_dir / path).resolve()
    if workspace_dir != safe_path and workspace_dir not in safe_path.parents:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'access denied')

    if not safe_path.exists() or safe_path.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'file not found')

    if safe_path.stat().st_size > 5 * 1024 * 1024:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'file is too large to read (max 5MB)')

    try:
        content = safe_path.read_text(encoding='utf-8', errors='replace')
        return {
            'path': path,
            'content': content,
        }
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f'could not read file: {exc}') from exc



@app.post('/api/v1/projects/{project_id}/sessions', response_model=SessionResponse, status_code=201)
async def create_project_session(
    project_id: str,
    request: CreateProjectSessionRequest,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[SessionStore, Depends(get_store)],
    sandbox_backend: Annotated[DockerSandboxBackend, Depends(get_sandbox_backend)],
) -> SessionResponse:
    project = await _get_project_or_404(store, project_id, current_user)
    session_request = CreateSessionRequest(
        session_id=request.session_id,
        project_id=project.project_id,
        sandbox_type=request.sandbox_type,
        workspace_policy=request.workspace_policy,
    )
    return await _create_session_for_project(session_request, project, current_user, settings, store, sandbox_backend)


@app.get('/api/v1/projects/{project_id}/sessions', response_model=list[SessionResponse])
async def list_project_sessions(
    project_id: str,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> list[SessionResponse]:
    project = await _get_project_or_404(store, project_id, current_user)
    records = await store.list_sessions_for_project(project.project_id)
    return [SessionResponse.from_record(r) for r in records if r.status != SessionStatus.DELETED]


@app.get('/api/v1/projects/{project_id}/skills', response_model=list[ProjectSkillResponse])
async def list_project_skills(
    project_id: str,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> list[ProjectSkillResponse]:
    await _get_project_or_404(store, project_id, current_user)
    return [ProjectSkillResponse.from_record(record) for record in await store.list_project_skills(project_id)]


@app.post('/api/v1/projects/{project_id}/skills/install', response_model=ProjectSkillResponse, status_code=201)
async def install_project_skill(
    project_id: str,
    request: InstallProjectSkillRequest,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    manager: Annotated[SkillManager, Depends(get_skill_manager)],
) -> ProjectSkillResponse:
    project = await _get_project_or_404(store, project_id, current_user)
    record = _install_skill_or_http(manager, request.skill_name, project)
    return ProjectSkillResponse.from_record(await store.upsert_project_skill(record))


@app.post('/api/v1/projects/{project_id}/skills/upload', response_model=ProjectSkillResponse, status_code=201)
async def upload_project_skill(
    project_id: str,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    manager: Annotated[SkillManager, Depends(get_skill_manager)],
    skill_name: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> ProjectSkillResponse:
    project = await _get_project_or_404(store, project_id, current_user)
    content = await file.read(MAX_ZIP_UPLOAD_BYTES + 1)
    try:
        record = manager.upload_zip(skill_name, project.project_id, Path(project.workspace_dir), content, file.filename)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return ProjectSkillResponse.from_record(await store.upsert_project_skill(record))


@app.post('/api/v1/projects/{project_id}/skills/{skill_name}/update', response_model=ProjectSkillResponse)
async def update_project_skill(
    project_id: str,
    skill_name: str,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    manager: Annotated[SkillManager, Depends(get_skill_manager)],
) -> ProjectSkillResponse:
    project = await _get_project_or_404(store, project_id, current_user)
    record = _install_skill_or_http(manager, skill_name, project)
    return ProjectSkillResponse.from_record(await store.upsert_project_skill(record))


@app.post('/api/v1/sessions', response_model=SessionResponse, status_code=201)
async def create_session(
    request: CreateSessionRequest,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[SessionStore, Depends(get_store)],
    sandbox_backend: Annotated[DockerSandboxBackend, Depends(get_sandbox_backend)],
    manager: Annotated[SkillManager, Depends(get_skill_manager)],
) -> SessionResponse:
    if request.project_id:
        project = await _get_project_or_404(store, request.project_id, current_user)
    else:
        project = await _create_project_record(
            store,
            settings,
            current_user.user_id,
            request.project_name or f'Session {request.session_id}',
        )
        for skill_name in request.skill_names:
            installed = _install_skill_or_http(manager, skill_name, project)
            await store.upsert_project_skill(installed)
    return await _create_session_for_project(request, project, current_user, settings, store, sandbox_backend)


@app.get('/api/v1/sessions/{session_id}', response_model=SessionResponse)
async def get_session(
    session_id: str,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> SessionResponse:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id, current_user)
    return SessionResponse.from_record(record)


@app.delete('/api/v1/sessions/{session_id}', response_model=SessionResponse)
async def delete_session(
    session_id: str,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    sandbox_backend: Annotated[DockerSandboxBackend, Depends(get_sandbox_backend)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SessionResponse:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id, current_user)

    resolved: ResolvedSandbox | None = None
    try:
        resolved = await _resolve_sandbox_for_record(
            record, settings, store, sandbox_backend
        )
    except Exception:
        pass

    # Pre-fetch and cache conversation events before tearing down the sandbox container
    events = []
    if record.conversation_id and resolved:
        try:
            page = await agent_client.search_events(
                resolved.sandbox_url,
                resolved.sandbox_api_key,
                record.conversation_id,
                limit=1000,
            )
            events = page.get('items', []) if isinstance(page, dict) else []
        except Exception:
            pass

    if record.conversation_id and resolved:
        try:
            await agent_client.delete_conversation(
                resolved.sandbox_url,
                resolved.sandbox_api_key,
                record.conversation_id,
            )
        except Exception:
            pass
    if record.sandbox_scope == SandboxScope.SESSION:
        try:
            await sandbox_backend.delete(record.container_name)
        except Exception:
            pass

    record.events = events
    record.status = SessionStatus.DELETED
    if record.sandbox_scope == SandboxScope.USER and record.created_by_user_id:
        await store.release_user_sandbox_lease(
            record.created_by_user_id, SandboxLeaseKind.EXECUTION
        )
        await store.delete_session_secret(record.session_id)
    deleted = await store.save(record)
    return SessionResponse.from_record(deleted)


@app.post('/api/v1/sessions/{session_id}/execute', response_model=ExecuteResponse)
async def execute(
    session_id: str,
    request: ExecuteRequest,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
    manager: Annotated[SkillManager, Depends(get_skill_manager)],
    settings: Annotated[Settings, Depends(get_settings)],
    sandbox_backend: Annotated[DockerSandboxBackend, Depends(get_sandbox_backend)],
) -> ExecuteResponse:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id, current_user)
    if record.status == SessionStatus.DELETED:
        raise HTTPException(status.HTTP_410_GONE, 'session is deleted')
    lease_token: str | None = None
    if record.sandbox_scope == SandboxScope.USER:
        lease_token = await store.acquire_user_sandbox_lease(
            current_user.user_id,
            SandboxLeaseKind.EXECUTION,
            record.session_id,
            settings.user_sandbox_lease_ttl_seconds,
        )
        if lease_token is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {'detail': 'user sandbox is busy', 'action_required': 'wait_for_running_session'},
            )
    previous_generation = record.sandbox_generation
    resolved = await _resolve_sandbox_for_record(
        record, settings, store, sandbox_backend
    )
    requires_restore = (
        record.sandbox_scope == SandboxScope.USER
        and record.conversation_id is not None
        and previous_generation != resolved.generation
    )
    record.status = SessionStatus.RUNNING
    record.error = None
    await store.save(record)
    try:
        start_task_id: str | None = None
        skills = request.skills
        if record.project_id:
            project = await store.get_project(record.project_id)
            if project:
                project_skills = await store.list_project_skills(project.project_id)
                skill_mount = (
                    record.conversation_working_dir
                    if record.sandbox_scope == SandboxScope.USER
                    else None
                )
                skills = manager.build_skill_specs(
                    Path(project.workspace_dir), project_skills, skill_mount
                ) + skills
        if record.sandbox_scope == SandboxScope.USER:
            await _save_user_session_runtime(store, settings, record, request)
        if record.conversation_id is None or requires_restore:
            llm = request.llm
            if llm is None:
                default_model = await store.get_default_model()
                if default_model:
                    from mghands_gateway.models import LLMOverride
                    from pydantic import SecretStr
                    llm = LLMOverride(
                        provider=default_model.provider,
                        model=default_model.model,
                        base_url=default_model.base_url,
                        api_key=SecretStr(default_model.api_key) if default_model.api_key else None,
                    )
            if requires_restore:
                restored = await _load_user_session_runtime(store, settings, record)
                if restored:
                    llm = restored.get('llm') or llm
                    request.mcp_config = restored.get('mcp_config') or request.mcp_config
            conversation_id = record.conversation_id or (
                uuid4().hex if record.sandbox_scope == SandboxScope.USER else None
            )
            info = await agent_client.start_conversation(
                resolved.sandbox_url,
                resolved.sandbox_api_key,
                request.task,
                llm,
                skills,
                request.mcp_config,
                conversation_id=conversation_id,
                working_dir=resolved.working_dir,
                persistence_dir=resolved.persistence_dir,
                restore=requires_restore,
            )
            record.conversation_id = _extract_conversation_id(info)
            if info.get('status') == 'interrupted':
                record.status = SessionStatus.INTERRUPTED
                await store.save(record)
                if lease_token and record.created_by_user_id:
                    await store.release_user_sandbox_lease(
                        record.created_by_user_id, SandboxLeaseKind.EXECUTION, lease_token
                    )
                return ExecuteResponse(
                    session_id=record.session_id,
                    project_id=record.project_id,
                    sandbox_id=record.sandbox_id,
                    conversation_id=record.conversation_id,
                    status=record.status,
                    start_task_id=start_task_id,
                )
        else:
            if skills or request.mcp_config:
                await agent_client.update_runtime(
                    resolved.sandbox_url,
                    resolved.sandbox_api_key,
                    record.conversation_id,
                    skills=skills if skills else None,
                    mcp_config=request.mcp_config,
                )
            await agent_client.send_message(
                resolved.sandbox_url,
                resolved.sandbox_api_key,
                record.conversation_id,
                request.task,
            )
        record.status = SessionStatus.RUNNING
        await store.save(record)
        return ExecuteResponse(
            session_id=record.session_id,
            project_id=record.project_id,
            sandbox_id=record.sandbox_id,
            conversation_id=record.conversation_id,
            status=record.status,
            start_task_id=start_task_id,
        )
    except Exception as exc:
        record.status = SessionStatus.ERROR
        record.error = _safe_error(exc)
        await store.save(record)
        if lease_token and record.created_by_user_id:
            await store.release_user_sandbox_lease(
                record.created_by_user_id, SandboxLeaseKind.EXECUTION, lease_token
            )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, record.error) from exc


@app.get('/api/v1/sessions/{session_id}/history', response_model=HistoryResponse)
async def history(
    session_id: str,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    sandbox_backend: Annotated[DockerSandboxBackend, Depends(get_sandbox_backend)],
    page_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(gt=0, le=100)] = 100,
) -> HistoryResponse:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id, current_user)
    if not record.conversation_id:
        raise HTTPException(status.HTTP_409_CONFLICT, 'session has no conversation yet')

    if record.status == SessionStatus.DELETED:
        cached = record.events or []
        start_idx = 0
        if page_id:
            for idx, ev in enumerate(cached):
                if str(ev.get('id')) == page_id:
                    start_idx = idx + 1
                    break
        sliced = cached[start_idx : start_idx + limit]
        next_page = str(sliced[-1].get('id')) if len(sliced) == limit and start_idx + limit < len(cached) else None
        return HistoryResponse(
            session_id=session_id,
            conversation_id=record.conversation_id,
            events=sliced,
            next_page_id=next_page,
        )

    resolved = await _resolve_sandbox_for_record(record, settings, store, sandbox_backend)
    try:
        page = await agent_client.search_events(
            resolved.sandbox_url,
            resolved.sandbox_api_key,
            record.conversation_id,
            page_id=page_id,
            limit=limit,
        )
    except Exception as exc:
        if record.events:
            cached = record.events
            start_idx = 0
            if page_id:
                for idx, ev in enumerate(cached):
                    if str(ev.get('id')) == page_id:
                        start_idx = idx + 1
                        break
            sliced = cached[start_idx : start_idx + limit]
            next_page = str(sliced[-1].get('id')) if len(sliced) == limit and start_idx + limit < len(cached) else None
            return HistoryResponse(
                session_id=session_id,
                conversation_id=record.conversation_id,
                events=sliced,
                next_page_id=next_page,
            )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, _safe_error(exc)) from exc

    events = page.get('items', []) if isinstance(page, dict) else []
    if events:
        record.last_event_id = str(events[-1].get('id') or record.last_event_id)
        existing = record.events or []
        existing_ids = {str(e.get('id')) for e in existing if e.get('id')}
        new_events = [e for e in events if str(e.get('id')) not in existing_ids]
        record.events = existing + new_events
        await store.save(record)
        await _sync_terminal_event(store, record, events)
    return HistoryResponse(
        session_id=session_id,
        conversation_id=record.conversation_id,
        events=events,
        next_page_id=page.get('next_page_id') if isinstance(page, dict) else None,
    )


@app.get('/api/v1/sessions/{session_id}/stream')
async def stream(
    session_id: str,
    request: Request,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    sandbox_backend: Annotated[DockerSandboxBackend, Depends(get_sandbox_backend)],
    after: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id, current_user)
    if not record.conversation_id:
        for _ in range(30):
            await asyncio.sleep(1.0)
            record = await _get_record_or_404(store, session_id, current_user)
            if record.conversation_id:
                break
        else:
            raise HTTPException(status.HTTP_409_CONFLICT, 'session has no conversation yet')
    event_id = after or request.headers.get('last-event-id')
    generator = _event_stream(
        session_id, event_id, store, agent_client, sandbox_backend, settings, request
    )
    headers = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache, no-transform',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
        'X-Content-Type-Options': 'nosniff',
    }
    return StreamingResponse(generator, media_type='text/event-stream', headers=headers)


@app.post('/api/v1/sessions/{session_id}/skills/reload')
async def reload_skills(
    session_id: str,
    request: RuntimeUpdateRequest,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    sandbox_backend: Annotated[DockerSandboxBackend, Depends(get_sandbox_backend)],
) -> dict[str, Any]:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id, current_user)
    if not record.conversation_id:
        raise HTTPException(status.HTTP_409_CONFLICT, 'session has no conversation yet')
    resolved = await _resolve_sandbox_for_record(record, settings, store, sandbox_backend)
    if request.skills or request.mcp_config:
        await agent_client.update_runtime(
            resolved.sandbox_url,
            resolved.sandbox_api_key,
            record.conversation_id,
            skills=request.skills if request.skills else None,
            mcp_config=request.mcp_config,
        )
    skills = await agent_client.get_skills(
        resolved.sandbox_url, resolved.sandbox_api_key
    )
    return {'status': 'ok', 'mode': 'refreshed', 'skills': skills.get('skills', [])}


@app.post('/api/v1/sessions/{session_id}/confirm', response_model=ExecuteResponse)
async def confirm_recovery(
    session_id: str,
    request: ConfirmRecoveryRequest,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    sandbox_backend: Annotated[DockerSandboxBackend, Depends(get_sandbox_backend)],
) -> ExecuteResponse:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id, current_user)
    if record.status == SessionStatus.DELETED:
        raise HTTPException(status.HTTP_410_GONE, 'session is deleted')
    if record.status != SessionStatus.INTERRUPTED:
        raise HTTPException(
            status.HTTP_409_CONFLICT, 'session is not in interrupted state'
        )

    if not request.confirm:
        record.status = SessionStatus.ERROR
        record.error = 'recovery rejected by user'
        await store.save(record)
        return ExecuteResponse(
            session_id=record.session_id,
            project_id=record.project_id,
            sandbox_id=record.sandbox_id,
            conversation_id=record.conversation_id,
            status=record.status,
            start_task_id=None,
        )

    lease_token = None
    if record.sandbox_scope == SandboxScope.USER:
        lease_token = await store.acquire_user_sandbox_lease(
            current_user.user_id,
            SandboxLeaseKind.EXECUTION,
            record.session_id,
            settings.user_sandbox_lease_ttl_seconds,
        )
        if lease_token is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {'detail': 'user sandbox is busy', 'action_required': 'wait_for_running_session'},
            )

    resolved = await _resolve_sandbox_for_record(
        record, settings, store, sandbox_backend
    )

    record.status = SessionStatus.RUNNING
    record.error = None
    await store.save(record)

    try:
        await agent_client.send_message(
            resolved.sandbox_url,
            resolved.sandbox_api_key,
            record.conversation_id,
            "",  # empty message to trigger resume
        )
        return ExecuteResponse(
            session_id=record.session_id,
            project_id=record.project_id,
            sandbox_id=record.sandbox_id,
            conversation_id=record.conversation_id,
            status=record.status,
            start_task_id=None,
        )
    except Exception as exc:
        record.status = SessionStatus.ERROR
        record.error = _safe_error(exc)
        await store.save(record)
        if lease_token and record.created_by_user_id:
            await store.release_user_sandbox_lease(
                record.created_by_user_id, SandboxLeaseKind.EXECUTION, lease_token
            )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, record.error) from exc


async def _event_stream(
    session_id: str,
    after: str | None,
    store: SessionStore,
    agent_client: AgentServerClient,
    sandbox_backend: DockerSandboxBackend,
    settings: Settings,
    request: Request,
) -> AsyncGenerator[bytes, None]:
    seen: set[str] = set()
    after_seen = after is None
    idle_for = 0.0
    heartbeat_for = 0.0
    offset = 0
    while idle_for < settings.sse_idle_timeout_seconds:
        if await request.is_disconnected():
            return
        record = await store.require(session_id)
        if not record.conversation_id:
            return
        try:
            resolved = await _resolve_sandbox_for_record(
                record, settings, store, sandbox_backend
            )
            page = await agent_client.search_events(
                resolved.sandbox_url,
                resolved.sandbox_api_key,
                record.conversation_id,
                page_id=str(offset) if offset > 0 else None,
                limit=100,
            )
        except Exception as exc:
            print(f"WARNING: error polling events from sandbox (session_id={session_id}): {exc}", flush=True)
            await asyncio.sleep(settings.sse_poll_seconds)
            idle_for += settings.sse_poll_seconds
            heartbeat_for += settings.sse_poll_seconds
            if heartbeat_for >= settings.sse_heartbeat_seconds:
                heartbeat_for = 0.0
                yield ': heartbeat\n\n'.encode('utf-8')
            continue
        emitted = False
        if isinstance(page, dict):
            items = page.get('items', [])
            db_dirty = False
            for event in items:
                event_id = str(event.get('id') or '')
                if not event_id or event_id in seen:
                    continue
                if not after_seen:
                    if event_id == after:
                        after_seen = True
                    continue
                seen.add(event_id)
                record.last_event_id = event_id
                
                # Sync Sandbox execution completion/error to Gateway database status
                if event.get('kind') == 'agent.result':
                    record.status = SessionStatus.COMPLETED
                elif event.get('kind') == 'agent.error':
                    record.status = SessionStatus.ERROR
                    record.error = event.get('data', {}).get('error')
                
                db_dirty = True
                emitted = True
                yield _sse(event='message', data=event, event_id=event_id).encode('utf-8')
            
            if items:
                offset += len(items)
                if len(items) < 100 and not after_seen:
                    after_seen = True
            else:
                if not after_seen:
                    after_seen = True

            if db_dirty:
                await store.save(record)
                await _sync_terminal_event(store, record, items)

        if record.status in {SessionStatus.ERROR, SessionStatus.DELETED}:
            terminal_type = 'error' if record.status == SessionStatus.ERROR else 'cancelled'
            terminal = TerminalEvent(
                type=terminal_type,
                session_id=session_id,
                conversation_id=record.conversation_id,
                detail=record.error,
            )
            yield _sse(event=terminal_type, data=terminal.model_dump()).encode('utf-8')
            return
        await asyncio.sleep(settings.sse_poll_seconds)
        if emitted:
            idle_for = 0.0
            heartbeat_for = 0.0
        else:
            idle_for += settings.sse_poll_seconds
            heartbeat_for += settings.sse_poll_seconds
            if heartbeat_for >= settings.sse_heartbeat_seconds:
                heartbeat_for = 0.0
                yield ': heartbeat\n\n'.encode('utf-8')


def _sse(event: str, data: object, event_id: str | None = None) -> str:
    parts: list[str] = []
    if event_id:
        parts.append(f'id: {event_id}')
    parts.append(f'event: {event}')
    payload = json.dumps(data, ensure_ascii=False, default=str)
    for line in payload.splitlines() or ['']:
        parts.append(f'data: {line}')
    return '\n'.join(parts) + '\n\n'


async def _bootstrap_admin(settings: Settings, store: SessionStore) -> None:
    if await store.user_count() > 0:
        return
    if not settings.bootstrap_admin_username or not settings.bootstrap_admin_password:
        return
    await store.create_user(
        UserRecord(
            username=settings.bootstrap_admin_username,
            password_hash=hash_password(settings.bootstrap_admin_password.get_secret_value()),
            role=UserRole.ADMIN,
            enabled=True,
        )
    )


async def _create_project_record(
    store: SessionStore,
    settings: Settings,
    user_id: str,
    name: str,
) -> ProjectRecord:
    project_id = new_id('prj')
    workspace_dir = _project_workspace(settings, user_id, project_id)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return await store.create_project(
        ProjectRecord(
            project_id=project_id,
            user_id=user_id,
            name=name,
            workspace_dir=str(workspace_dir),
        )
    )


def _project_workspace(settings: Settings, user_id: str, project_id: str) -> Path:
    data_root = settings.data_root.resolve()
    workspace = (
        data_root / 'users' / user_id / 'projects' / project_id / 'workspace'
    ).resolve()
    if data_root != workspace and data_root not in workspace.parents:
        raise RuntimeError('project workspace escapes data root')
    return workspace


def _install_skill_or_http(
    manager: SkillManager, skill_name: str, project: ProjectRecord
) -> ProjectSkillRecord:
    try:
        return manager.install(skill_name, project.project_id, Path(project.workspace_dir))
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'skill not found') from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


async def _get_project_or_404(
    store: SessionStore, project_id: str, current_user: UserRecord
) -> ProjectRecord:
    project = await store.get_project(project_id)
    if project is None or project.status == ProjectStatus.DELETED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'project not found')
    if project.user_id != current_user.user_id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'project not found')
    return project


async def _create_session_for_project(
    request: CreateSessionRequest,
    project: ProjectRecord,
    current_user: UserRecord,
    settings: Settings,
    store: SessionStore,
    sandbox_backend: DockerSandboxBackend,
) -> SessionResponse:
    if request.sandbox_type != SandboxType.DOCKER and not settings.allow_non_docker_sandbox:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            'Only docker sandbox sessions are allowed by default; set MGHANDS_ALLOW_NON_DOCKER_SANDBOX=true for local development only.',
        )
    active = await store.get_active_session_for_project(project.project_id)
    if active is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                'detail': 'project has a running session',
                'running_session_id': active.session_id,
                'action_required': 'stop_running_session_first',
            },
        )
    try:
        scope = current_user.sandbox_scope or settings.sandbox_scope
        if scope == SandboxScope.USER:
            if request.sandbox_type != SandboxType.DOCKER:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    'User-scoped sandboxes require docker.',
                )
            _require_user_sandbox_encryption(settings)
            mount_root = settings.user_sandbox_mount_path.rstrip('/')
            record = SessionRecord(
                session_id=request.session_id,
                project_id=project.project_id,
                created_by_user_id=current_user.user_id,
                sandbox_scope=SandboxScope.USER,
                sandbox_type=request.sandbox_type,
                workspace_policy=request.workspace_policy,
                workspace_dir=project.workspace_dir,
                conversation_working_dir=(
                    f'{mount_root}/projects/{project.project_id}/workspace'
                ),
                status=SessionStatus.CREATED,
            )
            return SessionResponse.from_record(await store.create(record))
        import inspect
        sig = inspect.signature(sandbox_backend.create)
        if 'store' in sig.parameters:
            sandbox = await sandbox_backend.create(request, Path(project.workspace_dir), store=store)
        else:
            sandbox = await sandbox_backend.create(request, Path(project.workspace_dir))
        record = SessionRecord(
            session_id=request.session_id,
            project_id=project.project_id,
            sandbox_id=sandbox.sandbox_id,
            sandbox_url=sandbox.sandbox_url,
            sandbox_api_key=sandbox.sandbox_api_key,
            container_name=sandbox.container_name,
            created_by_user_id=current_user.user_id,
            sandbox_type=request.sandbox_type,
            workspace_policy=request.workspace_policy,
            workspace_dir=sandbox.workspace_dir,
            status=SessionStatus.CREATED,
            sandbox_scope=SandboxScope.SESSION,
        )
        return SessionResponse.from_record(await store.create(record))
    except KeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, 'session_id already exists') from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, _safe_error(exc)) from exc


async def _get_record_or_404(
    store: SessionStore, session_id: str, current_user: UserRecord
) -> SessionRecord:
    try:
        record = await store.require(session_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'session not found') from exc
    if record.created_by_user_id != current_user.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'session not found')
    return record


def _validate_id_or_400(session_id: str) -> None:
    try:
        validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


def _safe_error(exc: Exception) -> str:
    return str(exc).replace('\r', ' ').replace('\n', ' ')[:1000]


def _require_user_sandbox_encryption(settings: Settings) -> None:
    key_id = settings.gateway_active_secret_key_id
    if not key_id or key_id not in settings.gateway_secret_keys:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            'user sandbox encryption is not configured',
        )


async def _resolve_sandbox_for_record(
    record: SessionRecord,
    settings: Settings,
    store: SessionStore,
    sandbox_backend: DockerSandboxBackend,
) -> ResolvedSandbox:
    if record.sandbox_scope == SandboxScope.SESSION:
        return ResolvedSandbox(
            sandbox_id=record.sandbox_id or record.session_id,
            sandbox_url=_require_sandbox_url(record),
            sandbox_api_key=_session_api_key_value(record),
            container_name=record.container_name or '',
            generation=None,
            working_dir=record.conversation_working_dir or settings.sandbox_workspace_mount_path,
        )
    if not record.created_by_user_id or not record.project_id:
        raise RuntimeError('user-scoped session is missing owner or project')
    _require_user_sandbox_encryption(settings)
    user_id = record.created_by_user_id
    lease = await store.acquire_user_sandbox_lease(
        user_id,
        SandboxLeaseKind.PROVISIONING,
        record.session_id,
        settings.user_sandbox_lease_ttl_seconds,
    )
    if lease is None:
        raise RuntimeError('user sandbox is being provisioned')
    try:
        cipher = SecretCipher(settings)
        sandbox = await store.get_user_sandbox(user_id)
        if sandbox is None or sandbox.status in {
            UserSandboxStatus.DELETED,
            UserSandboxStatus.UNHEALTHY,
        }:
            api_key = 'sk-mghands-' + secrets.token_urlsafe(24)
            ciphertext, key_id = cipher.encrypt(api_key)
            container_name = f'mghands-user-{user_id}'
            sandbox = await store.begin_user_sandbox_generation(
                UserSandboxRecord(
                    user_id=user_id,
                    sandbox_id=container_name,
                    container_name=container_name,
                    api_key_ciphertext=ciphertext,
                    api_key_key_id=key_id,
                    generation=1,
                    image_ref=settings.sandbox_image,
                    status=UserSandboxStatus.PROVISIONING,
                )
            )
        api_key = cipher.decrypt(
            sandbox.api_key_ciphertext, sandbox.api_key_key_id
        )
        user_root = (settings.data_root.resolve() / 'users' / user_id).resolve()
        data_root = settings.data_root.resolve()
        if data_root not in user_root.parents:
            raise RuntimeError('user root escapes data root')
        handle = await sandbox_backend.ensure_user_sandbox(
            user_id=user_id,
            generation=sandbox.generation,
            user_root=user_root,
            session_api_key=api_key,
            container_name=sandbox.container_name,
            store=store,
        )
        sandbox.sandbox_url = handle.sandbox_url
        sandbox.status = UserSandboxStatus.BUSY if record.status == SessionStatus.RUNNING else UserSandboxStatus.READY
        sandbox.last_activity_at = utc_now()
        sandbox.idle_expires_at = sandbox.last_activity_at + timedelta(
            seconds=settings.user_sandbox_idle_ttl_seconds
        )
        await store.save_user_sandbox(sandbox)
        if record.sandbox_generation != sandbox.generation or record.sandbox_id != sandbox.sandbox_id:
            record.sandbox_generation = sandbox.generation
            record.sandbox_id = sandbox.sandbox_id
            await store.save(record)
        mount_root = settings.user_sandbox_mount_path.rstrip('/')
        return ResolvedSandbox(
            sandbox_id=sandbox.sandbox_id,
            sandbox_url=handle.sandbox_url,
            sandbox_api_key=api_key,
            container_name=sandbox.container_name,
            generation=sandbox.generation,
            working_dir=record.conversation_working_dir
            or f'{mount_root}/projects/{record.project_id}/workspace',
            persistence_dir=f'{mount_root}/.mghands/conversations',
        )
    except Exception as exc:
        sandbox = await store.get_user_sandbox(user_id)
        if sandbox and sandbox.status == UserSandboxStatus.PROVISIONING:
            sandbox.status = UserSandboxStatus.UNHEALTHY
            sandbox.error = _safe_error(exc)
            await store.save_user_sandbox(sandbox)
        raise
    finally:
        await store.release_user_sandbox_lease(
            user_id, SandboxLeaseKind.PROVISIONING, lease
        )


async def _save_user_session_runtime(
    store: SessionStore,
    settings: Settings,
    record: SessionRecord,
    request: ExecuteRequest,
) -> None:
    payload = {
        'llm': request.llm.model_dump(
            mode='json', context={'expose_secrets': True}
        ) if request.llm else None,
        'mcp_config': request.mcp_config.model_dump(mode='json')
        if request.mcp_config else None,
    }
    ciphertext, key_id = SecretCipher(settings).encrypt(
        json.dumps(payload, separators=(',', ':'))
    )
    await store.save_session_secret(record.session_id, ciphertext, key_id)


async def _load_user_session_runtime(
    store: SessionStore, settings: Settings, record: SessionRecord
) -> dict[str, Any] | None:
    saved = await store.get_session_secret(record.session_id)
    if saved is None:
        return None
    plaintext = SecretCipher(settings).decrypt(saved[0], saved[1])
    payload = json.loads(plaintext)
    from mghands_gateway.models import LLMOverride, MCPConfigSpec

    return {
        'llm': LLMOverride.model_validate(payload['llm']) if payload.get('llm') else None,
        'mcp_config': MCPConfigSpec.model_validate(payload['mcp_config'])
        if payload.get('mcp_config') else None,
    }


async def _sync_terminal_event(
    store: SessionStore, record: SessionRecord, events: list[dict[str, Any]]
) -> None:
    terminal = next(
        (event for event in reversed(events) if event.get('kind') in {'agent.result', 'agent.error'}),
        None,
    )
    if terminal is None:
        return
    if terminal.get('kind') == 'agent.result':
        record.status = SessionStatus.COMPLETED
        record.error = None
    else:
        record.status = SessionStatus.ERROR
        record.error = terminal.get('data', {}).get('error')
    await store.save(record)
    if record.sandbox_scope == SandboxScope.USER and record.created_by_user_id:
        await store.release_user_sandbox_lease(
            record.created_by_user_id, SandboxLeaseKind.EXECUTION
        )
        sandbox = await store.get_user_sandbox(record.created_by_user_id)
        if sandbox:
            sandbox.status = UserSandboxStatus.READY
            sandbox.last_activity_at = utc_now()
            await store.save_user_sandbox(sandbox)


def _require_sandbox_url(record: SessionRecord) -> str:
    if not record.sandbox_url:
        raise RuntimeError('session has no sandbox_url')
    return record.sandbox_url


def _session_api_key_value(record: SessionRecord) -> str | None:
    if record.sandbox_api_key is None:
        return None
    return record.sandbox_api_key.get_secret_value()


def _extract_conversation_id(info: dict[str, Any]) -> str:
    value = info.get('id') or info.get('conversation_id')
    if not value:
        raise RuntimeError('agent-server did not return conversation id')
    return str(value)


def _mount_web_app() -> None:
    possible_roots = [
        Path(__file__).resolve().parents[2],
        Path(__file__).resolve().parents[1],
        Path.cwd(),
    ]
    web_dist = None
    for r in possible_roots:
        candidate = r / 'web' / 'dist'
        if (candidate / 'index.html').exists():
            web_dist = candidate
            break

    if not web_dist:
        print("WARNING: Web app dist directory not found! Web UI will not be served.", flush=True)
        return

    print(f"INFO: Mounting web app from {web_dist}", flush=True)
    index_file = web_dist / 'index.html'
    assets_dir = web_dist / 'assets'
    if assets_dir.exists():
        app.mount('/assets', StaticFiles(directory=assets_dir), name='web-assets')

    @app.get('/{path:path}', include_in_schema=False)
    async def web_app(path: str) -> FileResponse:
        if path.startswith('api/'):
            raise HTTPException(status.HTTP_404_NOT_FOUND, 'not found')
        return FileResponse(index_file)


async def _reconcile_loop(settings: Settings, store: SessionStore) -> None:
    from mghands_gateway.sandbox_backend import DockerSandboxBackend
    from mghands_gateway.agent_client import AgentServerClient
    agent_client = AgentServerClient(settings)
    backend = DockerSandboxBackend(settings, agent_client)
    instance_id = uuid4().hex
    
    while True:
        try:
            await asyncio.sleep(30.0)
            lease = await store.acquire_user_sandbox_lease(
                'global',
                SandboxLeaseKind.PROVISIONING,
                instance_id,
                ttl_seconds=60,
            )
            if lease is None:
                continue
            try:
                await _reconcile_sandboxes(settings, store, backend, agent_client)
            finally:
                await store.release_user_sandbox_lease(
                    'global', SandboxLeaseKind.PROVISIONING, lease
                )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"ERROR: reconciler error: {exc}", flush=True)


async def _reconcile_sandboxes(
    settings: Settings,
    store: SessionStore,
    backend: DockerSandboxBackend,
    agent_client: AgentServerClient,
) -> None:
    from mghands_gateway.secret_store import SecretCipher
    from mghands_gateway.skills import SkillManager
    
    sandboxes = await store.list_user_sandboxes()
    users = {u.user_id for u in await store.list_users()}
    db_sandbox_ids = set()
    
    for sandbox in sandboxes:
        if sandbox.status == UserSandboxStatus.DELETED:
            continue
        db_sandbox_ids.add(sandbox.container_name)
        
        user_id = sandbox.user_id
        if user_id not in users:
            print(f"INFO: user {user_id} not found, deleting container {sandbox.container_name}", flush=True)
            sandbox.status = UserSandboxStatus.DELETING
            await store.save_user_sandbox(sandbox)
            await backend.delete(sandbox.container_name)
            sandbox.status = UserSandboxStatus.DELETED
            sandbox.sandbox_url = None
            await store.save_user_sandbox(sandbox)
            continue

        is_running = await asyncio.to_thread(backend._container_running, sandbox.container_name)
        alive = False
        if is_running:
            cipher = SecretCipher(settings)
            api_key = cipher.decrypt(sandbox.api_key_ciphertext, sandbox.api_key_key_id)
            url = await backend._sandbox_url(sandbox.container_name)
            alive = await agent_client.alive(url, api_key)
            
        if not is_running or not alive:
            print(f"INFO: sandbox container {sandbox.container_name} is not running or not alive, recreating...", flush=True)
            running_sessions = await store.get_running_sessions_for_user(user_id)
            for sess in running_sessions:
                sess.status = SessionStatus.INTERRUPTED
                await store.save(sess)
            await store.release_user_sandbox_lease(user_id, SandboxLeaseKind.EXECUTION)
            
            try:
                cipher = SecretCipher(settings)
                api_key = cipher.decrypt(sandbox.api_key_ciphertext, sandbox.api_key_key_id)
                user_root = (settings.data_root.resolve() / 'users' / user_id).resolve()
                
                handle = await backend.ensure_user_sandbox(
                    user_id=user_id,
                    generation=sandbox.generation,
                    user_root=user_root,
                    session_api_key=api_key,
                    container_name=sandbox.container_name,
                    store=store,
                )
                sandbox.sandbox_url = handle.sandbox_url
                sandbox.status = UserSandboxStatus.READY
                sandbox.last_activity_at = utc_now()
                sandbox.idle_expires_at = sandbox.last_activity_at + timedelta(
                    seconds=settings.user_sandbox_idle_ttl_seconds
                )
                await store.save_user_sandbox(sandbox)
                
                active_sessions = await store.get_active_sessions_for_user(user_id)
                for sess in active_sessions:
                    if not sess.conversation_id:
                        continue
                    restored = await _load_user_session_runtime(store, settings, sess)
                    llm = restored.get('llm') if restored else None
                    mcp_config = restored.get('mcp_config') if restored else None
                    
                    skills = []
                    if sess.project_id:
                        project = await store.get_project(sess.project_id)
                        if project:
                            project_skills = await store.list_project_skills(project.project_id)
                            skill_mount = sess.conversation_working_dir
                            skills = SkillManager(
                                shared_root=settings.shared_skills_root,
                                workspace_mount_path=settings.sandbox_workspace_mount_path,
                            ).build_skill_specs(
                                Path(project.workspace_dir), project_skills, skill_mount
                            )
                    
                    mount_root = settings.user_sandbox_mount_path.rstrip('/')
                    working_dir = sess.conversation_working_dir or f'{mount_root}/projects/{sess.project_id}/workspace'
                    persistence_dir = f'{mount_root}/.mghands/conversations'
                    
                    try:
                        await agent_client.start_conversation(
                            base_url=handle.sandbox_url,
                            session_api_key=api_key,
                            task="",
                            llm=llm,
                            skills=skills,
                            mcp_config=mcp_config,
                            conversation_id=sess.conversation_id,
                            working_dir=working_dir,
                            persistence_dir=persistence_dir,
                            restore=True,
                        )
                    except Exception as e:
                        print(f"WARNING: failed to restore conversation {sess.conversation_id} in reconciler: {e}", flush=True)
            except Exception as e:
                print(f"ERROR: failed to recreate sandbox for user {user_id}: {e}", flush=True)
                sandbox.status = UserSandboxStatus.UNHEALTHY
                sandbox.error = _safe_error(e)
                await store.save_user_sandbox(sandbox)
        else:
            has_lease = await store.has_user_sandbox_lease(user_id, SandboxLeaseKind.EXECUTION)
            if not has_lease and sandbox.idle_expires_at and utc_now() > sandbox.idle_expires_at:
                print(f"INFO: recycling idle sandbox container {sandbox.container_name}", flush=True)
                sandbox.status = UserSandboxStatus.DELETING
                await store.save_user_sandbox(sandbox)
                await backend.delete(sandbox.container_name)
                sandbox.status = UserSandboxStatus.DELETED
                sandbox.sandbox_url = None
                await store.save_user_sandbox(sandbox)

    try:
        user_containers = await asyncio.to_thread(backend.list_user_containers)
        for name in user_containers:
            if name not in db_sandbox_ids:
                print(f"INFO: deleting orphan container {name}", flush=True)
                await backend.delete(name)
    except Exception as e:
        print(f"WARNING: failed to list or delete orphan containers: {e}", flush=True)


_mount_web_app()


def main() -> None:
    uvicorn.run('mghands_gateway.app:app', host='0.0.0.0', port=8080, reload=False)


if __name__ == '__main__':
    main()
