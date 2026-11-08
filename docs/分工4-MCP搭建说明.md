# 分工4 交付说明：设备 MCP Server 的搭建、原理与功能

> 2026-11-04 SG90更新：86项单元测试、50项本机闭环检查通过，前次两处验收缺口已修复；[当前上机步骤](01_使用步骤.md)、[交付与协作](09_最终交付与仓库集成.md)为最终执行依据。本文下方保留原五工具说明，真实GPIO/网关验收项不能由模拟结果代填。E2E凭证使用TASK4_ACCESS_TOKEN环境变量，不用用户名和密码。


> 模块 D（硬件适配组）· 交付文档 v1.0 · 2026-11
> 对应代码：`devices/device_mcp_server.py`、`devices/mcp_selftest.py`
> 接口契约：`docs/接口文档/mcp_tools_schema.md`

## 0. 一句话总结

我在树莓派上写了一个 **MCP Server**：把 LED、风扇、继电器、温湿度传感器变成 5 个
标准化的“工具”，Agent 通过 MCP 协议自动发现并调用它们——**加设备只是加一个工具，
Agent 代码零改动**。它已经通过 32 项协议级自检，并能被分工3 的 `MCPPool` 零改动拉起。

---

## 1. MCP 是什么

### 1.1 定义

**MCP（Model Context Protocol，模型上下文协议）** 是一个开放协议，用来把
**AI 应用（Agent）** 和 **外部能力（数据、工具、服务）** 连接起来。它由 Anthropic 在
2024 年 11 月开源，之后被大量 IDE、Agent 框架和设备厂商采用。

它规定了两件事：

1. **怎么描述能力**：一个 Server 有哪些工具、每个工具要什么参数、返回什么；
2. **怎么调用能力**：双方用 JSON-RPC 2.0 消息完成握手、发现、调用和错误传递。

一句话类比：**MCP 是 AI 世界的“USB-C 接口”**——以前每个 AI 应用要对接 N 个工具就要写
N 套适配代码；有了统一接口，工具做成标准 Server，任何支持 MCP 的 Agent 都能即插即用。

### 1.2 它解决什么问题

没有 MCP 时，工具和 Agent 是 **M×N 的网状耦合**：

```
Agent A ──私有代码── 工具1
Agent A ──私有代码── 工具2
Agent B ──另一套代码── 工具1
...
```

有 MCP 后变成 **M+N 的星形结构**：

```
Agent A ─┐                 ┌─ 设备 Server 1（LED/继电器）
Agent B ─┼── MCP 标准协议 ──┼─ 设备 Server 2（温湿度）
Agent C ─┘                 └─ 第三方 Server（搜索、数据库…）
```

对应到本课程设计：分工3 写 Agent，分工4 写设备 Server。**只要工具契约不变，
两边可以完全并行开发、独立替换**——这正是我们四个模块能解耦的基础。

### 1.3 三个角色

| 角色 | 在本项目里是谁 | 职责 |
| --- | --- | --- |
| **Host** | 分工3 的 Agent 进程 | 承载 LLM、决定何时调用工具 |
| **Client** | Agent 里的 `MCPPool` | 与 Server 建立连接、发 `tools/list` / `tools/call` |
| **Server** | `device_mcp_server.py` | 暴露工具，真正操作 GPIO/I2C |

> 本项目里 Host 和 Client 在同一个 Agent 进程内，Server 是被拉起的独立子进程。

### 1.4 三类能力（本项目的取舍）

MCP Server 可以暴露三类东西：

| 类型 | 含义 | 本项目 |
| --- | --- | --- |
| **Tools（工具）** | 可被模型调用的动作，如 `fan.set` | ✅ 全部用它 |
| **Resources（资源）** | 只读上下文，如文件、日志 | 暂不需要 |
| **Prompts（提示词模板）** | 预置的提示词 | 暂不需要 |

设备控制的本质是“执行动作并返回结果”，所以 5 个能力全部封装成 **Tools**。

### 1.5 传输方式

MCP 支持两种传输：

- **stdio**：Server 作为子进程，通过标准输入/输出收发 JSON-RPC，一行一条消息；
- **Streamable HTTP**：Server 是独立 HTTP 服务，适合远程/多客户端。

