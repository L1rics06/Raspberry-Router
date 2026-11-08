# PiAgent —— 基于树莓派的 AI Agent 物联网控制平台

> 创客课程设计 · 在树莓派上部署 AI Agent，把继电器、传感器等物理设备封装成 MCP 工具；
> 用户在任何地方说一句"把风扇打开"，指令经云端 VPS 中继送达树莓派，由 Agent 自主理解并驱动真实硬件。

## 当前进度（分工4集成版，2026-11-04）

| 模块 | 分工 | 状态 |
| --- | --- | --- |
| VPS 通信网关（`server/`） | 2 | ✅ **完成并部署**（systemd 常驻；LLM 出口代理已接入 DeepSeek `deepseek-flash`） |
| 树莓派 Agent（`agent/`） | 3 | ✅ **完成**（手写 tool-use 循环 + MCP 客户端 + 安全闸门，systemd 就绪） |
| 设备 MCP Server（`devices/`） | 4 | 软件及离线验收已完成：SG90墙灯硬件PWM、标定、MCP、部署和E2E；真机接线、标定及现场验收待执行 |
| 用户界面（`frontend/`） | 1 | ✅ **完成**（无构建纯静态 SPA：登录注册 / 设备面板 / 对话 / 手动控制 / 历史，由网关托管） |

分工4本次验证：86项单元测试、50项本机链路检查通过；真实网关代码、修复版Agent、正式舵机算法的sim后端和规则模型参与测试，不代表实际GPIO或团队真实模型已验收。其余模块状态沿用原项目自述。

> LLM 工具名兼容说明：DeepSeek 严格要求函数名 `[a-zA-Z0-9_-]`，Agent 已自动做
> `fan.set ⇄ fan_set` 双向映射，设备侧无需任何改动（见 `mcp_tools_schema.md` §3）。

## 分工4：SG90寝室开关

先看 [完整上机步骤](docs/01_使用步骤.md) 与 [交付及协作](docs/09_最终交付与仓库集成.md)。

- SG90红线接Pi5物理2脚5V，棕/黑线接物理6脚GND，橙/白线接物理32脚BCM12
- 使用RP1 PWM0通道0、50Hz硬件PWM，启动不输出；先空载，再现场标定home/on/off
- 开灯调用light.set({"on":true})，关灯为false；没有灯光反馈，lamp_state保持unknown
- 单舵机只启动wall_light_mcp.py；同时用原设备则只启动all_devices_mcp.py，避免devices.status重名
- 独立上机包及本集成版均已包含Agent错误传播修复；不需要改VPS或前端业务代码
- `python scripts/verify.py` 可复验全部软件检查；实际接线后才做GPIO动作、机构标定、10轮及现场网关联调

原项目PDF/PPT是早期材料，当前SG90接口与验收以本README和链接的Markdown说明为准。

## 架构

```
用户 ──HTTP/WS──> nginx(80) ──/piagent/──> VPS网关(uvicorn:8000)
                                            │  鉴权·设备管理·消息路由·LLM出口代理
                                            │ WS 出站长连接（穿透NAT）
                                     树莓派 Agent（收指令→LLM→调工具→汇报）
                                            │ MCP (stdio)
                                     设备 MCP Server（LED/风扇/继电器/温湿度）
                                            │ GPIO/I2C/PWM
                                     物理世界
```

## 目录

```
frontend/   分工1：无构建纯静态 SPA（登录注册/设备面板/对话/手动控制/历史；scripts/sync_frontend.sh 同步到 server/static/）
server/     分工2：FastAPI 网关（REST + 双 WS 通道 + SQLite + LLM 代理 + 托管前端；static/debug.html 为联调控制台）
agent/      分工3：Agent 守护进程（WS 客户端 + MCP 池 + 手写 ReAct 循环 + 安全闸门 + LLM 工具名映射）
devices/    分工4：wall_light_mcp.py 为SG90入口；all_devices_mcp.py 合并原五工具；原device_mcp_server.py继续提供LED等设备
scripts/    e2e_test.py 明确设备的SG90验收，默认只读；verify.py 软件回归；sync_frontend.sh 前端同步
docs/       项目文档 + 接口文档（api_frontend / protocol_pi_vps / mcp_tools_schema）+ 部署指南
```

## 快速开始

**线上环境（已部署，直接用）**

- 用户界面 / 调试控制台：网关根路径为正式 UI，`/debug.html` 为联调控制台（地址与访问令牌见团队群，不写入公开仓库）
- 登录方式：**访问令牌**（`pat-xxx`，运维 `manage.py add-user`/`token` 签发与轮换；注册与密码登录已关闭）
- SG90验收：`python scripts/e2e_test.py --profile profiles/hardware.local.toml`，令牌从TASK4_ACCESS_TOKEN环境变量读取；动作另加--actuate

**本地全栈（三步）**

```bash
python3 -m venv .venv && .venv/bin/pip install -r server/requirements.txt -r agent/requirements.txt
cd server && ../.venv/bin/python manage.py add-user demo   # 记下打印的 pat- 访问令牌
../.venv/bin/python manage.py add-device pi-dorm-01 --owner demo
set -a; source server.env.example 里填好的三个 LLM 变量; set +a   # 或直接跳过（llm_mode="mock"）
../.venv/bin/uvicorn app.main:app --port 8000 &          # 网关
cd ../agent && cp config.example.toml config.toml        # 填 token、llm_mode="mock"、mcp.command 用 venv python 绝对路径
../.venv/bin/python -m pi_agent &                        # Agent
```

浏览器开 <http://127.0.0.1:8000/> 即为正式用户界面（联调控制台在 `/debug.html`）。
要接真实大模型：复制 `server/server.env.example` 填好三个 `PIAGENT_LLM_*` 变量后 source 启动网关，
Agent 配置 `llm_mode = "proxy"`。

## 接口契约（冻结）

| 边界 | 文档 | 交接对象 |
| --- | --- | --- |
| 界面 ⇄ VPS | [docs/接口文档/api_frontend.md](docs/接口文档/api_frontend.md) | 分工1 |
| 树莓派 ⇄ VPS | [docs/接口文档/protocol_pi_vps.md](docs/接口文档/protocol_pi_vps.md) | 分工2/3 |
| Agent ⇄ 设备 | [docs/接口文档/mcp_tools_schema.md](docs/接口文档/mcp_tools_schema.md) | 分工4 |
| 部署运维 | [docs/部署指南.md](docs/部署指南.md) | 全员 |

## 设备环境

树莓派 5（aarch64，Pi OS）· 阿里云 VPS（Debian 13）
