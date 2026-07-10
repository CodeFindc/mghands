from typing import Any

import httpx

from mghands_gateway.config import Settings
from mghands_gateway.models import LLMOverride, MCPConfigSpec, SkillSpec, redact_sensitive


class AgentServerClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _headers(self, session_api_key: str | None) -> dict[str, str]:
        if not session_api_key:
            return {}
        return {'X-Session-API-Key': session_api_key}

    async def request(
        self,
        base_url: str,
        session_api_key: str | None,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        timeout = kwargs.pop('timeout', self.settings.request_timeout_seconds)
        async with httpx.AsyncClient(
            base_url=base_url.rstrip('/'),
            headers=self._headers(session_api_key),
            timeout=timeout,
        ) as client:
            response = await client.request(method, path, **kwargs)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = redact_sensitive(_safe_json(exc.response))
                raise RuntimeError(
                    f'Agent server {method} {path} failed with {exc.response.status_code}: {detail}'
                ) from exc
            return _safe_json(response)

    async def alive(self, base_url: str, session_api_key: str | None) -> bool:
        try:
            data = await self.request(base_url, session_api_key, 'GET', '/alive')
            return isinstance(data, dict) and data.get('status') == 'ok'
        except Exception:
            return False

    async def ready(self, base_url: str, session_api_key: str | None) -> dict[str, Any]:
        return await self.request(base_url, session_api_key, 'GET', '/ready')

    async def server_info(
        self, base_url: str, session_api_key: str | None
    ) -> dict[str, Any]:
        return await self.request(base_url, session_api_key, 'GET', '/server_info')

    async def runtime_info(
        self, base_url: str, session_api_key: str | None
    ) -> dict[str, Any]:
        return await self.request(base_url, session_api_key, 'GET', '/api/runtime')

    async def start_conversation(
        self,
        base_url: str,
        session_api_key: str | None,
        task: str,
        llm: LLMOverride | None,
        skills: list[SkillSpec] | None = None,
        mcp_config: MCPConfigSpec | None = None,
        *,
        conversation_id: str | None = None,
        working_dir: str | None = None,
        persistence_dir: str | None = None,
        restore: bool = False,
    ) -> dict[str, Any]:
        payload = _start_conversation_payload(task, llm, skills, mcp_config)
        if conversation_id:
            payload['conversation_id'] = conversation_id
        if working_dir:
            payload['working_dir'] = working_dir
        if persistence_dir:
            payload['persistence_dir'] = persistence_dir
        if restore:
            payload['restore'] = True
        return await self.request(
            base_url,
            session_api_key,
            'POST',
            '/api/conversations',
            json=payload,
            timeout=self.settings.conversation_start_timeout_seconds,
        )

    async def send_message(
        self,
        base_url: str,
        session_api_key: str | None,
        conversation_id: str,
        task: str,
    ) -> dict[str, Any]:
        return await self.request(
            base_url,
            session_api_key,
            'POST',
            f'/api/conversations/{conversation_id}/events',
            json={
                'role': 'user',
                'content': [{'type': 'text', 'text': task}],
                'run': True,
            },
        )

    async def update_runtime(
        self,
        base_url: str,
        session_api_key: str | None,
        conversation_id: str,
        *,
        skills: list[SkillSpec] | None = None,
        mcp_config: MCPConfigSpec | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if skills is not None:
            payload['skills'] = [skill.model_dump() for skill in skills]
        if mcp_config is not None:
            payload['mcp_config'] = mcp_config.model_dump()
        return await self.request(
            base_url,
            session_api_key,
            'POST',
            f'/api/conversations/{conversation_id}/runtime',
            json=payload,
        )

    async def get_runtime(
        self, base_url: str, session_api_key: str | None, conversation_id: str
    ) -> dict[str, Any]:
        return await self.request(
            base_url,
            session_api_key,
            'GET',
            f'/api/conversations/{conversation_id}/runtime',
        )

    async def shutdown(
        self,
        base_url: str,
        session_api_key: str | None,
        delay_seconds: float = 0.2,
    ) -> dict[str, Any]:
        return await self.request(
            base_url,
            session_api_key,
            'POST',
            '/api/shutdown',
            json={'delay_seconds': delay_seconds},
        )

    async def delete_conversation(
        self, base_url: str, session_api_key: str | None, conversation_id: str
    ) -> None:
        await self.request(
            base_url, session_api_key, 'DELETE', f'/api/conversations/{conversation_id}'
        )

    async def search_events(
        self,
        base_url: str,
        session_api_key: str | None,
        conversation_id: str,
        *,
        page_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {'limit': limit}
        if page_id is not None:
            params['page_id'] = page_id
        page = await self.request(
            base_url,
            session_api_key,
            'GET',
            f'/api/conversations/{conversation_id}/events/search',
            params=params,
        )
        return redact_sensitive(page)

    async def get_skills(
        self, base_url: str, session_api_key: str | None
    ) -> dict[str, Any]:
        skills = await self.request(base_url, session_api_key, 'GET', '/api/skills')
        return redact_sensitive(skills)


def _start_conversation_payload(
    task: str,
    llm: LLMOverride | None,
    skills: list[SkillSpec] | None,
    mcp_config: MCPConfigSpec | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'initial_message': {
            'role': 'user',
            'content': [{'type': 'text', 'text': task}],
            'run': True,
        }
    }
    if llm:
        llm_payload: dict[str, Any] = {}
        if llm.provider:
            llm_payload['provider'] = llm.provider
        if llm.model:
            llm_payload['model'] = llm.model
        if llm.base_url:
            llm_payload['base_url'] = llm.base_url
        if llm.api_key:
            llm_payload['api_key'] = llm.api_key.get_secret_value()
        if llm_payload:
            payload['llm'] = llm_payload
    if skills:
        payload['skills'] = [skill.model_dump() for skill in skills]
    if mcp_config:
        payload['mcp_config'] = mcp_config.model_dump()
    return payload


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text
