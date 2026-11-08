"""报告、脱敏及路径工具"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))
SECRET_KEYS = {"token", "access_token", "device_token", "password", "api_key", "secret", "authorization", "jwt"}
TOKEN_PATTERN = re.compile(r"\b(?:pat-|dev-|sk-)[A-Za-z0-9_-]{8,}|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


# now_text 表示获取北京时间，无参数
# 用于报告时间戳，不读取主机本地时区
def now_text() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# redact 表示脱敏，value 可以是字符串、列表或字典
# 保存报告前递归移除凭证，仍不应把真实配置当作普通报告上传
def redact(value):
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if str(k).lower() in SECRET_KEYS else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = TOKEN_PATTERN.sub("[REDACTED]", value)
        return re.sub(r"(?i)(Bearer\s+)[^\s\"<>]+", r"\1[REDACTED]", value)
    return value


# new_report 表示创建报告，kind 是检查类型，mode 是 sim 或 gpio
# 自动化检查从不把硬件状态设为已验收
def new_report(kind: str, mode: str = "sim") -> dict:
    return {"format_version": 1, "kind": kind, "created_at": now_text(), "expected_mode": mode,
            "status": "PENDING", "hardware_verified": False, "checks": [], "findings": []}


# check 表示加入一项检查，passed 为真或假，blocked 表示缺少前置交付
# detail 必须提供可复查的事实，不填入凭证
def check(report: dict, name: str, passed: bool, detail="", *, blocked: bool = False) -> bool:
    status = "PASS" if passed else ("BLOCKED" if blocked else "FAIL")
    report["checks"].append({"name": name, "status": status, "detail": redact(detail)})
    return passed


# finish 表示汇总检查，report 为报告字典
# gpio 自动检查通过后仍等待人工实物证据
def finish(report: dict) -> dict:
    counts = {name: sum(c["status"] == name for c in report["checks"]) for name in ("PASS", "FAIL", "BLOCKED")}
    report["counts"] = counts
    if counts["FAIL"]:
        report["status"] = "FAIL"
    elif counts["BLOCKED"]:
        report["status"] = "BLOCKED"
    elif report["expected_mode"] == "sim":
        report["status"] = "SIMULATION_PASS"
    else:
        report["status"] = "HARDWARE_EVIDENCE_PENDING"
    report["hardware_verified"] = False
    return report


# write_json 表示原子保存 JSON，path 是输出路径，private 控制权限
# 普通报告一律脱敏，真实配置只允许 private 模式写入
def write_json(path: Path, data, *, private: bool = False) -> Path:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data if private else redact(data), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if private:
        temp.chmod(0o600)
    os.replace(temp, path)
    return path


# save_report 表示保存报告，path 可留空自动生成
# 打印内容仅含摘要，返回报告文件的绝对路径
def save_report(report: dict, path: str | Path | None = None) -> Path:
    finish(report)
    if path is None:
        stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S_%f")
        path = ROOT / "reports" / f"{stamp}_{report['kind']}.json"
    target = write_json(Path(path), report)
    print(f"{report['status']} | {report.get('counts', {})}")
    print(f"报告：{target}")
    return target


# exit_code 表示报告对应的进程退出码
# 0 为软件检查通过，1 为失败，2 为前置条件未满足
def exit_code(report: dict) -> int:
    return 1 if report["status"] == "FAIL" else 2 if report["status"] == "BLOCKED" else 0


# file_sha256 表示计算文件指纹，path 为实际文件
# 用于快照和证据完整性记录，不代表对证据真实性的独立认证
def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# parse_tool_payload 表示解析设备工具结果，raw 为 JSON 文本或对象
# 成功必须显式返回布尔 ok，不能把非空字符串当作成功
def parse_tool_payload(raw) -> dict:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
        raise ValueError("设备结果必须是包含布尔 ok 的 JSON 对象")
    return raw


# inspect_gateway_result 表示核对网关的外层和设备内层结果
# 外层 ok=true 而内层 ok=false 时标为失败，并保留矛盾事实
def inspect_gateway_result(outer: dict) -> tuple[bool, dict, str]:
    if not isinstance(outer, dict):
        return False, {}, "网关结果不是对象"
    try:
        inner = parse_tool_payload(outer.get("text", ""))
    except (ValueError, TypeError) as error:
        return False, {}, f"设备结果无法校验：{error}"
    if outer.get("ok") is True and inner["ok"] is False:
        return False, inner, "外层 ok=true，但设备内层 ok=false，上游错误没有正确传播"
    success = outer.get("ok") is True and inner["ok"] is True
    return success, inner, "" if success else str(inner.get("error") or outer.get("text") or "工具失败")
