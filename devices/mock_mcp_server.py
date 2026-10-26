#!/usr/bin/env python3
"""模拟设备 MCP Server —— 分工 D（硬件组）的占位实现。

在没有真实硬件 / 未接线时提供与正式版同名的 MCP 工具，供全链路联调：
    led.set(on)          LED 灯开关
    fan.set(state)       风扇（继电器驱动）开关
    relay.set(ch,state)  继电器通道控制（高危）
    dht.read()           温湿度传感器读数（缓慢漂移的模拟值）

分工 D 替换方案：用 gpiozero/smbus2 实现同名工具（保持参数 schema 一致），
Agent 与上层零改动。工具契约见 docs/接口文档/mcp_tools_schema.md。

运行方式：由 Agent 按 [[mcp.servers]] 配置以 stdio 子进程拉起，
也可手动 `python mock_mcp_server.py` 直接在终端上观察。
"""
import json
import logging
import math
import random
import time

# MCP Python SDK 2.x 更名为 MCPServer，1.x 叫 FastMCP，做双版本兼容
try:
    from mcp.server.mcpserver import MCPServer as FastMCP  # mcp SDK >= 2.0
except ModuleNotFoundError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s [mock-device] %(message)s")
log = logging.getLogger("mock-devices")

mcp = FastMCP("mock-devices")

# 模拟硬件状态（内存态；真实实现里对应 GPIO 电平/继电器吸合）
STATE = {"led": False, "fan": False, "relay": [False] * 4}


def _ret(**kw) -> str:
    return json.dumps({"ok": True, **kw}, ensure_ascii=False)


@mcp.tool(name="led.set", description="控制 LED 灯。on=true 点亮，false 熄灭。")
def led_set(on: bool) -> str:
    STATE["led"] = bool(on)
    log.info("⚡ LED %s", "点亮" if on else "熄灭")
    return _ret(led=STATE["led"], detail="LED 已点亮" if on else "LED 已熄灭")


@mcp.tool(name="fan.set", description="控制风扇（继电器驱动）。state=true 转动，false 停止。")
def fan_set(state: bool) -> str:
    STATE["fan"] = bool(state)
    log.info("🌀 风扇 %s", "开始转动（继电器吸合）" if state else "停止")
    return _ret(fan=STATE["fan"], detail="风扇已打开" if state else "风扇已关闭")


@mcp.tool(name="relay.set",
          description="控制继电器通道（高危：可能连接大电流负载）。ch 通道号 0-3，state true=闭合。")
def relay_set(ch: int, state: bool) -> str:
    if not 0 <= ch <= 3:
        return json.dumps({"ok": False, "error": f"通道号 {ch} 越界（0-3）"}, ensure_ascii=False)
    STATE["relay"][ch] = bool(state)
    log.info("🔌 继电器 ch%s %s", ch, "闭合" if state else "断开")
    return _ret(ch=ch, state=STATE["relay"][ch])


@mcp.tool(name="dht.read", description="读取温湿度传感器。返回当前温度（°C）与相对湿度（%）。")
def dht_read() -> str:
    t = round(26.0 + 1.5 * math.sin(time.time() / 600) + random.uniform(-0.3, 0.3), 1)
    h = round(55 + 5 * math.sin(time.time() / 900) + random.uniform(-2, 2), 1)
    log.info("🌡 读数 temp=%s hum=%s", t, h)
    return json.dumps({"temp": t, "hum": h}, ensure_ascii=False)


@mcp.tool(name="devices.status", description="读取全部模拟设备的当前状态（调试用）。")
def devices_status() -> str:
    return json.dumps(STATE, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