本项目用 **stdio**：设备和 Agent 在同一台树莓派上，stdio 最简单、最安全，
不需要开端口，也不怕被外部访问。**日志必须写 stderr，因为 stdout 被协议占用**——
这是新手最容易踩的坑。

---

## 2. MCP 与 Function Calling 的关系（答辩常问）

两者不冲突，是上下层关系：

- **Function Calling** 是“模型的能力”：模型输出一段结构化 JSON，表示“我想调用 `fan.set`，
  参数是 `{"state": true}`”；
- **MCP** 是“工具从哪来、怎么执行、结果怎么回”的标准协议。

在本项目里的配合是：

```
MCP Server 用 @mcp.tool 声明工具
        ↓  tools/list
Agent 的 MCPPool 把工具转成 OpenAI function-calling 的 tools 数组
        ↓  随对话发给 LLM
LLM 返回 tool_calls: fan.set({"state": true})
        ↓  安全闸门校验参数
Agent 通过 MCP tools/call 发给设备 Server
        ↓
GPIO 动作 → 结果 JSON → 回填给 LLM → 自然语言回答
```

所以 **MCP 让工具“可插拔”，Function Calling 让模型“会决定调用”**，二者缺一不可。

---

## 3. 在本项目中的位置

```
用户 ──WS──> VPS 网关（分工2） ──WS 出站──> 树莓派 Agent（分工3）
                                              │ ① 把工具清单交给 LLM
                                              │ ② LLM 决定调用 relay.set
                                              │ ③ MCP/JSON-RPC over stdio
                                     device_mcp_server.py（分工4）
                                              │ ④ GPIO / I2C / PWM
                                        物理设备：LED / 风扇 / 继电器 / AHT20
```

分工边界非常清晰：

- **向上**：只对 Agent 暴露 MCP 工具，不关心网络、用户、前端；
- **向下**：只关心引脚、电平、时序和电气安全；
- **契约**：`docs/接口文档/mcp_tools_schema.md`（第一周冻结，双方共同遵守）。

---

## 4. 一次工具调用的完整协议流程

下面用真实报文说明 MCP 在“打开风扇”时发生了什么（id 仅示意）。

### 4.1 启动与握手

Agent 按 `config.toml` 拉起子进程：

```toml
[[mcp.servers]]
name = "devices"
command = "/opt/piagent/agent/.venv/bin/python"
args = ["/opt/piagent/devices/device_mcp_server.py"]
```

随后发送 `initialize`：

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
  "protocolVersion":"2025-06-18",
  "capabilities":{},
  "clientInfo":{"name":"piagent","version":"0.1"}
}}
```

Server 返回自己的能力与身份：

```json
{"jsonrpc":"2.0","id":1,"result":{
  "protocolVersion":"2025-06-18",
  "capabilities":{"tools":{"listChanged":false}},
  "serverInfo":{"name":"piagent-devices","version":"..."}
}}
```

客户端再发一条 `notifications/initialized` 通知，握手完成。

### 4.2 发现工具

Agent 发 `tools/list`，Server 返回由 Python 类型标注**自动生成**的 JSON Schema：

```json
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
```

```json
{"jsonrpc":"2.0","id":2,"result":{"tools":[
  {"name":"fan.set",
   "description":"控制风扇（经继电器或 MOS 管驱动）。state=true 转动，false 停止。",
   "inputSchema":{"type":"object",
                  "properties":{"state":{"type":"boolean"}},
                  "required":["state"]}}
]}}
```

Agent 把这些工具转成 OpenAI tools schema，随对话发给大模型。

### 4.3 调用工具

模型决定调用后，Agent 发：

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
  "name":"fan.set","arguments":{"state":true}
}}
```

Server 执行 GPIO 动作，把结果作为**文本内容**返回：

```json
{"jsonrpc":"2.0","id":3,"result":{
  "content":[{"type":"text","text":"{\"ok\": true, \"fan\": true, \"detail\": \"风扇已打开\"}"}],
  "isError":false
}}
```

