"""经过实际网关和Agent的验收客户端"""
from __future__ import annotations

import asyncio
import json

from .common import check, inspect_gateway_result, new_report
from .mcp_checks import validate_status
from .profile import gateway_settings, preflight


# login 表示访问令牌换JWT，client为HTTP客户端，token为私有令牌
# JWT仅保留在内存，不写入报告或日志
async def login(client, base: str, token: str) -> str:
    response = await client.post(base + "/api/auth/token-login", json={"token": token})
    response.raise_for_status()
    data = response.json()
    if not isinstance(data.get("token"), str):
        raise ValueError("登录响应缺少JWT")
    return data["token"]


# manual 表示发送一个直控请求，device_id必须明确指定
# 只有调用者通过就绪和模式检查后才可发送主动动作
async def manual(client, base: str, jwt: str, device_id: str, tool: str, args: dict):
    response = await client.post(base + f"/api/devices/{device_id}/command",
                                 headers={"Authorization": "Bearer " + jwt},
                                 json={"tool": tool, "args": args, "wait": True, "confirmed": False})
    response.raise_for_status()
    return response.json()


# chat_once 表示完成一条WS聊天，jwt为内存中的会话令牌
# 同时发送设备ID并按命令ID匹配结果，不把其他设备事件当作成功
async def chat_once(base: str, jwt: str, device_id: str, text: str, timeout: float = 30) -> dict:
    import websockets
    ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    async with websockets.connect(ws_base + "/ws/user?token=" + jwt, open_timeout=timeout) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if hello.get("type") != "hello":
            raise ValueError("未收到用户WS快照")
        await ws.send(json.dumps({"type": "chat", "device_id": device_id, "text": text}, ensure_ascii=False))
        deadline = asyncio.get_running_loop().time() + timeout
        command_id = None
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("聊天等待超时，未自动重发动作")
            message = json.loads(await asyncio.wait_for(ws.recv(), remaining))
            if message.get("type") == "cmd_ack":
                command_id = message.get("id")
            elif message.get("type") in ("cmd_error", "error"):
                raise ValueError(str(message.get("message", message)))
            elif message.get("type") == "result" and command_id and message.get("id") == command_id:
                return message


# unique_object 将JSON键值对还原为对象，pairs由JSON解析器传入
# 拒绝重复字段，避免同一条轨迹同时出现两个不同的on
def unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("工具参数含重复字段")
        result[key] = value
    return result


# validate_chat_trace 核对一条聊天的实际工具及方向，on是预期布尔目标
# 仅允许一次墙灯动作及无参数状态查询，未知工具或重复按压均不通过
def validate_chat_trace(result: dict, action_tool: str, status_tool: str, on: bool) -> tuple[bool, str]:
    if not isinstance(result, dict) or result.get("ok") is not True:
        return False, "聊天返回失败"
    actions = result.get("actions")
    if not isinstance(actions, list) or not actions:
        return False, "聊天未返回实际工具轨迹"
    presses = 0
    for action in actions:
        if not isinstance(action, str) or not action.endswith(")"):
            return False, "工具轨迹格式错误"
        name, separator, raw = action.partition("(")
        if not separator or name not in (action_tool, status_tool):
            return False, "工具轨迹包含未知工具"
        try:
            args = json.loads(raw[:-1], object_pairs_hook=unique_object)
        except (ValueError, TypeError):
            return False, "工具轨迹参数不是明确的JSON对象"
        if not isinstance(args, dict):
            return False, "工具轨迹参数必须是对象"
        if name == status_tool:
            if args:
                return False, "状态查询不能夹带动作参数"
        else:
            if set(args) != {"on"} or type(args["on"]) is not bool or args["on"] is not on:
                return False, "工具轨迹的开关方向或参数错误"
            presses += 1
    return (True, "") if presses == 1 else (False, "每条聊天必须恰好执行一次墙灯动作")


# validate_chat_effect 对照聊天前后的设备状态，on为预期目标
# 计数必须只增加一次，状态必须匹配，防止只生成轨迹而没有执行
def validate_chat_effect(before: dict, after: dict, on: bool, mode: str) -> bool:
    start, end = before.get("command_count"), after.get("command_count")
    mode_ok = after.get("mode") in ("sim", "simulated") if mode == "sim" else after.get("mode") == "gpio"
    return (type(start) is int and start >= 0 and type(end) is int and end == start + 1
            and after.get("requested_on") is on and after.get("ok") is True
            and mode_ok and after.get("ready") is True and after.get("calibrated") is True
            and not after.get("errors") and after.get("lamp_state") in ("unknown", "unverified"))


