import os
import asyncio
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status

from mghands_sandbox.models import (
    EventPage,
    MessageRequest,
    ServerInfo,
    ShutdownRequest,
    StartConversationRequest,
    Success,
    UpdateRuntimeRequest,
)
from mghands_sandbox.sdk_runtime import (
    SDKBuildError,
    SDKRunError,
    SDKRuntime,
    SDKUnavailableError,
)

SESSION_KEYS = {
    value
    for key, value in os.environ.items()
    if key.startswith('OH_SESSION_API_KEYS_') and value
}

runtime = SDKRuntime()
app = FastAPI(title='Mghands OpenHands SDK Sandbox', version='0.1.0')

STANDARD_ENDPOINTS = [
    'GET /alive',
    'GET /ready',
    'GET /server_info',
    'GET /api/runtime',
    'POST /api/conversations',
    'GET /api/conversations',
    'GET /api/conversations/{conversation_id}',
    'DELETE /api/conversations/{conversation_id}',
    'POST /api/conversations/{conversation_id}/events',
    'GET /api/conversations/{conversation_id}/events/search',
    'GET /api/conversations/{conversation_id}/runtime',
    'POST /api/conversations/{conversation_id}/runtime',
    'GET /api/skills',
    'POST /api/shutdown',
]


def verify_session_key(
    x_session_api_key: Annotated[str | None, Header(alias='X-Session-API-Key')] = None,
) -> None:
    if not SESSION_KEYS:
        return
    if not x_session_api_key or x_session_api_key not in SESSION_KEYS:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'invalid session api key')


@app.get('/alive')
async def alive() -> dict[str, str]:
    return {'status': 'ok'}


@app.get('/ready')
async def ready():
    info = runtime.runtime_info(
        session_auth_enabled=bool(SESSION_KEYS),
    )
    if info.status == 'shutting_down':
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, info.model_dump(mode='json'))
    return info


@app.get('/server_info')
async def server_info() -> ServerInfo:
    return ServerInfo(
        standard_endpoints=STANDARD_ENDPOINTS,
        browser_tools_enabled=os.getenv('MGHANDS_ENABLE_BROWSER_TOOLS', '').lower()
        in {'1', 'true', 'yes'},
        default_tool_sources=[
            'openhands.tools.get_default_tools(enable_browser=$MGHANDS_ENABLE_BROWSER_TOOLS, enable_sub_agents=True)',
            'openhands.tools.register_builtins_agents(enable_browser=$MGHANDS_ENABLE_BROWSER_TOOLS)',
            'openhands.sdk.settings.OpenHandsAgentSettings',
            'openhands.sdk.AgentContext',
        ],
    )


@app.get('/api/runtime', dependencies=[Depends(verify_session_key)])
async def runtime_info():
    return runtime.runtime_info(session_auth_enabled=bool(SESSION_KEYS))


@app.post('/api/conversations', dependencies=[Depends(verify_session_key)])
async def start_conversation(request: StartConversationRequest):
    try:
        return await runtime.create_conversation(request)
    except SDKUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except SDKBuildError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except SDKRunError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@app.get('/api/conversations', dependencies=[Depends(verify_session_key)])
async def get_conversations(ids: Annotated[list[str] | None, Query()] = None):
    if not ids:
        return []
    return [await runtime.get_conversation(conversation_id) for conversation_id in ids]


@app.get('/api/conversations/{conversation_id}', dependencies=[Depends(verify_session_key)])
async def get_conversation(conversation_id: str):
    conversation = await runtime.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'conversation not found')
    return conversation


@app.delete('/api/conversations/{conversation_id}', dependencies=[Depends(verify_session_key)])
async def delete_conversation(conversation_id: str) -> Success:
    deleted = await runtime.delete_conversation(conversation_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'conversation not found')
    return Success()


@app.post('/api/conversations/{conversation_id}/events', dependencies=[Depends(verify_session_key)])
async def send_event(conversation_id: str, request: MessageRequest):
    try:
        return await runtime.send_message(conversation_id, request)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'conversation not found') from exc
    except SDKRunError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@app.get(
    '/api/conversations/{conversation_id}/events/search',
    dependencies=[Depends(verify_session_key)],
)
async def search_events(
    conversation_id: str,
    page_id: str | None = None,
    limit: Annotated[int, Query(gt=0, le=100)] = 100,
) -> EventPage:
    try:
        items, next_page_id = await runtime.search_events(conversation_id, page_id, limit)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'conversation not found') from exc
    return EventPage(items=items, next_page_id=next_page_id)


@app.post('/api/conversations/{conversation_id}/runtime', dependencies=[Depends(verify_session_key)])
async def update_runtime(conversation_id: str, request: UpdateRuntimeRequest):
    try:
        return await runtime.update_runtime(
            conversation_id,
            skills=request.skills,
            mcp_config=request.mcp_config,
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'conversation not found') from exc
    except SDKUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


@app.get('/api/conversations/{conversation_id}/runtime', dependencies=[Depends(verify_session_key)])
async def get_runtime(conversation_id: str):
    try:
        return await runtime.get_runtime_state(conversation_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'conversation not found') from exc


@app.get('/api/skills', dependencies=[Depends(verify_session_key)])
async def list_skills(conversation_id: str | None = None):
    return {'skills': await runtime.list_skills(conversation_id)}


@app.post('/api/shutdown', dependencies=[Depends(verify_session_key)])
async def shutdown(request: ShutdownRequest) -> Success:
    runtime.mark_shutting_down()

    async def exit_later() -> None:
        await asyncio.sleep(request.delay_seconds)
        os._exit(0)

    asyncio.create_task(exit_later())
    return Success()


def main() -> None:
    import uvicorn

    uvicorn.run('mghands_sandbox.app:app', host='0.0.0.0', port=3000)


if __name__ == '__main__':
    main()