Agent 取出文本，原样交给 LLM 作为观察结果，LLM 再说“已为你打开风扇”。

### 4.4 错误怎么传

- **协议层错误**（未知工具、参数类型不合法）：返回 JSON-RPC error 或 `isError: true`；
- **业务层错误**（通道越界、传感器没接）：返回 `{"ok": false, "error": "原因"}`，
  Agent 会把原因交给 LLM 如实转述，不假装成功。

---

## 5. 我们的实现

### 5.1 代码结构

```python
mcp = FastMCP("piagent-devices")          # ① 创建 Server

@mcp.tool(name="fan.set", description="...")   # ② 注册工具，schema 自动生成
def fan_set(state: bool) -> str:
    ...
    return json.dumps({"ok": True, "fan": actual}, ensure_ascii=False)

mcp.run(transport="stdio")                # ③ 以 stdio 方式提供服务
```

真正的工程细节在工具函数背后：

| 设计 | 作用 |
| --- | --- |
| `GpioBackend` | 真机：gpiozero（Pi 5 走 lgpio），`DigitalOutputDevice`/`LED` |
| `SimBackend` | 无硬件：内存态 + 缓慢漂移的温湿度，电脑上也能跑全链路 |
| `DeviceConfig` | 引脚、有效电平、I2C 参数全部环境变量可配，换接线不改代码 |
| `_make()` 故障隔离 | 某个引脚初始化失败只禁用该设备，其余工具照常 |
| `threading.RLock` | MCP 同步工具在线程池执行，串行化 GPIO/I2C 访问 |
| `_ok/_err` | 统一 JSON 返回，异常绝不穿透到协议层 |
| 日志写 stderr | 不污染 stdout 的 JSON-RPC 通道 |
| `atexit` 清理 | 进程退出时关闭 GPIO / I2C，避免引脚悬空 |

### 5.2 为什么要有仿真模式

树莓派只有一台，四个人都要联调；真机也不适合天天插拔。仿真模式让：

- 分工1/2/3 在任何电脑上都能跑完整链路；
- CI 或答辩前可以快速回归；
- 真机接线前先用 `--sim` 验证协议和 Agent 逻辑，排除软件问题。

真机与仿真**共用同一套工具函数和返回格式**，所以“仿真通过”对真机很有参考价值。
但仿真通过 ≠ 真机通过，最终仍需 `--gpio` 上板验收。

### 5.3 双版本 SDK 兼容

`mcp` Python SDK 2.x 把 `FastMCP` 改名为 `MCPServer`，工具 schema 字段也从
`inputSchema` 变为 `input_schema`。代码做了兼容：

```python
try:      # SDK >= 2.0
    from mcp.server.mcpserver import MCPServer as FastMCP
except ModuleNotFoundError:   # SDK 1.x
    from mcp.server.fastmcp import FastMCP
```

Agent 侧的 `mcp_client.py` 同样对两个字段做了回退，保证 `pip install mcp` 升版本不会炸。

### 5.4 温湿度：为什么选 AHT20

项目文档推荐的候选是 AHT20（I2C）与 DHT22（单总线）。我们默认实现 AHT20：

- Pi 5 上 `Adafruit_DHT` / 老 `RPi.GPIO` 这类单总线方案兼容性差；
- AHT20 走标准 I2C，`smbus2` 直接读，稳定性好；
- 接线只占 SDA/SCL 两根线，不占额外 GPIO。

驱动按数据手册实现：上电等待 → 校准（0xBE）→ 触发测量（0xAC 0x33 0x00）→
等待 80ms → 读 7 字节 → 按 20 位原始值换算温度/湿度。传感器异常时返回
`{"ok": false, "error": "..."}`，不影响其他工具。

---

## 6. 工具契约与安全设计

| 工具 | 参数 | 成功返回 | 高危 |
| --- | --- | --- | --- |
| `led.set` | `on: bool` | `{"ok":true,"led":true,"detail":"..."}` | |
| `fan.set` | `state: bool` | `{"ok":true,"fan":true,"detail":"..."}` | |
| `relay.set` | `ch: int, state: bool` | `{"ok":true,"ch":0,"state":true,"detail":"..."}` | ⚠️ |
| `dht.read` | 无 | `{"ok":true,"temp":26.4,"hum":58.2}` | |
| `devices.status` | 无 | 状态快照 + `mode`/`pins`/`errors` | |

