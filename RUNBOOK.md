# Mghands Gateway 运行手册

本文档说明如何在当前项目中运行带 OpenHands SDK 容器沙箱的轻量 Session Gateway。

## 1. 架构说明

Mghands Gateway 是一个独立 FastAPI 服务，对外提供简化 session/task API，对内为每个 session 启动或获取一个 Docker sandbox 容器。容器内运行 `mghands_sandbox`，由它懒加载官方 `openhands-sdk` 初始化会话并暴露标准接口。

执行路径如下：

```text
client
  -> Mghands Gateway /api/v1/sessions...
  -> per-session Docker sandbox container
  -> mghands_sandbox standard APIs
  -> openhands-sdk
```

Gateway 不在自身进程内直接运行裸 `Conversation.run()`。它负责容器生命周期和 session 映射，实际任务执行发生在隔离容器内的 OpenHands SDK/agent-server 中。

完整生命周期：

```text
用户发起请求
  -> Gateway 调度器校验 session_id 和 sandbox_type
  -> 创建或获取 session 级 Docker 容器
  -> 容器注入 OH_SESSION_API_KEYS_0
  -> 容器启动 mghands_sandbox FastAPI 服务
  -> Gateway 调用容器 /api/conversations
  -> 容器内懒加载 openhands-sdk 并初始化 SDK Conversation
  -> 用户后续 execute 请求通过 /api/conversations/{id}/events 交互
  -> 请求可动态携带 skills / mcp_config
  -> 容器通过 /api/conversations/{id}/runtime 热更新 skills / MCP
  -> history / SSE 从容器事件接口读取
  -> DELETE session 删除 conversation 并销毁 Docker 容器
```

调度器推荐调用顺序：

```text
1. docker run 启动容器
2. GET /alive 确认 HTTP 服务可达
3. GET /ready 确认 runtime 可调度
4. GET /server_info 读取能力声明
5. POST /api/conversations 创建 SDK 会话
6. POST /api/conversations/{id}/events 发送用户消息
7. GET /api/conversations/{id}/events/search 查询历史
8. POST /api/conversations/{id}/runtime 热更新 skills/MCP
9. POST /api/shutdown 或 docker rm -f 销毁容器
```

## 容器内标准接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/alive` | 轻量探活，确认 HTTP 服务可达 |
| `GET` | `/ready` | 调度就绪状态，返回 SDK 可用性、conversation 数、当前状态 |
| `GET` | `/server_info` | 返回 API 版本、能力声明、标准端点列表 |
| `GET` | `/api/runtime` | 返回容器 runtime 状态，需要 session key |
| `POST` | `/api/conversations` | 初始化 OpenHands SDK conversation |
| `GET` | `/api/conversations?ids=...` | 批量查询 conversation |
| `GET` | `/api/conversations/{id}` | 查询单个 conversation |
| `DELETE` | `/api/conversations/{id}` | 删除 conversation |
| `POST` | `/api/conversations/{id}/events` | 发送用户消息并可触发 SDK run |
| `GET` | `/api/conversations/{id}/events/search` | 分页查询事件 |
| `GET` | `/api/conversations/{id}/runtime` | 查询该 conversation 的 skills/MCP/事件计数 |
| `POST` | `/api/conversations/{id}/runtime` | 热更新 skills/MCP，并重建 SDK runtime 绑定 |
| `GET` | `/api/skills` | 查询当前 skills |
| `POST` | `/api/shutdown` | 容器内服务自退出，供调度器优雅销毁 |

除 `/alive`、`/ready`、`/server_info` 外，其余接口默认使用 `X-Session-API-Key` 校验。Gateway 启动容器时会通过 `OH_SESSION_API_KEYS_0` 注入该 key。

容器创建 SDK 会话时会默认装配 OpenHands 官方编码能力。当前装配策略优先使用：

```text
openhands.tools.register_builtins_agents(enable_browser=$MGHANDS_ENABLE_BROWSER_TOOLS)
openhands.tools.get_default_tools(enable_browser=$MGHANDS_ENABLE_BROWSER_TOOLS, enable_sub_agents=True)
openhands.sdk.settings.OpenHandsAgentSettings
openhands.sdk.AgentContext
openhands.sdk.settings.ConversationSettings
```

