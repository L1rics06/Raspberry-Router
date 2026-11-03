#!/usr/bin/env python3
"""设备 MCP Server 契约自检 —— 分工4 的验收脚本（不需要树莓派接线也能跑）。

它会真的按 MCP 协议做一遍完整流程：
    启动子进程(stdio) → initialize 握手 → tools/list 发现工具
    → 逐个 tools/call 调用 → 校验返回值是否符合 mcp_tools_schema.md

用法：
    python devices/mcp_selftest.py            # 自动：无 GPIO 时仿真
    python devices/mcp_selftest.py --sim      # 强制仿真（电脑上推荐）
    python devices/mcp_selftest.py --gpio     # 树莓派真机（会真的动硬件！）
    python devices/mcp_selftest.py --gpio --skip-dht   # 还没接温湿度传感器时

退出码 0 表示全部通过（跳过的项不算失败）；非 0 表示有失败项。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
DEFAULT_SERVER = HERE / "device_mcp_server.py"

# 契约（docs/接口文档/mcp_tools_schema.md）：工具名 → (参数名 → JSON Schema 类型, 必填参数)
CONTRACT: dict[str, tuple[dict[str, str], set[str]]] = {
    "led.set": ({"on": "boolean"}, {"on"}),
    "fan.set": ({"state": "boolean"}, {"state"}),
    "relay.set": ({"ch": "integer", "state": "boolean"}, {"ch", "state"}),
    "dht.read": ({}, set()),
    "devices.status": ({}, set()),
}

_passed = 0
_failed = 0
_skipped = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  | {detail}" if detail and not cond else ""))
    if cond:
        _passed += 1
    else:
        _failed += 1


def skip(name: str, reason: str) -> None:
    global _skipped
    print(f"  ⏭️ {name}  | 跳过：{reason}")
    _skipped += 1


def parse_result(result) -> dict:
    """MCP tools/call 返回 CallToolResult，把文本内容拼起来按 JSON 解析。"""
    parts = getattr(result, "content", None) or []
    text = "\n".join(c.text for c in parts if hasattr(c, "text"))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text, "_isError": bool(getattr(result, "isError", False))}


def schema_props(tool) -> tuple[dict, set]:
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
    props = {k: (v or {}).get("type") for k, v in (schema.get("properties") or {}).items()}
    return props, set(schema.get("required") or [])


async def call(session: ClientSession, name: str, args: dict) -> dict:
    return parse_result(await session.call_tool(name, args))


async def run(server: Path, mode_args: list[str], expect_mode: str | None,
              skip_dht: bool = False, skip_relay: bool = False) -> None:
    print(f"== PiAgent 设备 MCP 契约自检 ==\nServer: {server}\nPython: {sys.executable}\n")

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    params = StdioServerParameters(
        command=sys.executable, args=[str(server), *mode_args], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            print("[1] MCP 握手 initialize")
            init = await session.initialize()
            info = getattr(init, "serverInfo", None) or getattr(init, "server_info", None)
            check("握手成功并返回 serverInfo", info is not None and bool(getattr(info, "name", "")),
                  str(init)[:200])
            print(f"      server = {getattr(info, 'name', '?')} "
                  f"{getattr(info, 'version', '')} · protocol {getattr(init, 'protocolVersion', '?')}")

            print("\n[2] 工具发现 tools/list")
            listed = await session.list_tools()
            tools = {t.name: t for t in listed.tools}
            check("发现 ≥5 个工具", len(tools) >= 5, f"实际: {sorted(tools)}")
            for name in CONTRACT:
                check(f"工具 {name} 存在", name in tools)

            print("\n[3] 参数 schema 与契约一致")
            for name, (want_props, want_required) in CONTRACT.items():
                tool = tools.get(name)
                if tool is None:
                    continue
                props, required = schema_props(tool)
                check(f"{name} 参数类型", props == want_props, f"期望 {want_props}，实际 {props}")
                check(f"{name} 必填参数", required == want_required,
                      f"期望 {want_required}，实际 {required}")
                check(f"{name} 有描述", bool(getattr(tool, "description", "")), "")

            print("\n[4] 执行器调用 tools/call")
            r = await call(session, "led.set", {"on": True})
            check("led.set(true) → ok/led=true", r.get("ok") is True and r.get("led") is True, str(r))
            r = await call(session, "led.set", {"on": False})
            check("led.set(false) → ok/led=false", r.get("ok") is True and r.get("led") is False, str(r))
            r = await call(session, "fan.set", {"state": True})
            check("fan.set(true) → ok/fan=true", r.get("ok") is True and r.get("fan") is True, str(r))
            r = await call(session, "fan.set", {"state": False})
            check("fan.set(false) → ok/fan=false", r.get("ok") is True and r.get("fan") is False, str(r))

            print("\n[5] 继电器通道 + 越界保护")
            if skip_relay:
                skip("relay.set(0,true)", "指定了 --skip-relay（未接继电器）")
            else:
                r = await call(session, "relay.set", {"ch": 0, "state": True})
                check("relay.set(0,true) → ok/ch=0",
                      r.get("ok") is True and r.get("ch") == 0, str(r))
            r = await call(session, "relay.set", {"ch": 99, "state": True})
            check("relay.set(99) 被拒绝（ok=false）", r.get("ok") is False and "越界" in r.get("error", ""),
                  str(r))

            print("\n[6] 传感器与状态快照")
            if skip_dht:
                skip("dht.read", "指定了 --skip-dht（未接温湿度传感器）")
            else:
                r = await call(session, "dht.read", {})
                temp, hum = r.get("temp"), r.get("hum")
                check("dht.read → ok + 数值合理", r.get("ok") is True
                      and isinstance(temp, (int, float)) and isinstance(hum, (int, float))
                      and -50 <= temp <= 80 and 0 <= hum <= 100, str(r))
            r = await call(session, "devices.status", {})
            check(f"devices.status → 含 led/fan/relay/mode"
                  + (f" 且 mode={expect_mode}" if expect_mode else ""),
                  r.get("ok") is True and {"led", "fan", "relay", "mode"} <= set(r)
                  and isinstance(r.get("relay"), list)
                  and (not expect_mode or r.get("mode") == expect_mode),
                  f"实际 mode={r.get('mode')!r}（sim=仿真，gpio=真机）；{r}")
            print(f"      状态快照: {json.dumps(r, ensure_ascii=False)}")

            print("\n[7] 协议层参数校验（错误类型应被拒绝，而不是照单全收）")

            def rejected(result) -> bool:
                data = parse_result(result)
                return bool(getattr(result, "isError", False)) or data.get("ok") is False \
                    or "_raw" in data

            bad_ok = False
            try:
                bad = await session.call_tool("led.set", {"on": {"bad": 1}})
                bad_ok = rejected(bad)
            except Exception:  # noqa: BLE001  SDK 直接在协议层报 validation error 也算通过
                bad_ok = True
            check("led.set(on={...}) 被拒绝", bad_ok)

            unknown_rejected = False
            try:
                res = await session.call_tool("not.a.tool", {})
                unknown_rejected = rejected(res)
            except Exception:  # noqa: BLE001  未知工具应返回 JSON-RPC error
                unknown_rejected = True
            check("未知工具调用返回协议错误", unknown_rejected)

    summary = f"\n== 结果: {_passed} 通过, {_failed} 失败"
    if _skipped:
        summary += f", {_skipped} 跳过"
    print(summary + " ==")
    if _failed == 0 and expect_mode == "gpio":
        print("✅ 真机验收通过：GPIO 输出、继电器通道与传感器读取均正常。")
    elif _failed == 0 and expect_mode == "sim":
        print("✅ 仿真验收通过：MCP 协议与工具契约正常（未验证真实接线）。")
    if _failed:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="设备 MCP Server 契约自检")
    parser.add_argument("--server", default=str(DEFAULT_SERVER), help="device_mcp_server.py 路径")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--sim", action="store_true", help="强制仿真（不动 GPIO）")
    group.add_argument("--gpio", action="store_true", help="真机模式（会真实驱动硬件）")
    parser.add_argument("--skip-dht", action="store_true", help="跳过温湿度读取（未接传感器时）")
    parser.add_argument("--skip-relay", action="store_true", help="跳过继电器动作（未接继电器时）")
    args = parser.parse_args()

    mode_args = ["--sim"] if args.sim else (["--gpio"] if args.gpio else [])
    expect_mode = "sim" if args.sim else ("gpio" if args.gpio else None)
    try:
        asyncio.run(run(Path(args.server), mode_args, expect_mode,
                        skip_dht=args.skip_dht, skip_relay=args.skip_relay))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
