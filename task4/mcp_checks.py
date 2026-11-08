"""真实MCP协议检查，默认不发送开关动作"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .common import ROOT, check, new_report, parse_tool_payload


# unpack_result 表示解析MCP返回值，result 是SDK结果对象
# 协议级 isError 优先判失败，防止错误文本被当成成功
def unpack_result(result) -> dict:
    text = "\n".join(item.text for item in getattr(result, "content", []) if hasattr(item, "text"))
    if getattr(result, "isError", False):
        raise ValueError(f"MCP协议报告错误：{text[:400]}")
    return parse_tool_payload(text)


# schema_of 表示读取工具参数结构，tool 为SDK工具对象
# 同时兼容SDK历史字段命名
def schema_of(tool) -> dict:
    return getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}


# validate_status 表示核对设备声明，report 为报告，payload 为状态结果
# gpio只说明驱动模式，还需单独提交实物验收证据
def validate_status(report: dict, payload: dict, *, wall: bool) -> bool:
    mode = payload.get("mode", payload.get("servo_mode"))
    expected = report["expected_mode"]
    mode_ok = mode in ("sim", "simulated") if expected == "sim" else mode == "gpio"
    passed = check(report, "设备模式与验收目标一致", mode_ok, {"expected": expected, "actual": mode})
    if expected == "gpio":
        passed = check(report, "真机状态不是模拟夹具", payload.get("fixture") is not True, payload.get("fixture")) and passed
    passed = check(report, "设备状态无错误", payload.get("ok") is True and not payload.get("errors"), payload.get("errors", {})) and passed
    if wall:
        passed = check(report, "设备已就绪", payload.get("ready") is True, payload.get("ready"), blocked=True) and passed
        passed = check(report, "设备已完成机构标定", payload.get("calibrated") is True, payload.get("calibrated"), blocked=True) and passed
        passed = check(report, "无反馈时不伪报灯状态", payload.get("lamp_state") in ("unknown", "unverified"),
                       payload.get("lamp_state")) and passed
    return passed


# run_mcp 表示检查一个设备服务，profile 是配置，actuate 表示允许主动动作
# 不会调用未知工具或原有45度与180度示例，只执行明确的开关接口
async def run_mcp(profile: dict, *, actuate: bool = False, cycles: int = 1) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    report = new_report("mcp", profile["expected_mode"])
    report["profile"] = profile.get("_path", "generated")
    report["actuation_requested"] = actuate
    report["cycles"] = cycles
    if not 1 <= cycles <= 10:
        check(report, "动作次数范围", False, "cycles只允许1～10")
        return report
    cfg = profile["mcp"]
    env = dict(os.environ)
    env.update(cfg.get("env", {}))
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    timeout = min(max(float(profile.get("timeout_s", 15)), 1), 60)
    params = StdioServerParameters(command=cfg["command"], args=cfg["args"], env=env)
    ROOT.joinpath(".runtime").mkdir(exist_ok=True)
    stderr_path = ROOT / ".runtime" / "mcp-child.log"
    try:
        with stderr_path.open("a", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout)
                    check(report, "MCP握手成功", True)
                    tools = (await asyncio.wait_for(session.list_tools(), timeout)).tools
                    by_name = {tool.name: tool for tool in tools}
                    report["tools"] = sorted(by_name)
                    wall = profile["contract"]["kind"] == "wall_light"
                    required = {profile["contract"]["action_tool"], profile["contract"]["status_tool"]} if wall else {
                        "led.set", "fan.set", "relay.set", "dht.read", "devices.status"}
                    if not check(report, "目标工具齐全", required <= set(by_name),
                                 {"required": sorted(required), "missing": sorted(required - set(by_name))}, blocked=True):
                        return report
                    if wall:
                        action = by_name[profile["contract"]["action_tool"]]
                        schema = schema_of(action)
                        props = schema.get("properties", {})
                        schema_ok = props.get("on", {}).get("type") == "boolean" and "on" in schema.get("required", [])
                        if not check(report, "开关参数为必填布尔on", schema_ok, schema):
                            return report
                    status = unpack_result(await asyncio.wait_for(session.call_tool(profile["contract"]["status_tool"], {}), timeout))
                    report["device_status"] = status
                    if not validate_status(report, status, wall=wall):
                        return report
                    if not actuate:
                        check(report, "本次未发送主动动作", True, "仅握手、工具清单和状态查询")
                        return report
                    if not wall:
                        check(report, "主动动作限墙灯接口", False, "通用设备仅做只读核查，不自动启动风扇或继电器")
                        return report
                    for cycle in range(1, cycles + 1):
                        for on in (True, False):
                            result = unpack_result(await asyncio.wait_for(session.call_tool(profile["contract"]["action_tool"], {"on": on}), timeout))
                            result_mode = result.get("mode")
                            mode_ok = result_mode in ("sim", "simulated") if profile["expected_mode"] == "sim" else result_mode == "gpio"
                            ok = result["ok"] is True and mode_ok and result.get("requested_on") is on
                            if not check(report, f"第{cycle}轮{'开灯' if on else '关灯'}命令", ok, result):
                                return report
                    report["action_checks_completed"] = True
    except Exception as error:
        check(report, "MCP会话正常结束", False, {"error": str(error), "stderr_log": str(stderr_path)})
    return report