默认关闭浏览器工具，因为它需要 Chromium。核心编码能力仍包括文件编辑、终端等工具。如果当前 SDK 版本缺少其中某个入口，容器会退回到最小 `Conversation` 构造路径。调度器可以通过 `GET /server_info` 查看 `default_coding_tools_enabled=true`、`browser_tools_enabled` 和 `default_tool_sources`，确认该容器声明的默认编码工具来源。

如需启用浏览器工具，需要在镜像内安装 Chromium/Playwright 依赖，并在启动容器时设置：

```bash
-e MGHANDS_ENABLE_BROWSER_TOOLS=true
```

## 2. 前置条件

1. Python 版本：`>=3.11`。
2. 当前目录：`D:\iso\Mghands`。
3. 本机或宿主机可执行 `docker` 命令。
4. 已构建本项目提供的 sandbox 容器镜像。
5. 容器内标准接口由 `mghands_sandbox` 提供，包含探活、元信息、runtime 状态、conversation、events、skills/MCP 更新和自销毁接口。

构建 sandbox 镜像：

```powershell
cd D:\iso\Mghands
docker build -f Dockerfile.sandbox -t mghands-sandbox:latest .
```

默认 sandbox 镜像可配置为：

```text
mghands-sandbox:latest
```

## 3. 安装依赖

在 PowerShell 中执行：

```powershell
cd D:\iso\Mghands
python -m pip install -e .[test]
```

项目依赖固定包含：

```text
openhands-sdk==1.29.0
openhands-agent-server==1.29.0
openhands-tools==1.29.0
```

不要改成宽松依赖，例如 `openhands>=0.1.0`。

## 4. 环境变量

### Sandbox 镜像

```powershell
$env:MGHANDS_SANDBOX_IMAGE="mghands-sandbox:latest"
```

默认容器命令：

```text
python -m uvicorn mghands_sandbox.app:app --host 0.0.0.0 --port 3000
```

如果你的容器需要自定义启动命令：

```powershell
$env:MGHANDS_SANDBOX_COMMAND="your agent server command"
```

### Sandbox 端口

容器内部 agent-server 默认端口：

```powershell
$env:MGHANDS_SANDBOX_INTERNAL_PORT="3000"
```

Gateway 会使用 Docker 随机发布到 `127.0.0.1` 的宿主端口，不要求你手工分配端口。

### Sandbox 工作区

每个 session 会挂载独立 workspace：

```powershell
$env:MGHANDS_SANDBOX_WORKSPACE_ROOT="D:\iso\Mghands\.mghands\workspaces"
$env:MGHANDS_SANDBOX_WORKSPACE_MOUNT_PATH="/workspace"
```

### Sandbox 资源限制

默认每个 session 容器会带资源限制：

```powershell
$env:MGHANDS_SANDBOX_MEMORY_LIMIT="2g"
$env:MGHANDS_SANDBOX_CPUS="2"
$env:MGHANDS_SANDBOX_PIDS_LIMIT="512"
```

Gateway 启动容器时还会附加：

```text
--security-opt no-new-privileges
```

### SQLite Session 映射存储路径

默认路径：

```text
.mghands/sessions.sqlite3
```

可覆盖为：

```powershell
$env:MGHANDS_DATABASE_PATH="D:\iso\Mghands\.mghands\sessions.sqlite3"
```

### Sandbox 隔离策略

默认只允许创建 `sandbox_type=docker` 的 session：

```text
MGHANDS_ALLOW_NON_DOCKER_SANDBOX=false
```

本地开发如果必须使用 process sandbox，可显式放开：

```powershell
$env:MGHANDS_ALLOW_NON_DOCKER_SANDBOX="true"
```

注意：当前实现实际只提供 Docker backend。`MGHANDS_ALLOW_NON_DOCKER_SANDBOX=true` 仅保留为本地开发扩展入口，不能用于生产安全隔离声明。

### SSE 参数

可选配置：

```powershell
$env:MGHANDS_SSE_POLL_SECONDS="1"
$env:MGHANDS_SSE_HEARTBEAT_SECONDS="15"
$env:MGHANDS_SSE_IDLE_TIMEOUT_SECONDS="300"
```

