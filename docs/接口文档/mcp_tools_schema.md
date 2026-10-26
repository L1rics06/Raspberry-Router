# Agent ⇄ 设备 MCP 工具契约（分工3 ⇄ 分工4 接口文档）

> **对接双方**：模块 C（Agent，`agent/`）与模块 D（设备适配，`devices/`）。
> Agent 对硬件**零感知**：只认"工具清单"，加设备/换设备不改 Agent 代码。
> 参考实现：`devices/mock_mcp_server.py`（模拟硬件，当前已在线跑通全链路）。
> 版本：v1.0 · 2026-10-26

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

## 3. 首批工具清单（v1，mock 已实现，D 按此用 gpiozero 替换）

| 工具 | 参数 | 返回示例 | 高危 |
| --- | --- | --- | --- |
| `led.set` | `on: bool` | `{"ok":true,"led":true,"detail":"LED 已点亮"}` | |
| `fan.set` | `state: bool` | `{"ok":true,"fan":true,"detail":"风扇已打开"}` | |
| `relay.set` | `ch: int(0-3), state: bool` | `{"ok":true,"ch":0,"state":true}` | ⚠️ 是 |
| `dht.read` | （无） | `{"temp":26.4,"hum":58}` | |
| `devices.status` | （无） | 全部设备当前状态快照（调试用） | |

高危说明：`relay.set` 在 Agent 配置的 `confirm_tools` 名单里——**聊天指令永远不执行它**，只有手动控制台带 `confirmed=true` 的直控请求才放行。新增高危工具时同步改 `agent/config.toml`。

## 4. 真机替换指引（分工 D）

| 事项 | 要求 |
| --- | --- |
| GPIO 库 | 只用 `gpiozero`（Pi 5 兼容，lgpio 后端），**禁用 RPi.GPIO** |
| I2C（OLED/AHT20） | `smbus2`，先 `raspi-config` 开 I2C |
| 电平 | GPIO 坚持 3.3V；5V 信号（HC-SR04 Echo）分压；共地；**不碰 220V** |
| 位置 | 真机实现放 `devices/device_mcp_server.py`，保持上表工具名与参数不变，Agent 与上层零改动 |
| 自测 | `python devices/mock_mcp_server.py` 换成你的实现后，直接跑 `scripts/e2e_test.py` 全绿即接入成功 |

## 5. 验收标准（分工 D 的 DoD）

1. 树莓派上 `python device_mcp_server.py` 可独立启动不报错；
2. Agent 在线后，前端调试控制台（网关根路径，地址见团队群）手动按钮能驱动真实硬件；
3. `scripts/e2e_test.py` 对真机全部通过。
