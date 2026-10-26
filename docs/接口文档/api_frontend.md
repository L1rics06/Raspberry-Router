# 界面 ⇄ VPS 接口文档（分工1 交接文档）

> **交接对象：模块 A（用户界面）**。本文档由模块 B（VPS 通信网关）提供并冻结，前端按此开发，无需等待后端。
> 版本：v1.0 · 2026-10-26 · 对应网关代码 `server/`

## 0. 基础信息

| 项 | 值 |
| --- | --- |
| 生产地址 | `http://<VPS地址>/piagent`（nginx 反代 → 网关 :8000；实际地址与账号见团队群） |
| 本地联调 | `http://127.0.0.1:8000`（`server/` 目录 `uvicorn app.main:app`） |
| 协议 | HTTP REST（JSON）+ WebSocket，全部 UTF-8 |
| 鉴权 | JWT Bearer：登录后拿 `token`，之后每个请求带 `Authorization: Bearer <token>` |
| Token 有效期 | 7 天，过期返回 401，前端静默重新登录即可 |
| CORS | 已全开放（开发期 vite 热更新无障碍），生产可收紧 |
| 调试控制台 | 浏览器打开生产地址根路径即可（内置联调页面，登录+聊天+手动控制） |
| 体验账号 | 见团队群（联调账号已绑定测试设备 `pi-dorm-01`） |

30 秒上手（curl）：

```bash
BASE=http://<VPS地址>/piagent     # 或本地 http://127.0.0.1:8000
TOKEN=$(curl -s $BASE/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"<用户名>","password":"<密码>"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s $BASE/api/devices -H "Authorization: Bearer $TOKEN"
```

---

## 1. REST API

### 1.1 注册 `POST /api/auth/register`

Body：`{"username": "alice", "password": "alice123"}`（用户名 2-32 字符，密码 6-64 字符）

- `201` `{"id": 2, "username": "alice", "token": "<jwt>"}`
- `409` `{"detail": "用户名已被占用"}` / `422` 字段不合法

注册即登录，直接返回 token。

### 1.2 登录 `POST /api/auth/login`

Body 同上。`200` 同上结构；`401` `{"detail": "用户名或密码错误"}`。

### 1.3 绑定设备 `POST /api/devices/bind` 🔒

用户凭**设备 token** 认领一台树莓派（设备 token 由运维在服务端 `manage.py add-device` 生成，交付时线下发给用户）。

Body：`{"device_id": "pi-dorm-01", "device_token": "dev-xxxx"}`

- `200` `{"device_id": "pi-dorm-01", "owner": "demo"}`
- `404` 设备不存在或 token 错误；`409` 已被其他用户绑定

> 演示模式（P1）可用"扫码=打开 `bind?device_token=...` 链接"实现，接口不变。

### 1.4 设备列表 `GET /api/devices` 🔒

`200` 返回数组（当前用户绑定的所有设备）：

```json
[{
  "device_id": "pi-dorm-01",
  "online": true,
  "caps": ["led.set", "fan.set", "relay.set", "dht.read", "devices.status"],
  "last_seen": 1793000000.0,
  "telemetry": {"temp": 26.4, "hum": 58},
  "bound": true
}]
```

| 字段 | 说明 |
| --- | --- |
| `online` | WS 长连接是否在线（**设备面板的在线状态就用它**） |
| `caps` | 设备当前注册的 MCP 工具清单（手动控制台可据此渲染按钮） |
| `telemetry` | 最近一次心跳上报的遥测（温度/湿度等，随心跳每 15s 刷新） |
| `last_seen` | Unix 时间戳（秒） |

### 1.5 手动直控 `POST /api/devices/{device_id}/command` 🔒

**绕过 Agent/LLM，直接调用设备上的 MCP 工具**——手动控制台的兜底通道，也是联调利器。

Body：

```json
{
  "tool": "fan.set",
  "args": {"state": true},
  "confirmed": false,
  "wait": true
}
```

| 字段 | 说明 |
| --- | --- |
| `tool` | 工具名，取自 `caps`，如 `led.set` / `fan.set` / `dht.read` |
| `args` | 工具参数对象（schema 见 `mcp_tools_schema.md`） |
| `confirmed` | 高危工具（如 `relay.set`）必须传 `true` 才执行，否则 Agent 安全闸门拒绝 |
| `wait` | `true`（默认）阻塞等结果，方便调试；`false` 立即返回 cmd_id |

响应（wait=true 时）：

```json
{
  "cmd_id": "c-1a2b3c4d5e",
  "status": "done",
  "type": "result", "id": "c-1a2b3c4d5e",
  "ok": true,
  "text": "{\"ok\": true, \"fan\": true, \"detail\": \"风扇已打开\"}",
  "actions": ["fan.set({\"state\": true})"]
}
```

- `ok=false` 且 text 含"确认"→ 高危未确认；`ok=false` 其他 → 执行失败，`text` 是原因
- `400` 设备未绑定 / `503` 设备离线（`detail` 中带 `[code=...]`）

### 1.6 历史记录 `GET /api/history?device_id=&limit=50` 🔒

`200` 数组，按时间倒序（指令与执行审计日志，P1 历史日志页数据源）：

