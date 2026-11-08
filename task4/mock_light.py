"""任务4专用墙灯模拟夹具，不导入任何GPIO库"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import StrictBool

mcp = FastMCP("task4-wall-light-simulation")
LOCK = threading.Lock()
STATE = {"count": 0, "requested_on": None, "trace": []}


# controls 表示读取故障注入参数，无参数
# 只读取任务4创建的本地夹具文件，缺省始终为明确仿真模式
def controls() -> dict:
    path = os.environ.get("TASK4_MOCK_CONTROL", "")
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


# status 表示生成状态快照，无参数
# 模式永远为sim，不能通过参数伪装成GPIO设备
def status() -> dict:
    cfg = controls()
    return {"ok": True, "mode": "sim", "ready": cfg.get("ready", True),
            "calibrated": cfg.get("calibrated", True), "lamp_state": "unknown",
            "errors": {}, "fixture": True, "command_count": STATE["count"],
            "requested_on": STATE["requested_on"], "trace": list(STATE["trace"])}


# light_set 表示模拟开关动作，on为布尔目标
# 仅记录命令与三个动作阶段，故障注入不会改变状态
@mcp.tool(name="light.set", description="任务4模拟寝室墙灯，on=true开灯指令，false关灯指令，没有真实硬件")
def light_set(on: StrictBool) -> str:
    with LOCK:
        cfg = controls()
        if type(on) is not bool or cfg.get("failure") or not cfg.get("ready", True) or not cfg.get("calibrated", True):
            return json.dumps({"ok": False, "mode": "sim", "error": "任务4注入的设备失败或未就绪", "lamp_state": "unknown"}, ensure_ascii=False)
        STATE["count"] += 1
        STATE["requested_on"] = on
        STATE["trace"] = ["home", "press_on" if on else "press_off", "home"]
        return json.dumps({"ok": True, "mode": "sim", "requested_on": on, "lamp_state": "unknown",
                           "trace": list(STATE["trace"]), "detail": "仅模拟按压流程，没有真实GPIO动作"}, ensure_ascii=False)


# devices_status 表示查询模拟状态，无参数
# 与正式交接契约保持字段一致，fixture标记不能用于真机验收
@mcp.tool(name="devices.status", description="查询任务4模拟设备状态，始终明确标为sim")
def devices_status() -> str:
    with LOCK:
        return json.dumps(status(), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
