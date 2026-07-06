import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from mghands_gateway.config import Settings, get_settings
from mghands_gateway.agent_client import AgentServerClient
from mghands_gateway.models import (
    CreateSessionRequest,
    ExecuteRequest,
    ExecuteResponse,
    HistoryResponse,
    SessionRecord,
    SessionResponse,
    SessionStatus,
    RuntimeUpdateRequest,
    SandboxType,
    TerminalEvent,
    validate_session_id,
)
from mghands_gateway.sandbox_backend import DockerSandboxBackend
from mghands_gateway.session_store import SessionStore


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await SessionStore(get_settings().database_path).init()
    yield


app = FastAPI(
    title='Mghands OpenHands Gateway',
    version='0.1.0',
    lifespan=lifespan,
)


@app.get('/health')
async def health() -> dict[str, str]:
    return {'status': 'ok'}


@app.post('/api/v1/sessions', response_model=SessionResponse, status_code=201)
async def create_session(
    request: CreateSessionRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[SessionStore, Depends(get_store)],
    sandbox_backend: Annotated[DockerSandboxBackend, Depends(get_sandbox_backend)],
) -> SessionResponse:
    if request.sandbox_type != SandboxType.DOCKER and not settings.allow_non_docker_sandbox:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            'Only docker sandbox sessions are allowed by default; set MGHANDS_ALLOW_NON_DOCKER_SANDBOX=true for local development only.',
        )
    try:
        sandbox = await sandbox_backend.create(request)
        record = SessionRecord(
            session_id=request.session_id,
            sandbox_id=sandbox.sandbox_id,
            sandbox_url=sandbox.sandbox_url,
            sandbox_api_key=sandbox.sandbox_api_key,
            container_name=sandbox.container_name,
            sandbox_type=request.sandbox_type,
            workspace_policy=request.workspace_policy,
            workspace_dir=sandbox.workspace_dir,
            status=SessionStatus.CREATED,
        )
        return SessionResponse.from_record(await store.create(record))
    except KeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, 'session_id already exists') from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, _safe_error(exc)) from exc


@app.get('/api/v1/sessions/{session_id}', response_model=SessionResponse)
async def get_session(
    session_id: str,
    store: Annotated[SessionStore, Depends(get_store)],
) -> SessionResponse:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id)
    return SessionResponse.from_record(record)


@app.delete('/api/v1/sessions/{session_id}', response_model=SessionResponse)
async def delete_session(
    session_id: str,
    store: Annotated[SessionStore, Depends(get_store)],
    sandbox_backend: Annotated[DockerSandboxBackend, Depends(get_sandbox_backend)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
) -> SessionResponse:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id)
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
    store: Annotated[SessionStore, Depends(get_store)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
) -> ExecuteResponse:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id)
    if record.status == SessionStatus.DELETED:
        raise HTTPException(status.HTTP_410_GONE, 'session is deleted')
    record.status = SessionStatus.RUNNING
    record.error = None
    await store.save(record)
    try:
        start_task_id: str | None = None
        if record.conversation_id is None:
            info = await agent_client.start_conversation(
                _require_sandbox_url(record),
                _session_api_key_value(record),
                request.task,
                request.llm,
                request.skills,
                request.mcp_config,
            )
            record.conversation_id = _extract_conversation_id(info)
        else:
            if request.skills or request.mcp_config:
                await agent_client.update_runtime(
                    _require_sandbox_url(record),
                    _session_api_key_value(record),
                    record.conversation_id,
                    skills=request.skills if request.skills else None,
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
    store: Annotated[SessionStore, Depends(get_store)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
    page_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(gt=0, le=100)] = 100,
) -> HistoryResponse:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id)
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
    store: Annotated[SessionStore, Depends(get_store)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    after: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id)
    if not record.conversation_id:
        raise HTTPException(status.HTTP_409_CONFLICT, 'session has no conversation yet')
    event_id = after or request.headers.get('last-event-id')
    generator = _event_stream(session_id, event_id, store, agent_client, settings, request)
    return StreamingResponse(generator, media_type='text/event-stream')


@app.post('/api/v1/sessions/{session_id}/skills/reload')
async def reload_skills(
    session_id: str,
    request: RuntimeUpdateRequest,
    store: Annotated[SessionStore, Depends(get_store)],
    agent_client: Annotated[AgentServerClient, Depends(get_agent_client)],
) -> dict[str, Any]:
    _validate_id_or_400(session_id)
    record = await _get_record_or_404(store, session_id)
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
) -> AsyncGenerator[str, None]:
    seen: set[str] = set()
    after_seen = after is None
    idle_for = 0.0
    heartbeat_for = 0.0
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
            limit=100,
        )
        emitted = False
        if isinstance(page, dict):
            for event in page.get('items', []):
                event_id = str(event.get('id') or '')
                if not event_id or event_id in seen:
                    continue
                if not after_seen:
                    if event_id == after:
                        after_seen = True
                    continue
                seen.add(event_id)
                record.last_event_id = event_id
                await store.save(record)
                emitted = True
                yield _sse(event='message', data=event, event_id=event_id)
        if record.status in {SessionStatus.ERROR, SessionStatus.DELETED}:
            terminal_type = 'error' if record.status == SessionStatus.ERROR else 'cancelled'
            terminal = TerminalEvent(
                type=terminal_type,
                session_id=session_id,
                conversation_id=record.conversation_id,
                detail=record.error,
            )
            yield _sse(event=terminal_type, data=terminal.model_dump())
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
                yield ': heartbeat\n\n'


def _sse(event: str, data: object, event_id: str | None = None) -> str:
    parts: list[str] = []
    if event_id:
        parts.append(f'id: {event_id}')
    parts.append(f'event: {event}')
    payload = json.dumps(data, ensure_ascii=False, default=str)
    for line in payload.splitlines() or ['']:
        parts.append(f'data: {line}')
    return '\n'.join(parts) + '\n\n'


async def _get_record_or_404(store: SessionStore, session_id: str) -> SessionRecord:
    try:
        return await store.require(session_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'session not found') from exc


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


def main() -> None:
    uvicorn.run('mghands_gateway.app:app', host='0.0.0.0', port=8080, reload=False)


if __name__ == '__main__':
    main()
