#!/usr/bin/env python3
"""PiAgent 端到端验收脚本：模拟前端用户的完整操作路径。

用法：
    python scripts/e2e_test.py [网关地址] [用户名] [密码]
    python scripts/e2e_test.py http://127.0.0.1:8000 demo demo123456

前置条件：网关已启动、测试用户已创建并绑定在线设备、Agent 已连上。
覆盖：登录 → 设备列表 → 手动直控（含高危拒绝/确认） → WS 聊天（Agent+MCP） → 历史记录。
"""
import asyncio
import json
import sys

import httpx
import websockets

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
WS_BASE = BASE.replace("http://", "ws://").replace("https://", "wss://")
USERNAME = sys.argv[2] if len(sys.argv) > 2 else "demo"
PASSWORD = sys.argv[3] if len(sys.argv) > 3 else "demo123456"

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  | {detail}" if detail and not cond else ""))
    if cond:
        _passed += 1
    else:
        _failed += 1


async def wait_result(ws, want_id: str | None = None, timeout: float = 60) -> dict | None:
    """从用户 WS 里等 result（跳过 telemetry/device_status 等事件）。"""
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        m = json.loads(raw)
        if m.get("type") == "result" and (want_id is None or m.get("id") == want_id):
            return m
        if m.get("type") == "cmd_error":
            return m


async def main() -> None:
    print(f"== PiAgent E2E 测试 ==\n网关: {BASE}  用户: {USERNAME}")

    async with httpx.AsyncClient(timeout=60) as c:
        # 1. 登录
        r = await c.post(f"{BASE}/api/auth/login",
                         json={"username": USERNAME, "password": PASSWORD})
        check("登录", r.status_code == 200, r.text)
        token = r.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        # 2. 设备列表（在线 + 能力上报）
        r = await c.get(f"{BASE}/api/devices", headers=auth)
        devices = r.json()
        check("设备列表可查", r.status_code == 200 and len(devices) >= 1, r.text)
        dev = next((d for d in devices if d["online"]), None)
        check("设备在线且上报能力", dev is not None and "fan.set" in dev["caps"],
              json.dumps(devices, ensure_ascii=False))
        if dev is None:
            print("设备不在线，无法继续，退出")
            return
        dev_id = dev["device_id"]

        # 3. 手动直控：开风扇
        r = await c.post(f"{BASE}/api/devices/{dev_id}/command", headers=auth,
                         json={"tool": "fan.set", "args": {"state": True}})
        check("手动开风扇", r.status_code == 200 and r.json().get("ok") is True, r.text)

        # 4. 高危工具未确认 → 应被 Agent 安全闸门拒绝
        r = await c.post(f"{BASE}/api/devices/{dev_id}/command", headers=auth,
                         json={"tool": "relay.set", "args": {"ch": 0, "state": True}})
        body = r.json()
        check("高危工具未确认被拒绝",
              r.status_code == 200 and body.get("ok") is False and "确认" in body.get("text", ""),
              r.text)

        # 5. 高危工具确认后放行
        r = await c.post(f"{BASE}/api/devices/{dev_id}/command", headers=auth,
                         json={"tool": "relay.set", "args": {"ch": 0, "state": True},
                               "confirmed": True})
        check("高危工具确认后执行", r.status_code == 200 and r.json().get("ok") is True, r.text)

    # 6. WS 聊天（走 Agent 循环 + MCP）
    async with websockets.connect(f"{WS_BASE}/ws/user?token={token}") as ws:
        hello = json.loads(await ws.recv())
        check("WS hello 快照", hello.get("type") == "hello" and "devices" in hello)

        await ws.send(json.dumps({"type": "chat", "text": "把风扇关掉"}))
        res = await wait_result(ws)
        check("聊天: 关风扇（Agent→MCP）",
              res and res.get("ok") and "fan.set" in "".join(res.get("actions", [])),
              json.dumps(res, ensure_ascii=False))

        await ws.send(json.dumps({"type": "chat", "text": "寝室现在多少度？"}))
        res = await wait_result(ws)
        check("聊天: 读温湿度（Agent→MCP）",
              res and res.get("ok") and "dht.read" in "".join(res.get("actions", []))
              and "°C" in res.get("text", ""),
              json.dumps(res, ensure_ascii=False))

    # 7. 历史审计
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/api/history", headers=auth,
                        params={"device_id": dev_id, "limit": 20})
        rows = r.json()
        kinds = {row["kind"] for row in rows}
        check("历史记录落库", r.status_code == 200 and len(rows) >= 5
              and {"chat", "tool"} <= kinds, f"rows={len(rows)} kinds={kinds}")

    print(f"\n== 结果: {_passed} 通过, {_failed} 失败 ==")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
