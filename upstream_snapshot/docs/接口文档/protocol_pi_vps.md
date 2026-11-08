# 树莓派 ⇄ VPS 消息协议（分工2 ⇄ 分工3 接口契约）

> **对接双方**：模块 B（VPS 网关，`server/`）与模块 C（树莓派 Agent，`agent/`）。
> 本文档在 `project_intro.md` §6 草案基础上补全了握手、超时、错误处理，已按此实现并冻结。
> 版本：v1.0 · 2026-10-26

## 1. 连接模型

- 树莓派作为**客户端主动出站**连接，天然穿透 NAT，无需公网 IP：

  ```
  ws://<VPS地址>/piagent/ws/device      # 生产（地址见团队群）
  ws://127.0.0.1:8000/ws/device           # 本地
  ```

- 一条双向长连接承载全部消息（JSON 文本帧，UTF-8）；
- 断线由 Agent 侧**指数退避重连**（1s → 2s → … 封顶 60s），网关侧心跳超时强制断开；
- 同一 `device_id` 重复上线：新连接顶替旧连接（旧连接收到 `error: replaced` 后被关闭）——Agent 重启场景天然幂等。

## 2. 消息总览

| 方向 | type | 说明 |
| --- | --- | --- |
| Pi → VPS | `register` | 连接后第一条，设备鉴权 + 能力上报 |
| VPS → Pi | `register_ack` | 注册确认，下发心跳周期 |
| Pi → VPS | `heartbeat` | 心跳（可捎带遥测） |
| VPS → Pi | `heartbeat_ack` | 心跳确认 |
| VPS → Pi | `cmd` | 指令下发（chat / tool 两种） |
| Pi → VPS | `result` | 执行结果（按 `id` 原路路由回发起用户） |
| 双向 | `error` | 错误通知 |

## 3. 消息定义

### 3.1 register（Pi → VPS，连接后 10s 内必须发送）

```json
{
  "type": "register",
  "device_id": "pi-dorm-01",
  "token": "dev-9f3c……（服务端 manage.py add-device 生成的一次性 token）",
  "caps": ["led.set", "fan.set", "relay.set", "dht.read"]
}
```

`caps` 是该设备的 MCP 工具清单（Agent 启动时从 MCP Server 动态发现），网关存库并通过 `GET /api/devices` 暴露给前端。

**鉴权失败** → 网关回 `{"type":"error","code":"auth_failed","message":"..."}` 并以 4401 关闭；Agent 收到 `auth_failed` 应视为致命错误（token 失效），退避长重连或退出告警。

### 3.2 register_ack（VPS → Pi）

```json
{"type": "register_ack", "ok": true, "heartbeat_interval": 15, "server_time": 1793000000.0}
```

Agent 以 `heartbeat_interval`（秒）为准发心跳（当前 15s）。

### 3.3 heartbeat（Pi → VPS）

```json
{"type": "heartbeat", "ts": 1793000000.0, "telemetry": {"temp": 26.4, "hum": 58}}
```

- `telemetry` 可选：有传感器工具时建议每次心跳读取并捎带，网关入库并实时推送给绑定用户（前端设备面板数据源）；
- 网关回 `{"type":"heartbeat_ack","ts":...}`；
- **超过 45s 无任何消息 → 网关判定离线**：强制断开连接、向绑定用户推送 `device_status: false`。

### 3.4 cmd（VPS → Pi）

**chat：用户自然语言指令，走 Agent 循环（LLM + MCP）**

```json
{"type": "cmd", "id": "c-1a2b3c4d5e", "payload": {"kind": "chat", "text": "把风扇打开"}}
```

**tool：手动控制台直控，绕过 LLM**

```json
{
  "type": "cmd", "id": "c-1a2b3c4d5e",
  "payload": {"kind": "tool", "tool": "fan.set", "args": {"state": true}, "confirmed": true}
}
```

- `id`：指令唯一标识，result 必须原样带回；
- `confirmed`：高危工具（默认 `relay.set`）二次确认标志，仅 tool 类携带；chat 类永远视为未确认（用户聊天不可能确认高危操作）；
- **超时**：网关等 result，chat 90s、tool 30s，超时向用户回 `cmd_error: timeout`（result 之后到达会被忽略并留档）。

### 3.5 result（Pi → VPS）

```json
{
  "type": "result", "id": "c-1a2b3c4d5e", "ok": true,
  "text": "已为你打开风扇",
  "actions": ["fan.set({\"state\": true})"]
}
```

- `ok=false`：执行失败/被安全闸门拒绝，`text` 为原因（会展示给用户，须人类可读）；
- `actions`：工具调用轨迹，纯展示用途；
- 网关按 `id` 找到发起方路由回用户 WS，并写入 history 审计表。

### 3.6 error

`{"type":"error","code":"...","message":"..."}`，常见 code：

| code | 方向 | 含义 |
| --- | --- | --- |
| `auth_failed` | VPS→Pi | 设备 token 无效（致命） |
| `replaced` | VPS→Pi | 被同名新连接顶替（正常，静默退出即可） |
| `expected_register` | VPS→Pi | 第一条消息不是 register |
| `bad_json` / `unknown_type` | VPS→Pi | 消息格式错误 |

## 4. 状态机（Agent 视角）

```
DISCONNECTED ──connect──> CONNECTING ──register+ack──> ONLINE
     ^                        │                          │  send fail / recv close /
     └──── backoff 1s..60s ◄──┴──────────────────────────┘  heartbeat_ack 超时
```

## 5. 实现参考

- 网关侧：`server/app/ws_device.py`（约 120 行）
- Agent 侧：`agent/pi_agent/ws_client.py`（含心跳、退避重连、指令串行化）
- 双端联调：`scripts/e2e_test.py`
