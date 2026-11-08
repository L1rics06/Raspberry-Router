"""人工实物证据完整性检查，不替代现场观察"""
from __future__ import annotations

import json
from pathlib import Path

from .common import ROOT, check, file_sha256, new_report


# collect_evidence 表示核对人工验收记录，technical为技术报告路径
# evidence为操作员填写的记录，只认真实GPIO报告和明确的人工观察
def collect_evidence(technical: Path, evidence: Path) -> dict:
    report = new_report("physical-evidence", "gpio")
    technical = Path(technical).resolve()
    evidence = Path(evidence).resolve()
    try:
        data = json.loads(technical.read_text(encoding="utf-8-sig"))
        human = json.loads(evidence.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as error:
        check(report, "证据文件可读取", False, str(error), blocked=True)
        return report
    if not isinstance(data, dict) or not isinstance(human, dict):
        check(report, "证据文件为JSON对象", False, blocked=True)
        return report
    valid = data.get("kind") == "gateway" and data.get("expected_mode") == "gpio"
    valid = valid and data.get("status") == "HARDWARE_EVIDENCE_PENDING"
    valid = valid and data.get("action_checks_completed") is True and type(data.get("cycles")) is int and data["cycles"] >= 10
    valid = valid and data.get("chat_trace_only") is True
    valid = valid and data.get("chat_direction_checked") is True and data.get("chat_device_state_checked") is True
    technical_checks = data.get("checks", [])
    valid = valid and bool(technical_checks) and all(isinstance(c, dict) and c.get("status") == "PASS" for c in technical_checks)
    check(report, "真实网关动作与聊天检查已完成", valid, "需gpio模式、至少10轮、聊天方向和执行计数均通过、零失败报告", blocked=True)
    check(report, "操作员及时间已填写", bool(str(human.get("operator", "")).strip()) and bool(str(human.get("tested_at", "")).strip()), blocked=True)
    check(report, "设备身份匹配", bool(data.get("device_id")) and human.get("device_id") == data.get("device_id"), data.get("device_id"), blocked=True)
    observed = human.get("cycles_observed")
    check(report, "实际观察至少10轮", type(observed) is int and observed >= 10, observed, blocked=True)
    required = ("on_verified", "off_verified", "returned_home", "no_reset", "manual_switch_access", "restart_safe", "offline_error_clear")
    checks = human.get("observations", {})
    checks = checks if isinstance(checks, dict) else {}
    check(report, "现场验收项已逐项确认", all(checks.get(key) is True for key in required), {key: checks.get(key) for key in required}, blocked=True)
    attachments = human.get("attachments", [])
    records = []
    video = False
    for value in attachments if isinstance(attachments, list) else []:
        path = (evidence.parent / str(value)).resolve()
        safe = path.is_relative_to(ROOT.resolve()) and path.is_file() and path.stat().st_size > 0
        if check(report, "附件存在并保存在交付目录", safe, str(path), blocked=True):
            records.append({"path": str(path), "size": path.stat().st_size, "sha256": file_sha256(path)})
            video = video or path.suffix.lower() in (".mp4", ".mov", ".mkv", ".avi", ".webm")
    check(report, "至少有一份非空操作视频", video, blocked=True)
    report["attachments"] = records
    report["technical_report_sha256"] = file_sha256(technical)
    report["operator"] = human.get("operator", "")
    report["operator_evidence_complete"] = all(c["status"] == "PASS" for c in report["checks"])
    report["independently_reviewed"] = False
    report["note"] = "本工具核对记录与附件完整性，未自动判断视频内容或独立证明灯光状态"
    return report
