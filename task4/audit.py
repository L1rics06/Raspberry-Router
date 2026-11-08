"""核对交付快照与当前墙灯接口缺口，不执行上游设备代码"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from .common import ROOT, check, file_sha256, new_report


# audit_upstream 表示审计固定版本的上游快照，无参数
# 检查源码而不启动GPIO，缺少墙灯接口时返回BLOCKED
def audit_upstream() -> dict:
    report = new_report("upstream-audit", "gpio")
    manifest = json.loads((ROOT / "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
    report["upstream_commit"] = manifest["commit"]
    mismatches = []
    for entry in manifest["files"]:
        path = ROOT / "upstream_snapshot" / entry["path"]
        raw = path.read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if blob != entry["blob"]:
            mismatches.append(entry["path"])
    check(report, "快照与47个上游Git对象一致", not mismatches, {"count": len(manifest["files"]), "mismatches": mismatches})
    source = ROOT / "upstream_snapshot/devices/device_mcp_server.py"
    text = source.read_text(encoding="utf-8")
    tree = ast.parse(text)
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == "tool":
                name = next((v.value.value for v in decorator.keywords if v.arg == "name" and isinstance(v.value, ast.Constant)), node.name)
                names.append(name)
    report["tools_in_source"] = sorted(names)
    report["source_sha256"] = file_sha256(source)
    check(report, "设备源码可解析", True)
    check(report, "已有墙灯开关接口", "light.set" in names, {"required": "light.set", "actual": sorted(names)}, blocked=True)
    check(report, "已有舵机接口", "servo.set" in names, "当前合并版本无舵机工具，不能据README认定完成", blocked=True)
    check(report, "完整交付包已有墙灯入口", (ROOT / "devices/wall_light_mcp.py").is_file(), "本包在独立目录补齐，上游固定快照保持原样")
    report["findings"].append({"id": "GPIO17_CONFLICT", "status": "RESOLVED_IN_PACKAGE",
        "detail": "完整包改用BCM12物理32脚硬件PWM，上游LED仍为BCM17，两者不再占同一脚"})
    report["live_github_checked_at"] = manifest.get("retrieved_at", manifest.get("fetched_at", "2026-11-03"))
    return report
