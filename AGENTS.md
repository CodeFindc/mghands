# Mghands Project Guide

This file is the quick repository guide for future contributors and AI agents. It summarizes the implemented architecture, API flow, runtime behavior, and important constraints discovered during integration.

## Project Purpose

Mghands is a lightweight FastAPI gateway around OpenHands SDK sandbox containers.

The gateway exposes stable session/task APIs to clients and starts one isolated Docker sandbox per session. Each sandbox runs `mghands_sandbox`, which lazy-loads the official OpenHands SDK and executes agent work inside the container workspace.

High-level flow:

```text
client/frontend
  -> mghands_gateway FastAPI service
  -> per-session Docker sandbox
  -> mghands_sandbox FastAPI service
  -> openhands-sdk Conversation
  -> OpenHands tools such as file editor and terminal
```

Gateway does not run `Conversation.run()` directly. It handles session persistence, Docker lifecycle, sandbox auth, history, and SSE forwarding.

## Key Directories

- `src/mghands_gateway/`: public gateway service and session orchestration.
- `src/mghands_sandbox/`: container-side OpenHands SDK adapter and standard API surface.
- `tests/`: unit tests for gateway models, session store, sandbox APIs, and SDK adapter behavior.
- `Dockerfile.sandbox`: builds the sandbox image that runs `mghands_sandbox`.
- `RUNBOOK.md`: operator-focused runbook with commands, endpoints, and examples.

## Important Runtime Components

### Gateway

Important files:

- `src/mghands_gateway/app.py`
- `src/mghands_gateway/agent_client.py`
- `src/mghands_gateway/sandbox_backend.py`
- `src/mghands_gateway/session_store.py`
- `src/mghands_gateway/models.py`

Gateway responsibilities:

- Create, query, delete sessions.
- Start one Docker sandbox per session.
- Mount a host workspace directory into the container.
- Inject `OH_SESSION_API_KEYS_0` for sandbox request auth.
- Create or reuse sandbox conversations.
- Forward user tasks to sandbox events.
- Poll sandbox event history and expose Gateway history/SSE endpoints.

### Sandbox

Important files:

- `src/mghands_sandbox/app.py`
- `src/mghands_sandbox/sdk_runtime.py`
- `src/mghands_sandbox/models.py`

Sandbox responsibilities:

- Expose OpenHands-compatible conversation endpoints.
- Build OpenHands SDK `Conversation` with default coding tools.
- Bind workspace to `/workspace` by default.
- Send user prompt via `conversation.send_message(prompt)` and then execute `conversation.run()`.
- Capture OpenHands SDK raw callback events into local `EventRecord` objects.
- Expose event history for Gateway history/SSE.

## Current Public Gateway APIs

Prefer these APIs for frontend integration.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/sessions` | Create a session and sandbox container. |
| `GET` | `/api/v1/sessions/{session_id}` | Query session metadata. |
| `DELETE` | `/api/v1/sessions/{session_id}` | Delete conversation and destroy sandbox. |
| `POST` | `/api/v1/sessions/{session_id}/execute` | Send a user task. Creates the sandbox conversation on first use. |
| `GET` | `/api/v1/sessions/{session_id}/history` | Fetch paginated conversation events. |
| `GET` | `/api/v1/sessions/{session_id}/stream` | SSE stream of new events. |
| `POST` | `/api/v1/sessions/{session_id}/skills/reload` | Update skills or MCP config and return current skills. |

Frontend should usually consume:

```text
POST /api/v1/sessions
POST /api/v1/sessions/{session_id}/execute
GET  /api/v1/sessions/{session_id}/stream
GET  /api/v1/sessions/{session_id}/history
```

## Current Sandbox APIs

These are container-internal APIs. Gateway calls these directly. Except `/alive`, `/ready`, and `/server_info`, requests normally require `X-Session-API-Key`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/alive` | Lightweight health check. |
| `GET` | `/ready` | Runtime status, SDK availability, conversation count. |
| `GET` | `/server_info` | Capability declaration and supported endpoints. |
| `GET` | `/api/runtime` | Sandbox runtime state. |
| `POST` | `/api/conversations` | Create OpenHands SDK conversation. |
| `GET` | `/api/conversations?ids=...` | Batch query conversations. |
| `GET` | `/api/conversations/{id}` | Query one conversation. |
| `DELETE` | `/api/conversations/{id}` | Delete one conversation. |
| `POST` | `/api/conversations/{id}/events` | Send user message and optionally run. |
| `GET` | `/api/conversations/{id}/events/search` | Fetch paginated event records. |
| `GET` | `/api/conversations/{id}/runtime` | Query runtime state for one conversation. |
| `POST` | `/api/conversations/{id}/runtime` | Hot-update skills/MCP config. |
| `GET` | `/api/skills` | List current skills. |
| `POST` | `/api/shutdown` | Gracefully self-terminate sandbox service. |

## Event Streaming Behavior

Gateway SSE endpoint:

```text
GET /api/v1/sessions/{session_id}/stream
```

It polls sandbox `/api/conversations/{conversation_id}/events/search` and emits each event as:

```text
id: <event_id>
event: message
data: <EventRecord JSON>
```

It supports:

- `?after=<event_id>`
- `Last-Event-ID: <event_id>`
- heartbeat comments: `: heartbeat`

Current `EventRecord` shape:

```json
{
  "id": "...",
  "kind": "openhands.ActionEvent",
  "timestamp": "...",
  "data": {}
}
```

