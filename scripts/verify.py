"""重复执行交付包的软件检查，全程不连接GPIO或付费模型"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# main 表示执行固定的软件验证清单，无参数
# BLOCKED只在两个预期缺失条件的检查中被接受，原始结果仍保留
def main() -> int:
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT))
    entries = [
        ("unit-tests", ["-m", "unittest", "discover", "-s", "tests", "-v"], 0),
        ("syntax", ["-m", "compileall", "-q", *[name for name in ("task4", "tests", "devices", "scripts", "runtime_source", "agent", "server", "upstream_snapshot") if (ROOT / name).is_dir()]], 0),
        ("mcp-simulation", ["-m", "task4.cli", "mcp-check", "--profile", "profiles/simulation.toml", "--actuate", "--cycles", "2", "--report", "reports/mcp-simulation.json"], 0),
        ("mcp-upstream-sim", ["-m", "task4.cli", "mcp-check", "--profile", "profiles/upstream-sim.toml", "--report", "reports/mcp-upstream-sim.json"], 0),
        ("servo-mcp-simulation", ["-m", "task4.cli", "mcp-check", "--profile", "profiles/servo-simulation.toml", "--actuate", "--cycles", "2", "--report", "reports/servo-mcp-simulation.json"], 0),
        ("combined-mcp-simulation", ["-m", "task4.cli", "mcp-check", "--profile", "profiles/combined-simulation.toml", "--actuate", "--cycles", "1", "--report", "reports/combined-mcp-simulation.json"], 0),
        ("upstream-selftest", ["upstream_snapshot/devices/mcp_selftest.py", "--sim"], 0),
        ("combined-existing-contract", ["upstream_snapshot/devices/mcp_selftest.py", "--server", "devices/all_devices_mcp.py", "--sim"], 0),
        ("upstream-audit", ["-m", "task4.cli", "audit-upstream", "--report", "reports/upstream-audit.json"], 2),
        ("hardware-template-preflight", ["-m", "task4.cli", "preflight", "--profile", "profiles/hardware.example.toml", "--report", "reports/hardware-template-preflight.json"], 2),
        ("local-demo", ["-m", "task4.cli", "demo", "--report", "reports/local-demo.json"], 0),
    ]
    results = []
    for name, args, expected in entries:
        try:
            process = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=150)
            text = process.stdout + process.stderr
            code = process.returncode
        except subprocess.TimeoutExpired:
            text, code = "Software verification timed out\n", 124
        (reports / (name + ".txt")).write_text(text, encoding="utf-8")
        passed = code == expected
        results.append({"name": name, "expected_exit": expected, "actual_exit": code, "passed": passed})
        print(f"{'PASS' if passed else 'FAIL'} {name}: exit={code}, expected={expected}", flush=True)
    summary = {"created_at": datetime.now(timezone.utc).isoformat(), "hardware_verified": False,
               "real_llm_verified": False, "public_network_verified": False, "checks": results,
               "passed": all(item["passed"] for item in results)}
    (reports / "software-validation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