### Conversation 启动等待参数

可选配置：

```powershell
$env:MGHANDS_CONVERSATION_START_TIMEOUT_SECONDS="180"
$env:MGHANDS_CONVERSATION_START_POLL_SECONDS="2"
```

## 5. 启动服务

开发运行：

```powershell
cd D:\iso\Mghands
python -m uvicorn mghands_gateway.app:app --host 0.0.0.0 --port 8080 --reload
```

普通运行：

```powershell
cd D:\iso\Mghands
python -m uvicorn mghands_gateway.app:app --host 0.0.0.0 --port 8080
```

安装为 editable 后，也可以运行：

```powershell
mghands-gateway
```

健康检查：

```powershell
curl http://127.0.0.1:8080/health
```

预期返回：

```json
{"status":"ok"}
```

## 6. API 使用示例

以下示例假设 Gateway 运行在：

```text
http://127.0.0.1:8080
```

### 创建 Session

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/sessions `
  -H "Content-Type: application/json" `
  -d '{"session_id":"tenant-a-task-001","sandbox_type":"docker","workspace_policy":"isolated"}'
```

预期返回类似：

```json
{
  "session_id": "tenant-a-task-001",
  "sandbox_id": "...",
  "conversation_id": null,
  "status": "created",
  "created_at": "...",
  "updated_at": "...",
  "last_event_id": null,
  "error": null
}
```

### 查询 Session

```powershell
curl http://127.0.0.1:8080/api/v1/sessions/tenant-a-task-001
```

### 执行任务

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/sessions/tenant-a-task-001/execute `
  -H "Content-Type: application/json" `
  -d '{"task":"在当前目录创建 facts.txt 并写入 3 个物理常数","stream":true}'
```

### 使用请求级 LLM 覆写

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/sessions/tenant-a-task-001/execute `
  -H "Content-Type: application/json" `
  -d '{"task":"输出一句 hello","llm":{"model":"deepseek-chat","base_url":"https://api.deepseek.com/v1","api_key":"sk-..."},"stream":true}'
```

说明：

- `llm.model`、`llm.api_key` 和 `llm.base_url` 会传给容器内 SDK runtime。
- Gateway 不会把 `api_key` 写入 session 映射、history、SSE 或错误响应。

### 动态注入 Skills 和 MCP

首次创建 SDK 会话时注入：

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/sessions/tenant-a-task-001/execute `
  -H "Content-Type: application/json" `
  -d '{"task":"实现测试","skills":[{"name":"testing","content":"Always run pytest","triggers":["test"]}],"mcp_config":{"mcpServers":{"local":{"command":"echo"}}}}'
```

已有 conversation 后再次 `execute` 携带 `skills` 或 `mcp_config`，Gateway 会先调用容器：

```text
POST /api/conversations/{conversation_id}/runtime
```

然后再发送用户消息，因此新的 skills/MCP 对后续交互生效。

### 查询 History

```powershell
curl "http://127.0.0.1:8080/api/v1/sessions/tenant-a-task-001/history?limit=100"
```

分页查询：

```powershell
curl "http://127.0.0.1:8080/api/v1/sessions/tenant-a-task-001/history?page_id=100&limit=100"
```

### SSE Streaming

```powershell
curl -N http://127.0.0.1:8080/api/v1/sessions/tenant-a-task-001/stream
```

断点续连：

```powershell
curl -N "http://127.0.0.1:8080/api/v1/sessions/tenant-a-task-001/stream?after=<event_id>"
```

SSE 会发送：

```text
id: <event_id>
event: message
data: {...}
```

心跳格式：

```text
: heartbeat
```

### 刷新 Skills / MCP

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/sessions/tenant-a-task-001/skills/reload `
  -H "Content-Type: application/json" `
  -d '{"skills":[{"name":"testing","content":"Always run pytest"}],"mcp_config":{"mcpServers":{"local":{"command":"echo"}}}}'