## OpenHands Raw Event Capture

The sandbox SDK adapter now injects OpenHands SDK `callbacks` into `Conversation(...)` and stores every raw SDK event as an `EventRecord`.

Implemented in:

- `src/mghands_sandbox/sdk_runtime.py`
- `_OfficialSDKAdapter._build_event_callback(...)`
- `_sdk_event_payload(...)`

Event kinds are prefixed with `openhands.` and use the SDK event class name:

```text
openhands.MessageEvent
openhands.ActionEvent
openhands.ObservationEvent
openhands.ConversationErrorEvent
```

The payload contains:

```json
{
  "event_type": "ActionEvent",
  "source": "agent",
  "sdk_event_id": "...",
  "sdk_timestamp": "...",
  "raw": {},
  "preview": "human readable SDK visualize text"
}
```

Frontend guidance:

- Use `kind` to branch UI rendering.
- Use `data.preview` for a quick human-readable timeline display.
- Use `data.raw` for detailed tool call, observation, message, source, and IDs.
- Render `openhands.ActionEvent` as tool/action start.
- Render `openhands.ObservationEvent` as tool/action result.
- Render `openhands.MessageEvent` as user/agent/environment message.
- Render `openhands.*Error*` as error rows.

## Critical OpenHands SDK Integration Notes

### Prompt Execution

OpenHands SDK 1.29 local `Conversation.run()` is a no-argument method. The user prompt must be sent first:

```python
conversation.send_message(prompt)
conversation.run()
```

Do not rely on `conversation.run(prompt)`. That silently caused the prompt to be lost after a `TypeError` fallback and made the agent ask what task to do instead of executing the user request.

### Initial Message

Do not pass `initial_message` into `ConversationSettings` for local SDK construction in this adapter. The adapter sends the initial user message through `send_message()` after conversation creation. Passing it into settings can cause the SDK to consume it during construction and trigger an empty corrective run before our explicit send path.

### Workspace Binding

The preferred OpenHands 1.29 constructor path is direct kwargs:

```python
Conversation(
    agent=agent,
    workspace=workspace,
    conversation_id=conversation_id,
    callbacks=[event_callback],
)
```

Fallback constructor attempts for `start_request`, `settings`, and bare `Conversation(agent)` still exist for SDK compatibility, but direct kwargs should remain first. This preserves `/workspace` binding and raw SDK event callbacks.

## Docker Build Notes

`Dockerfile.sandbox` intentionally avoids `pip install .` because that triggers PEP 517 build isolation and downloads the local project build backend `hatchling` during image build.

Instead it:

- Sets `PYTHONPATH=/opt/mghands/src`.
- Installs runtime dependencies explicitly.
- Runs `python -m uvicorn mghands_sandbox.app:app ...` against source code.
- Uses the Tsinghua PyPI mirror with `-i https://pypi.tuna.tsinghua.edu.cn/simple`.
- Pins/constraints dependency versions to reduce resolver backtracking and conflicts.

Important dependency constraints currently include:

```text
openhands-agent-server==1.29.0
openhands-sdk==1.29.0
openhands-tools==1.29.0
uvicorn[standard]==0.35.0
fastmcp==3.4.0
fastmcp-slim[client,server]==3.4.0
browser-use==0.11.13
```

The `uvicorn` version must satisfy `fastmcp-slim[server]`, which requires `uvicorn>=0.35`.

## Known Current Limitations

- Gateway `/execute` currently waits for sandbox execution to complete instead of immediately returning a background task ID.
- Gateway SSE is event-level streaming, not token-level streaming.
- OpenHands raw events are now captured, but frontend normalization is still the client's responsibility.
- Successful completion currently appears through wrapper events such as `agent.result` and OpenHands events; a dedicated Gateway `completed` terminal SSE event may still be useful.
- Browser tools are disabled by default because Chromium/Playwright dependencies are not installed in the sandbox image.
- LiteLLM cost-map warnings for custom model names are non-fatal and unrelated to execution.

## Recommended Frontend Timeline Mapping

Suggested UI mapping for current event records:

| Event kind | UI meaning |
| --- | --- |
| `message` | Gateway wrapper user message. |
| `agent.result` | Gateway wrapper run result. |
| `agent.error` | Gateway wrapper run error. |
| `openhands.MessageEvent` | OpenHands message event. |
| `openhands.ActionEvent` | Tool call/action proposed by agent. |
| `openhands.ObservationEvent` | Tool result/observation. |
| `openhands.AgentErrorEvent` | Agent-level error. |
| `openhands.ConversationErrorEvent` | Conversation-level error. |

Use `data.preview` when available for display text. Use `data.raw` for detailed expandable panels.

## Development And Validation

Run tests from repository root:

```powershell
pytest
```

Current tested behavior includes:

- Sandbox health and runtime APIs.
- LLM config serialization and redaction.
- OpenAI-compatible model prefixing.
- Direct OpenHands `Conversation(...)` constructor preference.
- Workspace/callback preservation.
- Prompt send-before-run behavior.
- Raw OpenHands event callback capture.
- Gateway session creation and session store behavior.

## Git Hygiene

- Keep commits focused.
- Do not commit `.mghands/`, `.pytest_cache/`, `__pycache__/`, or built images.
- Before push, run at least `pytest` for code changes.
- Docker build verification may happen remotely if Docker is unavailable locally.
