import asyncio
import json
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
    yield


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
    return UserResponse.from_record(await store.update_user(user))


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
) -> ProjectResponse:
    project = await _get_project_or_404(store, project_id, current_user)
    project.status = ProjectStatus.DELETED
    return ProjectResponse.from_record(await store.save_project(project))


@app.get('/api/v1/projects/{project_id}/files')
async def list_project_files(
    project_id: str,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, Any]]:
    project = await store.get_project(project_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'project not found')
    if project.user_id != current_user.user_id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'forbidden')

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
    project = await store.get_project(project_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'project not found')
    if project.user_id != current_user.user_id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'forbidden')

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
    active = await store.get_active_session_for_project(project.project_id)
    active_id = active.session_id if active else None

    filtered = []
    for r in records:
        if r.status != SessionStatus.DELETED or r.session_id == active_id:
            filtered.append(SessionResponse.from_record(r))
    return filtered


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
) -> SessionResponse:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id, current_user)
    try:
        if record.conversation_id:
            await agent_client.delete_conversation(
                _require_sandbox_url(record),
                _session_api_key_value(record),
                record.conversation_id,
            )
        await sandbox_backend.delete(record.container_name)
    except Exception as exc:
        record.status = SessionStatus.ERROR
        record.error = _safe_error(exc)
        await store.save(record)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, record.error) from exc
    deleted = await store.mark_deleted(session_id)
    return SessionResponse.from_record(deleted)


@app.post('/api/v1/sessions/{session_id}/execute', response_model=ExecuteResponse)
async def execute(
    session_id: str,
    request: ExecuteRequest,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
    manager: Annotated[SkillManager, Depends(get_skill_manager)],
) -> ExecuteResponse:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id, current_user)
    if record.status == SessionStatus.DELETED:
        raise HTTPException(status.HTTP_410_GONE, 'session is deleted')
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
                skills = manager.build_skill_specs(Path(project.workspace_dir), project_skills) + skills
        if record.conversation_id is None:
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
            info = await agent_client.start_conversation(
                _require_sandbox_url(record),
                _session_api_key_value(record),
                request.task,
                llm,
                skills,
                request.mcp_config,
            )
            record.conversation_id = _extract_conversation_id(info)
        else:
            if skills or request.mcp_config:
                await agent_client.update_runtime(
                    _require_sandbox_url(record),
                    _session_api_key_value(record),
                    record.conversation_id,
                    skills=skills if skills else None,
                    mcp_config=request.mcp_config,
                )
            await agent_client.send_message(
                _require_sandbox_url(record),
                _session_api_key_value(record),
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
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, record.error) from exc


@app.get('/api/v1/sessions/{session_id}/history', response_model=HistoryResponse)
async def history(
    session_id: str,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
    page_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(gt=0, le=100)] = 100,
) -> HistoryResponse:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id, current_user)
    if not record.conversation_id:
        raise HTTPException(status.HTTP_409_CONFLICT, 'session has no conversation yet')
    page = await agent_client.search_events(
        _require_sandbox_url(record),
        _session_api_key_value(record),
        record.conversation_id,
        page_id=page_id,
        limit=limit,
    )
    events = page.get('items', []) if isinstance(page, dict) else []
    if events:
        record.last_event_id = str(events[-1].get('id') or record.last_event_id)
        await store.save(record)
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
    generator = _event_stream(session_id, event_id, store, agent_client, settings, request)
    headers = {
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
    }
    return StreamingResponse(generator, media_type='text/event-stream', headers=headers)


@app.post('/api/v1/sessions/{session_id}/skills/reload')
async def reload_skills(
    session_id: str,
    request: RuntimeUpdateRequest,
    current_user: Annotated[UserRecord, Depends(_require_auth)],
    store: Annotated[SessionStore, Depends(get_store)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
) -> dict[str, Any]:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id, current_user)
    if not record.conversation_id:
        raise HTTPException(status.HTTP_409_CONFLICT, 'session has no conversation yet')
    if request.skills or request.mcp_config:
        await agent_client.update_runtime(
            _require_sandbox_url(record),
            _session_api_key_value(record),
            record.conversation_id,
            skills=request.skills if request.skills else None,
            mcp_config=request.mcp_config,
        )
    skills = await agent_client.get_skills(
        _require_sandbox_url(record), _session_api_key_value(record)
    )
    return {'status': 'ok', 'mode': 'refreshed', 'skills': skills.get('skills', [])}


async def _event_stream(
    session_id: str,
    after: str | None,
    store: SessionStore,
    agent_client: AgentServerClient,
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
        page = await agent_client.search_events(
            _require_sandbox_url(record),
            _session_api_key_value(record),
            record.conversation_id,
            page_id=str(offset) if offset > 0 else None,
            limit=100,
        )
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
    if (
        project is None
        or project.status == ProjectStatus.DELETED
        or project.user_id != current_user.user_id
    ):
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


_mount_web_app()


def main() -> None:
    uvicorn.run('mghands_gateway.app:app', host='0.0.0.0', port=8080, reload=False)


if __name__ == '__main__':
    main()
