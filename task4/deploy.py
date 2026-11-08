"""生成独立Agent配置与systemd服务，不改写上游源码"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .common import ROOT, check, new_report
from .profile import preflight
from .layout import agent_repo


# toml_string 表示编码TOML基本字符串，value为配置值
# 保留真实Unicode并正确转义路径，拒绝控制字符
def toml_string(value: str) -> str:
    if any(ord(c) < 32 for c in value):
        raise ValueError("配置值不能包含换行或控制字符")
    return json.dumps(value, ensure_ascii=False)


# make_agent_config 表示渲染上游Agent配置，token为设备私有令牌
# 使用上游实际支持的PIAGENT_CONFIG环境变量读取配置
def make_agent_config(profile: dict, token: str, *, device_id: str | None = None, base_url: str | None = None) -> str:
    gateway = (base_url or profile["gateway"].get("base_url", "")).rstrip("/")
    ws_url = gateway.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    device_id = device_id or profile.get("agent_device_id", profile["gateway"].get("device_id", ""))
    cfg = profile["mcp"]
    agent = profile.get("agent", {})
    allowed = agent.get("allow_tools", ["light.set", "devices.status"])
    confirm = agent.get("confirm_tools", ["relay.set"])
    if any(not isinstance(values, list) or any(not isinstance(v, str) for v in values) for values in (allowed, confirm)):
        raise ValueError("agent.allow_tools和confirm_tools必须为字符串数组")
    lines = ["# 由任务4生成，含私有设备令牌，不能上传或放入交付压缩包",
             "server_url = " + toml_string(ws_url), "device_id = " + toml_string(device_id),
             "device_token = " + toml_string(token), 'llm_mode = "proxy"', "max_steps = 6",
             "allow_tools = [" + ", ".join(toml_string(v) for v in allowed) + "]",
             "confirm_tools = [" + ", ".join(toml_string(v) for v in confirm) + "]",
             "telemetry_tool = " + toml_string(agent.get("telemetry_tool", "")), "", "[[mcp.servers]]", 'name = "wall-light"',
             "command = " + toml_string(cfg["command"]),
             "args = [" + ", ".join(toml_string(v) for v in cfg["args"]) + "]"]
    env = dict(cfg.get("env", {}))
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env:
        lines.append("env = { " + ", ".join(toml_string(k) + " = " + toml_string(v) for k, v in env.items()) + " }")
    return "\n".join(lines) + "\n"


# unit_quote 表示转义systemd字符串，value为路径或环境值
# exec_arg仅用于Exec指令，Environment和工作目录不展开美元符号
def unit_quote(value: str, *, exec_arg: bool = False) -> str:
    value = value.replace("%", "%%")
    return toml_string(value.replace("$", "$$") if exec_arg else value)


# make_service 表示生成服务文本，user为Linux服务账号
# 服务使用独立名称和控制组，停止时同时回收MCP子进程
def make_service(profile_path: Path, agent_config: Path, *, user: str, python: Path, repo: Path) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?", user):
        raise ValueError("请提供有效的Linux服务用户名")
    env_python = str(repo / "agent") + ":" + str(ROOT)
    return "\n".join([
        "[Unit]", "Description=PiAgent wall-light deployment managed by task4", "After=network-online.target maker-servo-pwm-prepare.service", "Wants=network-online.target", "Requires=maker-servo-pwm-prepare.service", "",
        "[Service]", "Type=simple", "User=" + user, "WorkingDirectory=" + unit_quote(str(ROOT)),
        "Environment=" + unit_quote("PYTHONPATH=" + env_python), "Environment=" + unit_quote("PYTHONUTF8=1"),
        "Environment=" + unit_quote("PYTHONDONTWRITEBYTECODE=1"),
        "Environment=" + unit_quote("PIAGENT_CONFIG=" + str(agent_config)),
        "ExecStartPre=" + unit_quote(str(python), exec_arg=True) + " -m task4.cli preflight --profile " + unit_quote(str(profile_path), exec_arg=True),
        "ExecStart=" + unit_quote(str(python), exec_arg=True) + " -m pi_agent",
        "ExecStopPost=/bin/bash " + unit_quote(str(ROOT / "scripts/stop_pi5_pwm.sh"), exec_arg=True),
        "Restart=on-failure", "RestartSec=3", "KillMode=control-group", "TimeoutStopSec=10", "",
        "[Install]", "WantedBy=multi-user.target", ""])


# prepare_agent 表示生成私有配置和服务文件，profile为真机配置
# 生成前必须有完整交接，不安装服务，也不启动硬件
def prepare_agent(profile: dict, *, output: Path, user: str, repo: Path | None = None) -> dict:
    report = preflight(profile)
    report["kind"] = "prepare-agent"
    if profile["expected_mode"] != "gpio":
        check(report, "部署目标为真机", False, "模拟使用demo命令，不安装常驻服务", blocked=True)
    device_id = profile.get("agent_device_id", "")
    check(report, "设备ID与验收目标相同", bool(device_id) and device_id == profile["gateway"].get("device_id") and "CHANGE_ME" not in device_id,
          device_id, blocked=True)
    gateway = profile["gateway"].get("base_url", "")
    check(report, "网关地址已填写", gateway.startswith(("http://", "https://")) and "CHANGE_ME" not in gateway, gateway, blocked=True)
    token = os.environ.get(profile.get("agent_device_token_env", "TASK4_DEVICE_TOKEN"), "").strip()
    check(report, "设备私有令牌已提供", token.startswith("dev-") and len(token) >= 20, "只读取环境变量，不显示令牌", blocked=True)
    repo = Path(repo or agent_repo()).resolve()
    check(report, "Agent源码存在", (repo / "agent/pi_agent/__main__.py").is_file(), str(repo), blocked=True)
    if any(item["status"] != "PASS" for item in report["checks"]):
        return report
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o700)
    agent_config = output / "agent.private.toml"
    service = output / "piagent-task4.service"
    for path, text in ((agent_config, make_agent_config(profile, token)),
                       (service, make_service(Path(profile["_path"]), agent_config, user=user, python=Path(sys.executable), repo=repo))):
        if path.exists():
            check(report, "不覆盖已有生成文件", False, f"{path} 已存在，请选择新的输出目录", blocked=True)
            return report
    agent_config.write_text(make_agent_config(profile, token), encoding="utf-8")
    agent_config.chmod(0o600)
    service.write_text(make_service(Path(profile["_path"]), agent_config, user=user, python=Path(sys.executable), repo=repo), encoding="utf-8")
    service.chmod(0o644)
    check(report, "已生成配置和独立服务", True, {"agent_config": str(agent_config), "service": str(service)})
    report["generated_service"] = str(service)
    report["service_installed"] = False
    return report