```json
[{
  "cmd_id": "c-1a2b3c4d5e", "device_id": "pi-dorm-01", "kind": "chat",
  "payload": {"kind": "chat", "text": "把风扇打开"},
  "ok": true, "result_text": "已打开风扇",
  "actions": ["fan.set({\"state\": true})"],
  "created_at": 1793000000.0, "finished_at": 1793000002.3
}]
```

`kind`：`chat`（走 Agent）| `tool`（手动直控）。

### 1.7 健康检查 `GET /api/health`（无鉴权）

`{"status":"ok","devices_online":["pi-dorm-01"],"llm_proxy":"configured|not_configured"}`

### 错误格式约定

REST 错误统一为 FastAPI 风格：`{"detail": "人类可读信息"}`，HTTP 状态码语义化（401/404/409/422/503）。

---

## 2. WebSocket `/ws/user?token=<jwt>` 🔒

**对话页核心通道**：发自然语言指令，收 Agent 结果与各类推送事件。

连接：`ws://<VPS地址>/piagent/ws/user?token=<jwt>`（注意生产环境走 80 端口的 `/piagent` 前缀；本地为 `ws://127.0.0.1:8000/ws/user`）
连接失败：token 无效时服务端以 `4401` 关闭。

### 2.1 连接成功 → 服务端立即下发 hello 快照

```json
{
  "type": "hello", "user": {"id": 1, "username": "demo"},
  "devices": [ /* 与 GET /api/devices 相同结构的数组 */ ],
  "ts": 1793000000.0
}
```

前端可据此直接渲染设备面板初始状态，**无需再调一次 REST**。

### 2.2 客户端 → 服务端

**发指令（对话页）：**

```json
{"type": "chat", "text": "把风扇打开", "device_id": "pi-dorm-01"}
```

`device_id` 可省略（默认路由到第一台在线设备）。发送后先收到：

```json
{"type": "cmd_ack", "id": "c-1a2b3c4d5e", "device_id": "pi-dorm-01", "kind": "chat"}
```

（前端可显示"Agent 正在思考…"loading 态）

**心跳：** `{"type": "ping"}` → `{"type": "pong"}`（可选，链路保活用）

### 2.3 服务端 → 客户端（重点：result）

**Agent 执行结果**（对应一次 chat / 或直控回传）：

```json
{
  "type": "result", "id": "c-1a2b3c4d5e", "ok": true,
  "text": "已为你打开风扇",
  "actions": ["fan.set({\"state\": true})"]
}
```

- `text`：Agent 的自然语言回复（**直接渲染为聊天气泡**）
- `actions`：本次调用的工具轨迹（**建议渲染为"⚙ 已调用 fan.set"的过程行**——这是演示时评委最想看到的"Agent 在动手"）

**指令失败/超时：**

```json
{"type": "cmd_error", "id": "c-1a2b3c4d5e", "code": "timeout", "message": "设备响应超时"}
```

**遥测推送**（设备每 15s 心跳捎带，实时刷新传感器面板）：

```json
{"type": "telemetry", "device_id": "pi-dorm-01", "data": {"temp": 26.4, "hum": 58}, "ts": 1793000000.0}
```

**设备上下线**（演示"拔网线再插回"就靠它）：

```json
{"type": "device_status", "device_id": "pi-dorm-01", "online": false}
```

**通用错误：** `{"type": "error", "code": "device_offline", "message": "..."}`（chat 发出时设备不在线等场景）

### 2.4 错误码总表

| code | 场景 | 前端建议处理 |
| --- | --- | --- |
| `device_offline` | 设备离线时发指令 | 提示"设备不在线"，面板置灰 |
| `no_device` | 没有绑定任何设备 | 引导去绑定页 |
| `device_not_bound` | 指定了未绑定设备 | 提示并刷新设备列表 |
| `timeout` | 设备 90s（chat）/30s（tool）未回 | 提示重试 |
| `empty_text` | chat 文本为空 | 忽略即可 |
| `bad_json` / `unknown_type` | 客户端消息格式错误 | 开发期自查 |

### 2.5 前端实现建议

1. **断线重连**：WS 断开后指数退避重连（1s→2s→…→30s），重连成功用 hello 快照重置 UI；
2. **401 处理**：REST 401 或 WS 4401 → 清 token 回登录页；
3. **指令并发**：同一用户多条指令会排队在设备端串行执行，前端对同一设备的 chat 做 loading 锁即可；
4. **手动控制台**按钮用 §1.5 的 REST（`wait=true` 同步拿结果，比 WS 简单）。

---

## 3. 联调指南（写给分工 A）

1. **零后端依赖开发**：网关 24h 运行在生产地址（见团队群），测试设备 `pi-dorm-01`（模拟硬件）由分工 B/C 维护在线，联调账号进群索取；
2. **接口自测**：仓库根目录 `python scripts/e2e_test.py <网关地址> <用户名> <密码>` 覆盖了全部接口的期望行为（10 个断言），前端联调时可当"活的接口文档"逐条对照；
3. **调试控制台**：生产地址根路径 `/` 有内置联调页（登录/聊天/手动控制/事件流），前后端行为不一致时先看它；
4. **接口变更流程**：本文档即契约。需要新字段/新事件 → 在群里提 → B 更新文档与实现 → A 再跟进，避免口头约定。
