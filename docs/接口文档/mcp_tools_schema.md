# Agent ⇄ 设备 MCP 工具契约（分工3 ⇄ 分工4 接口文档）

> **对接双方**：模块 C（Agent，`agent/`）与模块 D（设备适配，`devices/`）。  
> Agent 对硬件**零感知**：只认"工具清单"，加设备/换设备不改 Agent 代码。  
> 正式实现：`devices/device_mcp_server.py`（真机 gpiozero + 无硬件自动仿真）；  
> 参考 mock：`devices/mock_mcp_server.py`；自检：`devices/mcp_selftest.py`（32/32 通过）。  
> 版本：v1.1 · 2026-11（v1.0 契约不变，仅补充实现与验收说明）

## 1. 接入方式

设备侧用 **MCP 官方 Python SDK** 写一个 stdio Server，Agent 按配置拉起子进程并动态发现工具：

```python
# 兼容 mcp SDK 1.x/2.x
try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ModuleNotFoundError:
    from mcp.server.fastmcp import FastMCP

mcp = FastMCP("devices")

@mcp.tool(name="fan.set", description="控制风扇。state=true 转动，false 停止。")
def fan_set(state: bool) -> str:
    ...
    return json.dumps({"ok": True, "fan": state})

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

Agent 侧配置（`agent/config.toml`，可挂多个 Server）：

```toml
[[mcp.servers]]
name = "devices"
command = "python3"
args = ["/opt/piagent/devices/device_mcp_server.py"]
```

## 2. 工具约定（契约核心）

1. **命名**：`<设备>.<动作>`，全小写点分，如 `fan.set` / `dht.read`；
2. **参数**：用 Python 类型标注（bool/int/float/str），SDK 自动生成 JSON Schema，Agent 侧据此做强校验；
3. **返回**：**JSON 字符串**。成功 `{"ok": true, ...}`；失败 `{"ok": false, "error": "原因"}`——Agent 会把内容原样交给 LLM 观察或回传用户，请保证可读；
4. **禁止**：工具内不得联网、不得长阻塞（>5s 的动作拆成"启动+查询"两个工具）；
5. **日志**：stdout 被 MCP 协议占用，**日志一律打 stderr**。

## 3. 首批工具清单（v1，`device_mcp_server.py` 已全部实现）

| 工具               | 参数                          | 返回示例                                                                                            | 高危   |
| ---------------- | --------------------------- | ----------------------------------------------------------------------------------------------- | ---- |
| `led.set`        | `on: bool`                  | `{"ok":true,"led":true,"detail":"LED 已点亮"}`                                                     |      |
| `fan.set`        | `state: bool`               | `{"ok":true,"fan":true,"detail":"风扇已打开"}`                                                       |      |
| `relay.set`      | `ch: int(0-3), state: bool` | `{"ok":true,"ch":0,"state":true}`                                                               | ⚠️ 是 |
| `dht.read`       | （无）                         | `{"ok":true,"temp":26.4,"hum":58}`                                                              |      |
| `devices.status` | （无）                         | `{"ok":true,"led":false,"fan":false,"relay":[...],"mode":"gpio","pins":{...},"errors":{}}`（调试用） |      |

高危说明：`relay.set` 在 Agent 配置的 `confirm_tools` 名单里——**聊天指令永远不执行它**，只有手动控制台带 `confirmed=true` 的直控请求才放行。新增高危工具时同步改 `agent/config.toml`。

## 4. 真机实现与部署（分工 D）

正式实现已在 `devices/device_mcp_server.py`，v1 契约的工具名、参数、返回格式全部保持不变，  
Agent 与上层零改动；`mock_mcp_server.py` 仅保留作对照。

| 事项         | 要求 / 现状                                                                   |
| ---------- | ------------------------------------------------------------------------- |
| GPIO 库     | 只用 `gpiozero`（Pi 5 兼容，lgpio 后端），**禁用 RPi.GPIO**                           |
| I2C（AHT20） | `smbus2`，先 `raspi-config` 开 I2C；地址 0x38                                   |
| 电平         | GPIO 坚持 3.3V；5V 信号（HC-SR04 Echo）分压；共地；**不碰 220V**                         |
| 引脚         | 默认 LED=BCM17、风扇=BCM27、继电器=BCM5/6/13/19，可用环境变量覆盖                           |
| 无硬件        | 自动进入仿真（非 Linux / 无 gpiozero），或 `--sim` 强制                                 |
| 故障隔离       | 单个引脚初始化失败只禁用该设备，`devices.status.errors` 中可见                               |
| 配置         | `PIAGENT_*` 环境变量（完整表见 `devices/README.md` §6），可在 `[[mcp.servers]].env` 里传 |

接入 Agent 只需把配置指向正式实现（绝对路径）：

```toml
[[mcp.servers]]
name = "devices"
command = "/opt/piagent/agent/.venv/bin/python"
args = ["/opt/piagent/devices/device_mcp_server.py"]
```

## 5. 验收标准（分工 D 的 DoD）

1. 协议自检全绿：`python devices/mcp_selftest.py --sim`（32 项，已在开发机通过）；
2. 树莓派上 `python devices/mcp_selftest.py --gpio` 全绿，工具能真实驱动硬件；
3. Agent 在线后，前端调试控制台（网关根路径，地址见团队群）手动按钮能驱动真实硬件；
4. `scripts/e2e_test.py` 对真机全部通过；
5. 接线图、引脚表、环境变量与排障说明齐全：`devices/README.md`。

## 6. 契约自检覆盖范围（`mcp_selftest.py`）

| 阶段         | 检查内容                                                |
| ---------- | --------------------------------------------------- |
| initialize | 握手成功、返回 `serverInfo`                                |
| tools/list | 5 个工具全部存在、有描述                                       |
| schema     | 参数名、JSON Schema 类型、必填项与本文档逐项一致                      |
| tools/call | `led/fan` 开与关、`relay` 通道 0、`dht` 数值范围、`status` 快照字段 |
| 防御         | 继电器通道越界、错误参数类型、未知工具均被拒绝                             |
| 联调         | 分工3 的 `MCPPool` 零改动列出并调用全部工具（已验证）                   |

MCP 原理、实现细节与答辩说明见 `docs/分工4-MCP搭建说明.md`。
