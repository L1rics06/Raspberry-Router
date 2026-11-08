"""Agent 配置：TOML 文件 + 环境变量覆盖。"""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class MCPServerCfg:
    name: str
    command: str
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)


@dataclass
class Config:
    server_url: str = "ws://127.0.0.1:8000"
    device_id: str = ""
    device_token: str = ""
    llm_mode: str = "proxy"          # proxy | mock
    llm_model: str | None = None     # 覆盖服务端默认模型
    max_steps: int = 6
    allow_tools: list = field(default_factory=lambda: ["*"])
    confirm_tools: list = field(default_factory=lambda: ["relay.set"])
    telemetry_tool: str = "dht.read"
    mcp_servers: list = field(default_factory=list)


def load_config(path: str | None = None) -> Config:
    path = path or os.getenv("PIAGENT_CONFIG", "config.toml")
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"配置文件不存在: {p.resolve()}\n"
            f"请复制 config.example.toml 为 config.toml 并填写（不要提交到 git）"
        )
    raw = tomllib.loads(p.read_text(encoding="utf-8"))

    cfg = Config()
    for k in ("server_url", "device_id", "device_token", "llm_mode",
              "llm_model", "telemetry_tool"):
        if k in raw and raw[k] is not None:
            setattr(cfg, k, raw[k])
    cfg.max_steps = int(raw.get("max_steps", cfg.max_steps))
    cfg.allow_tools = list(raw.get("allow_tools", cfg.allow_tools))
    cfg.confirm_tools = list(raw.get("confirm_tools", cfg.confirm_tools))

    # 环境变量覆盖（systemd/容器场景更方便）
    cfg.server_url = os.getenv("PIAGENT_SERVER_URL", cfg.server_url)
    cfg.device_id = os.getenv("PIAGENT_DEVICE_ID", cfg.device_id)
    cfg.device_token = os.getenv("PIAGENT_DEVICE_TOKEN", cfg.device_token)
    cfg.llm_mode = os.getenv("PIAGENT_LLM_MODE", cfg.llm_mode)

    for srv in raw.get("mcp", {}).get("servers", []):
        cfg.mcp_servers.append(MCPServerCfg(
            name=srv["name"], command=srv["command"],
            args=list(srv.get("args", [])), env=dict(srv.get("env", {})),
        ))

    if not cfg.device_id or not cfg.device_token:
        raise ConfigError("device_id / device_token 未配置")
    if cfg.llm_mode not in ("proxy", "mock"):
        raise ConfigError("llm_mode 只能是 proxy 或 mock")
    return cfg