契约里的四条硬约定（见 `mcp_tools_schema.md` §2）：

1. 命名 `<设备>.<动作>`，全小写点分；
2. 参数用 Python 类型标注，SDK 自动生成 JSON Schema，Agent 侧据此强校验；
3. 返回**永远是 JSON 字符串**，成功 `ok:true`，失败 `ok:false + error`；
4. 工具内不联网、不长时间阻塞；日志只写 stderr。

安全是分层做的：

| 层 | 措施 | 在哪 |
| --- | --- | --- |
| 模型层 | 系统提示词约束“只做设备管家” | `agent_loop.py` |
| 参数层 | JSON Schema 强校验（jsonschema） | `safety.py` |
| 策略层 | 工具白名单 + 高危工具二次确认 | `safety.py` / `config.toml` |
| 设备层 | 通道越界检查、类型检查、故障隔离 | `device_mcp_server.py` |
| 电气层 | 3.3V 电平、共地、低压负载、不碰 220V | 接线规范 |

`relay.set` 被列进 `confirm_tools`：**聊天指令永远不执行它**，只有手动控制台带
`confirmed=true` 的直控请求才放行。这样即使 LLM 幻觉，也不会在聊天中误吸合大电流继电器。

---

## 7. 验证结果

### 7.1 协议级自检（无需硬件）

```bash
python devices/mcp_selftest.py --sim
```

实际输出（2026-11-02，Windows + mcp SDK 2.3.0，仿真模式）：

```
[1] MCP 握手 initialize            ✅ server = piagent-devices
[2] 工具发现 tools/list            ✅ 5 个工具全部存在
[3] 参数 schema 与契约一致          ✅ 15 项逐项比对
[4] 执行器调用 tools/call          ✅ led/fan 开关各两次
[5] 继电器通道 + 越界保护           ✅ ch=99 被拒绝
[6] 传感器与状态快照                ✅ 温湿度数值合理
[7] 协议层参数校验                  ✅ 错误类型/未知工具被拒绝

== 结果: 32 通过, 0 失败 ==
```

### 7.2 与分工3 的零改动联调

用分工3 的 `MCPPool` 直接拉起本 Server（只改 `args` 指向 `device_mcp_server.py`）：

```
tool_names: ['led.set', 'fan.set', 'relay.set', 'dht.read', 'devices.status']
fan.set -> {"ok": true, "fan": true, "detail": "风扇已打开"}
dht.read -> {"ok": true, "temp": 27.4, "hum": 59.5}
relay.set -> {"ok": true, "ch": 3, "state": true, "detail": "继电器通道 3 已闭合"}
```

说明：工具发现、OpenAI schema 转换、调用与返回解析都正常，**Agent 代码一行未改**。

### 7.3 上板后还需要做的（诚实说明）

- [ ] 树莓派真机接线，`python devices/mcp_selftest.py --gpio` 全绿；
- [ ] 接上 VPS 后跑 `scripts/e2e_test.py` 全链路验收；
- [ ] 用手机热点/4G 远程演示一次“千里之外开风扇”。

真机自检通过时会打印 `✅ 真机验收通过`，且状态快照里 `"mode": "gpio"`；
若显示 `"mode": "sim"`，说明仍是仿真、没有驱动 GPIO。还没接温湿度/继电器时，
可先跑 `--gpio --skip-dht --skip-relay` 验收其余部分（跳过项不计失败，最终需补跑全量）。

仿真验证解决的是“软件契约与协议正确”，真机验证解决的是“接线与驱动正确”，
两者都完成才算分工4 最终交付。

---

## 8. 怎么接进 Agent（给分工3 的操作说明）

只改 `agent/config.toml` 一处：

```toml
[[mcp.servers]]
name = "devices"
command = "/opt/piagent/agent/.venv/bin/python"                 # 必须绝对路径
args = ["/opt/piagent/devices/device_mcp_server.py"]      # 真机版
# 想回到 mock：把 args 换成 mock_mcp_server.py 即可
```