# run_gateway 表示运行网关验收，profile指定唯一设备及预期模式
# 默认只读；chat必须同时允许actuate，超时或失败不会盲目重发
async def run_gateway(profile: dict, *, actuate: bool = False, cycles: int = 1, chat: bool = False) -> dict:
    import httpx

    report = new_report("gateway", profile["expected_mode"])
    report["actuation_requested"] = actuate
    report["cycles"] = cycles
    if not check(report, "动作次数有效", 1 <= cycles <= 10, cycles):
        return report
    if not check(report, "聊天动作已明确启用", not chat or actuate, "--chat需同时使用--actuate"):
        return report
    if profile["expected_mode"] == "gpio":
        gate = preflight(profile, require_platform=False, require_entry=False)
        report["checks"].extend(gate["checks"])
        if any(c["status"] != "PASS" for c in gate["checks"]):
            return report
    try:
        base, token, device_id = gateway_settings(profile)
        report["device_id"] = device_id
        report["gateway"] = base
    except (ValueError, OSError) as error:
        check(report, "网关凭证与目标已填写", False, str(error), blocked=True)
        return report
    try:
        async with httpx.AsyncClient(timeout=35, trust_env=False) as client:
            jwt = await login(client, base, token)
            check(report, "访问令牌登录成功", True)
            response = await client.get(base + "/api/devices", headers={"Authorization": "Bearer " + jwt})
            response.raise_for_status()
            devices = response.json()
            device = next((d for d in devices if d.get("device_id") == device_id), None)
            if not check(report, "目标设备存在且在线", device is not None and device.get("online") is True, device_id, blocked=True):
                return report
            needed = {profile["contract"]["action_tool"], profile["contract"]["status_tool"]}
            if not check(report, "目标设备上报墙灯工具", needed <= set(device.get("caps", [])), device.get("caps"), blocked=True):
                return report
            outer = await manual(client, base, jwt, device_id, profile["contract"]["status_tool"], {})
            ok, status, reason = inspect_gateway_result(outer)
            if not check(report, "状态响应内外层一致", ok, reason or status):
                return report
            report["device_status"] = status
            if not validate_status(report, status, wall=True):
                return report
            if not actuate:
                check(report, "本次未发送主动动作", True)
                return report
            for cycle in range(1, cycles + 1):
                for on in (True, False):
                    outer = await manual(client, base, jwt, device_id, profile["contract"]["action_tool"], {"on": on})
                    ok, inner, reason = inspect_gateway_result(outer)
                    mode = inner.get("mode")
                    mode_ok = mode in ("sim", "simulated") if profile["expected_mode"] == "sim" else mode == "gpio"
                    valid = ok and mode_ok and inner.get("requested_on") is on
                    if not check(report, f"第{cycle}轮{'开灯' if on else '关灯'}", valid, reason or inner):
                        return report
            report["action_checks_completed"] = True
            if chat:
                for text, on in (("打开寝室灯", True), ("关闭寝室灯", False)):
                    outer = await manual(client, base, jwt, device_id, profile["contract"]["status_tool"], {})
                    ok, before, reason = inspect_gateway_result(outer)
                    if not check(report, "聊天前状态可核对：" + text, ok and validate_status(report, before, wall=True), reason or before):
                        return report
                    result = await chat_once(base, jwt, device_id, text)
                    valid, reason = validate_chat_trace(result, profile["contract"]["action_tool"], profile["contract"]["status_tool"], on)
                    if not check(report, "聊天工具及方向：" + text, valid, reason or result):
                        return report
                    outer = await manual(client, base, jwt, device_id, profile["contract"]["status_tool"], {})
                    ok, after, reason = inspect_gateway_result(outer)
                    if not check(report, "聊天执行计数与目标：" + text,
                                 ok and validate_chat_effect(before, after, on, profile["expected_mode"]), reason or after):
                        return report
                report["chat_trace_only"] = True
                report["chat_direction_checked"] = True
                report["chat_device_state_checked"] = True
            history = await client.get(base + "/api/history", headers={"Authorization": "Bearer " + jwt},
                                       params={"device_id": device_id, "limit": 50})
            history.raise_for_status()
            check(report, "目标设备审计记录可查询", bool(history.json()) and all(row["device_id"] == device_id for row in history.json()))
    except Exception as error:
        check(report, "网关会话正常完成", False, str(error))
    return report
