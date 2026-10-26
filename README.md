# PiAgent —— 基于树莓派的 AI Agent 物联网控制平台

> 创客课程设计 · 在树莓派上部署 AI Agent，把继电器、传感器等物理设备封装成 MCP 工具；
> 用户在任何地方说一句"把风扇打开"，指令经云端 VPS 中继送达树莓派，由 Agent 自主理解并驱动真实硬件。

## 当前进度（2026-10-26）

| 模块 | 分工 | 状态 |
| --- | --- | --- |
| VPS 通信网关（`server/`） | 2 | ✅ **完成并部署**（systemd 常驻，地址见团队群） |
| 树莓派 Agent（`agent/`） | 3 | ✅ **完成**（手写 tool-use 循环 + MCP 客户端 + 安全闸门，systemd 就绪） |
| 模拟设备 MCP Server（`devices/`） | 4 占位 | ✅ mock 版在线（真机版待分工4 用 gpiozero 替换） |
| 用户界面（`frontend/`） | 1 | ⬜ 未开始（接口文档已冻结交付：`docs/接口文档/api_frontend.md`） |

**全链路已跑通**：本机模拟树莓派 --公网 WS--> VPS 网关 <--REST/WS-- 用户；"把风扇打开" → Agent 调 LLM → MCP 工具 → 结果回传，端到端验收 10/10 通过（`scripts/e2e_test.py`）。
当前 LLM 为 mock 规则模式（VPS 尚未配 Key，配上即切真实大模型，见部署指南）。

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
server/     分工2：FastAPI 网关（REST + 双 WS 通道 + SQLite + LLM 代理 + 调试控制台）
agent/      分工3：Agent 守护进程（WS 客户端 + MCP 池 + 手写 ReAct 循环 + 安全闸门）
devices/    分工4：mock_mcp_server.py 模拟设备（真机版按同名工具替换）
scripts/    e2e_test.py 端到端验收脚本
docs/       项目文档 + 接口文档（api_frontend / protocol_pi_vps / mcp_tools_schema）+ 部署指南
```

## 快速开始

**线上环境（已部署，直接用）**

- 调试控制台：网关根路径（地址与联调账号见团队群，不写入公开仓库）
- 端到端验收：`python scripts/e2e_test.py <网关地址> <用户名> <密码>`

**本地全栈（三步）**

```bash
python3 -m venv .venv && .venv/bin/pip install -r server/requirements.txt -r agent/requirements.txt
cd server && ../.venv/bin/python manage.py add-user demo demo123456 && ../.venv/bin/python manage.py add-device pi-dorm-01 --owner demo
../.venv/bin/uvicorn app.main:app --port 8000 &          # 网关
cd ../agent && cp config.example.toml config.toml        # 填 token、llm_mode="mock"、mcp.command 用 venv python 绝对路径
../.venv/bin/python -m pi_agent &                        # Agent
```

浏览器开 <http://127.0.0.1:8000/> 即为调试控制台。

## 接口契约（冻结）

| 边界 | 文档 | 交接对象 |
| --- | --- | --- |
| 界面 ⇄ VPS | [docs/接口文档/api_frontend.md](docs/接口文档/api_frontend.md) | 分工1 |
| 树莓派 ⇄ VPS | [docs/接口文档/protocol_pi_vps.md](docs/接口文档/protocol_pi_vps.md) | 分工2/3 |
| Agent ⇄ 设备 | [docs/接口文档/mcp_tools_schema.md](docs/接口文档/mcp_tools_schema.md) | 分工4 |
| 部署运维 | [docs/部署指南.md](docs/部署指南.md) | 全员 |

## 设备环境

树莓派 5（aarch64，Pi OS）· 阿里云 VPS（Debian 13）