重启 Agent，日志出现 `MCP server 'devices' 已启动，工具: [...]` 与
`注册成功，工具能力已上报: [...]` 即接入成功。网关的设备 `caps` 里能看到这 5 个工具。

---

## 9. 常见问题（Q&A）

**Q1：为什么不直接在 Agent 里写 GPIO 代码？**
A：那样 Agent 就和具体硬件绑死了：换引脚、加设备都要改 Agent；而且 LLM 的工具清单
无法自动发现。MCP 把“设备能力”变成可插拔模块，Agent 保持“薄”，这也是项目亮点之一。

**Q2：MCP Server 一定要用 Python 写吗？**
A：不一定。MCP 是语言无关的协议，官方有 Python/TypeScript 等 SDK。本项目选 Python
是因为 gpiozero/smbus2 生态成熟，和 Agent 同语言也便于维护。

**Q3：一个 Agent 能挂多个设备 Server 吗？**
A：能。`config.toml` 里可以写多个 `[[mcp.servers]]`，`MCPPool` 会分别拉起并合并工具清单
（重名工具会告警）。比如以后可以加一个摄像头 Server、一个 MicroBit Server。

**Q4：工具变多了，LLM 会不会选错？**
A：工具名和 description 就是给模型看的“说明书”。所以我们坚持 `<设备>.<动作>` 命名、
写清楚参数含义，并控制单次暴露的工具数量；工具过多时可以按场景拆分多个 Server。

**Q5：MCP 会不会有安全风险？**
A：会，所以不能只靠协议。我们做了工具白名单、参数 schema 校验、高危工具二次确认、
设备层越界检查、电气隔离。MCP 本身不提供权限模型，权限必须由 Host（Agent）实现。

**Q6：为什么用 stdio 而不是 HTTP？**
A：同机部署，stdio 零端口、零网络攻击面、零配置；HTTP 适合设备与 Agent 不在同一台机器，
或一个 Server 服务多个 Agent 的场景。以后要做“多树莓派共享一个设备服务”时可切换。

**Q7：仿真模式会不会掩盖真机问题？**
A：会，所以它只用于软件联调，不能替代上板验收。我们的验收清单里真机自检和 e2e 都是
独立项，文档也明确写了“仿真通过 ≠ 真机通过”。

---

## 10. 局限与后续可以做的事

1. **舵机**：已新增SG90 RP1硬件PWM、标定和light.set，见上机步骤；真实按压仍待现场测试。原五工具继续保留。
2. **摄像头/OLED**：可新增 `camera.snap` / `oled.show` 工具，Server 结构已支持故障隔离。
3. **Resources / Prompts**：目前只用了 Tools；以后可把设备日志做成 Resource，把常用场景
   做成 Prompt 模板。
4. **多 Server 编排**：把“寝室设备”“教室设备”拆成两个 Server，由 Agent 按用户绑定选择。
5. **可观测性**：给工具调用加耗时、成功率指标，便于演示时展示“Agent 真的在动手”。

---

## 11. 复现命令清单

```bash
# 电脑上（仿真）
python3 -m venv .venv && .venv/bin/pip install -r devices/requirements.txt
.venv/bin/python devices/mcp_selftest.py --sim

# 树莓派上（真机；依赖装进 Agent 的 venv）
sudo raspi-config                       # 开启 I2C
i2cdetect -y 1                          # 确认 0x38
agent/.venv/bin/python devices/mcp_selftest.py --gpio

# 接入 Agent 后
journalctl -u piagent-agent -f          # 看 MCP server 启动与工具上报
.venv/bin/python scripts/e2e_test.py --profile profiles/hardware.local.toml --base-url <网关地址> --device-id <设备ID> --actuate --chat
```

## 12. 参考资料

- MCP 官方站点：<https://modelcontextprotocol.io/>
- MCP 规范：<https://modelcontextprotocol.io/specification>
- Python SDK：<https://github.com/modelcontextprotocol/python-sdk>
- gpiozero 文档：<https://gpiozero.readthedocs.io/>
- 项目内契约：`docs/接口文档/mcp_tools_schema.md`