```

该接口会调用容器 `/api/conversations/{conversation_id}/runtime` 热更新 skills/MCP，然后返回当前可见 skills 列表。

### 删除 Session

```powershell
curl -X DELETE http://127.0.0.1:8080/api/v1/sessions/tenant-a-task-001
```

删除时会尽量清理关联 conversation 或 sandbox，并将本地 session 状态标记为 `deleted`。

## 7. 测试与验证

运行单元测试：

```powershell
cd D:\iso\Mghands
pytest
```

当前验证结果：

```text
10 passed
```

运行编译检查：

```powershell
python -m compileall src tests
```

导入 FastAPI app：

```powershell
$env:PYTHONPATH="src"
python -c "from mghands_gateway.app import app; print(app.title)"
```

预期输出：

```text
Mghands OpenHands Gateway
```

## 8. 常见故障排查

### `ModuleNotFoundError: No module named 'mghands_gateway'`

原因：项目未安装或 `src` 未加入 Python 路径。

处理：

```powershell
python -m pip install -e .[test]
```

或临时设置：

```powershell
$env:PYTHONPATH="src"
```

### `Only docker sandbox sessions are allowed by default`

原因：请求使用了 `sandbox_type=process` 或 `sandbox_type=remote`，但 Gateway 默认只允许 Docker sandbox。

处理：生产环境改用：

```json
{"sandbox_type":"docker"}
```

本地开发临时放开：

```powershell
$env:MGHANDS_ALLOW_NON_DOCKER_SANDBOX="true"
```

### `Docker command failed`

原因：Gateway 无法启动或检查 sandbox 容器。

检查：

```powershell
docker version
docker image inspect $env:MGHANDS_SANDBOX_IMAGE
docker ps -a --filter "name=mghands-"
```

处理：确认 Docker daemon 运行、镜像存在、端口可发布、workspace 路径可挂载。

### `Sandbox did not become ready`

原因：容器启动了，但 `/alive` 在超时内不可用。

检查：

```powershell
docker logs <container_name>
docker port <container_name> 3000
```

处理：确认容器镜像的 agent-server 命令、内部端口、`MGHANDS_SANDBOX_INTERNAL_PORT`、`MGHANDS_SANDBOX_COMMAND` 配置。

### `session_id already exists`

原因：同名 session 已存在于 SQLite 映射中。

处理：换一个 `session_id`，或删除旧 session：

```powershell
curl -X DELETE http://127.0.0.1:8080/api/v1/sessions/<session_id>
```

### `session has no conversation yet`

原因：只创建了 session，还没有执行过任务，因此没有 `conversation_id`。

处理：先调用：

```powershell
POST /api/v1/sessions/{session_id}/execute
```

### SSE 一直只有 heartbeat

原因：OpenHands event service 暂无新增事件，或任务尚未启动成功。

检查：

```powershell
curl http://127.0.0.1:8080/api/v1/sessions/<session_id>
curl http://127.0.0.1:8080/api/v1/sessions/<session_id>/history
```

### 请求级 LLM 配置未生效

当前 Gateway 对 `llm.model` 使用 OpenHands `llm_model` 字段，对 `llm.api_key` 和 `llm.base_url` 使用请求级 secrets 传递。若底层 OpenHands app-server 未消费这些 secrets，需要在 OpenHands 侧扩展 conversation start 或 LLM profile/switch LLM 逻辑。

## 9. 安全注意事项

1. `session_id` 只允许字母、数字、下划线和中划线，防止路径穿越。
2. 本地 SQLite 仅保存 session 映射，不保存 LLM API key。
3. Gateway 会对 history、skills、错误响应中的常见敏感字段做脱敏。
4. 每个 session 独立 Docker 容器和独立 workspace 目录。
5. Gateway 默认拒绝非 Docker sandbox。
6. 不要把 process sandbox 视为强安全隔离。
7. 生产部署应为 Gateway 配置鉴权、TLS、容器 CPU/内存/磁盘/进程数限制和日志脱敏。

## 10. 文件位置

主要实现文件：

```text
src/mghands_gateway/app.py
src/mghands_gateway/config.py
src/mghands_gateway/models.py
src/mghands_gateway/agent_client.py
src/mghands_gateway/sandbox_backend.py
src/mghands_gateway/session_store.py
```

测试文件：

```text
tests/test_models.py
tests/test_session_store.py
```

本地数据文件默认位置：

```text
.mghands/sessions.sqlite3
```
