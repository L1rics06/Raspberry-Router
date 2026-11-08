"""配置解析与不会主动发送舵机动作的静态预检"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from .common import ROOT, check, new_report


# expand 表示展开包路径和当前解释器，value 是模板字符串
# 仅识别两个固定占位符，不执行 shell 或任意模板表达式
def expand(value: str) -> str:
    return value.replace("{bundle}", ROOT.as_posix()).replace("{python}", sys.executable)


# load_profile 表示读取 TOML，path 为配置文件路径
# 交接文件路径相对于配置文件处理，入口建议使用绝对路径
def load_profile(path: str | Path) -> dict:
    path = Path(path).resolve()
    raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    mode = raw.get("expected_mode")
    if mode not in ("sim", "gpio"):
        raise ValueError("expected_mode 只能为 sim 或 gpio")
    mcp = raw.get("mcp", {})
    if not isinstance(mcp.get("command"), str) or not isinstance(mcp.get("args"), list):
        raise ValueError("mcp.command 必须是字符串，mcp.args 必须是数组")
    if any(not isinstance(v, str) for v in mcp["args"]):
        raise ValueError("mcp.args 的每一项必须是字符串")
    env = mcp.get("env", {})
    if not isinstance(env, dict) or any(not isinstance(v, str) for v in env.values()):
        raise ValueError("mcp.env 的值必须为字符串")
    mcp["command"] = expand(mcp["command"])
    mcp["args"] = [expand(v) for v in mcp["args"]]
    mcp["env"] = {k: expand(v) for k, v in env.items()}
    raw["_path"] = str(path)
    raw["_base"] = str(path.parent)
    raw.setdefault("contract", {})
    raw["contract"].setdefault("kind", "wall_light")
    raw["contract"].setdefault("action_tool", "light.set")
    raw["contract"].setdefault("status_tool", "devices.status")
    raw.setdefault("gateway", {})
    return raw


# profile_path 表示解析配置中的数据路径，value 为相对或绝对路径
# 缺失值返回一个不会误匹配现有文件的占位路径
def profile_path(profile: dict, value: str) -> Path:
    value = expand(value)
    path = Path(value)
    return path.resolve() if path.is_absolute() else (Path(profile["_base"]) / path).resolve()


# validate_calibration 表示核对现场标定数据，data为已解析配置
# 所有位置必须实测且落在工作范围内，布尔值不当作数值
def validate_calibration(data: dict) -> list[str]:
    errors = []
    if data.get("calibrated") is not True:
        errors.append("calibrated 尚未确认为 true")
    if not str(data.get("operator", "")).strip() or not str(data.get("tested_at", "")).strip():
        errors.append("缺少标定人或标定时间")
    keys = ("min_us", "max_us", "home_us", "on_us", "off_us")
    if any(type(data.get(k)) is not int for k in keys):
        errors.append("脉宽字段必须全部为整数")
        return errors
    low, high = data["min_us"], data["max_us"]
    if not 1000 <= low < high <= 2000:
        errors.append("SG90本包工作脉宽范围需位于1000～2000微秒内")
    if any(not low <= data[k] <= high for k in ("home_us", "on_us", "off_us")):
        errors.append("三个位置没有全部落在工作范围内")
    if len({data[k] for k in ("home_us", "on_us", "off_us")}) != 3:
        errors.append("三个位置不能相同")
    wait = data.get("wait_s")
    if type(wait) not in (int, float) or not 0.05 <= wait <= 1.5:
        errors.append("wait_s 应为0.05～1.5秒，整套动作需符合工具超时约定")
    if data.get("clearance_verified") is not True:
        errors.append("尚未确认退回后不碰开关")
    return errors


# preflight 表示运行静态预检，profile 为已加载配置
# 不启动设备服务，也不调用 light.set，不能代替真实GPIO检查
def preflight(profile: dict, *, require_platform: bool = True, require_entry: bool = True) -> dict:
    mode = profile["expected_mode"]
    report = new_report("preflight", mode)
    report["profile"] = profile["_path"]
    command = profile["mcp"]["command"]
    executable = Path(command).is_file() or shutil.which(command) is not None
    check(report, "MCP解释器存在", executable, command, blocked=True)
    args = profile["mcp"]["args"]
    entries = [Path(v) for v in args if v.endswith(".py")]
    if require_entry:
        check(report, "MCP入口文件存在", bool(entries) and all(p.is_file() for p in entries), [str(p) for p in entries], blocked=True)
    if mode == "sim":
        check(report, "仿真模式已明确标注", True, "本报告不证明真实舵机动作")
        return report
    check(report, "真机配置不使用仿真入口", not any("mock" in p.name.lower() for p in entries) and "--sim" not in args,
          "本包已提供devices/wall_light_mcp.py真机入口", blocked=True)
    if require_platform:
        model_path = Path("/proc/device-tree/model")
        model = model_path.read_text(errors="replace").replace("\0", "") if model_path.exists() else ""
        check(report, "运行于树莓派5 Linux", platform.system() == "Linux" and model.startswith("Raspberry Pi 5"), model or platform.system(), blocked=True)
    settings = None
    if any(p.name in ("wall_light_mcp.py", "all_devices_mcp.py") for p in entries):
        try:
            from .servo_control import load_settings, calibration_errors
            settings_path = Path(args[args.index("--config") + 1])
            settings = load_settings(settings_path, "gpio")
            errors = calibration_errors(settings)
            check(report, "墙灯驱动配置及标定完整", not errors, errors or str(settings_path), blocked=True)
            if require_platform:
                from .pwm_driver import doctor
                hardware = doctor()
                check(report, "Pi5硬件PWM已准备", hardware["ok"], hardware, blocked=True)
        except (ValueError, OSError, IndexError) as error:
            check(report, "墙灯驱动配置存在", False, str(error), blocked=True)
    handoff = profile.get("handoff", {})
    for name in ("electrical", "calibration"):
        path = profile_path(profile, handoff.get(name, "MISSING"))
        if not check(report, f"{name}交接文件存在", path.is_file(), str(path), blocked=True):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig")) if path.suffix == ".json" else tomllib.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("交接文件必须为键值对象")
            if name == "calibration":
                errors = validate_calibration(data)
                check(report, "机械标定已交付", not errors, errors or "标定记录完整", blocked=True)
                if settings is not None:
                    keys = ("servo_model", "servo_profile", "power_nominal_v", "min_us", "max_us", "home_us", "on_us", "off_us", "wait_s", "operator", "tested_at")
                    check(report, "驱动实际配置与标定记录一致", all(data.get(k) == settings.get(k) for k in keys), blocked=True)
            else:
                from .servo_specs import electrical_errors
                errors = electrical_errors(data)
                check(report, "SG90 5V供电已实测确认", not errors, errors or "型号、电压、三线接法与负载测试记录完整", blocked=True)
        except (ValueError, OSError) as error:
            check(report, f"{name}文件可解析", False, str(error), blocked=True)
    check(report, "模拟环境变量未残留", all("SIM" not in k.upper() or v.strip().lower() in ("0", "false", "no", "off")
          for k, v in profile["mcp"].get("env", {}).items()), "检查mcp.env中的模拟开关", blocked=True)
    return report


# gateway_settings 表示读取网关地址与凭证，profile 为配置
# 令牌只从环境变量或私有文件读取，不接受命令行明文参数
def gateway_settings(profile: dict) -> tuple[str, str, str]:
    cfg = profile["gateway"]
    base = str(cfg.get("base_url", "")).rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or "CHANGE_ME" in base or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("请填写有效的网关HTTP地址，路径可包含 /piagent，不能含凭证或查询参数")
    token = os.environ.get(cfg.get("access_token_env", "TASK4_ACCESS_TOKEN"), "").strip()
    if not token and cfg.get("access_token_file"):
        token = profile_path(profile, cfg["access_token_file"]).read_text(encoding="utf-8-sig").strip()
    if not token.startswith("pat-"):
        raise ValueError("未找到访问令牌，请设置指定环境变量或私有令牌文件")
    device_id = str(cfg.get("device_id", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", device_id) or "CHANGE_ME" in device_id:
        raise ValueError("必须明确指定测试设备ID，不能自动选择第一台在线设备")
    return base, token, device_id
