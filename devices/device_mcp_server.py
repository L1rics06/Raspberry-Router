#!/usr/bin/env python3
"""真机设备 MCP Server —— 分工 D（硬件组）真机实现（舵机 SG90 版）。

与 mock_mcp_server.py 保持完全相同的工具契约（Agent / 网关 / 前端零改动）：
    servo.set(angle)     舵机角度控制（真实 GPIO，本版核心演示硬件）
    led.set(on)          LED 灯开关（模拟，未接线同样可演示全链路）
    fan.set(state)       风扇开关（模拟）
    relay.set(ch,state)  继电器通道控制（模拟，高危）
    dht.read()           温湿度读数（模拟值缓慢漂移）

接线（SG90 舵机，三根线）：
    信号线（橙/黄）→ GPIO17（环境变量 PIAGENT_SERVO_PIN 可改）
    VCC（红）      → 5V（演示轻载可取自树莓派；带负载请用外部 5V 电源）
    GND（棕）      → GND（外部供电时必须与树莓派共地）

运行要求：仅树莓派上需要 `pip install -r requirements-device.txt`（gpiozero）；
在非树莓派环境（无 /dev/gpiomem）或初始化失败时自动降级为模拟模式，
工具与返回结构不变，返回中的 mode 字段标明 gpio / simulated。
"""
import atexit
import json
import logging
import math
import os
import random
import time

# MCP Python SDK 2.x 更名为 MCPServer，1.x 叫 FastMCP，做双版本兼容
try:
    from mcp.server.mcpserver import MCPServer as FastMCP  # mcp SDK >= 2.0
except ModuleNotFoundError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s [device] %(message)s")
log = logging.getLogger("real-devices")

mcp = FastMCP("real-devices")

STATE = {"led": False, "fan": False, "relay": [False] * 4, "servo": 0}

# ---------------- 舵机：真实 GPIO 优先，失败自动降级模拟 ----------------

_servo = None
_servo_mode = "simulated"


def _release_servo() -> None:
    try:
        if _servo is not None:
            _servo.detach()   # 停发脉冲，避免舵机持续抖动/发热
            _servo.close()
    except Exception:  # noqa: BLE001
        pass


def _init_servo() -> None:
    global _servo, _servo_mode
    if os.environ.get("PIAGENT_SERVO_SIM") == "1":
        log.warning("PIAGENT_SERVO_SIM=1，舵机强制运行在模拟模式")
        return
    if not os.path.exists("/dev/gpiomem"):
        log.warning("无 /dev/gpiomem（非树莓派或未启用 GPIO），舵机运行在模拟模式")
        return
    try:
        import gpiozero
        try:
            from gpiozero.pins.lgpio import LGPIOFactory  # rpi-lgpio 包，树莓派5 兼容
            gpiozero.Device.pin_factory = LGPIOFactory()
            log.info("使用 lgpio 引脚后端")
        except Exception:  # noqa: BLE001
            pass  # 回退 gpiozero 默认后端（老款 Pi 的 RPi.GPIO 等）
        from gpiozero import AngularServo
        pin = int(os.environ.get("PIAGENT_SERVO_PIN", "17"))
        _servo = AngularServo(pin, initial_angle=0, min_angle=0, max_angle=180)
        _servo_mode = "gpio"
        atexit.register(_release_servo)
        log.info("舵机已挂载 GPIO%d（0-180°，真实硬件模式）", pin)
    except Exception as e:  # noqa: BLE001
        log.warning("舵机 GPIO 初始化失败（%s），降级为模拟模式", e)


_init_servo()


def _ret(**kw) -> str:
    return json.dumps({"ok": True, **kw}, ensure_ascii=False)


@mcp.tool(name="servo.set",
          description="控制舵机转动角度。angle 取 0-180 度：90=打开/开启位置，0=关闭/复位位置，180=另一侧最大角度。")
def servo_set(angle: int) -> str:
    if not 0 <= angle <= 180:
        return json.dumps({"ok": False, "error": f"角度 {angle} 越界（0-180）"}, ensure_ascii=False)
    STATE["servo"] = int(angle)
    if _servo is not None:
        try:
            _servo.angle = angle
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": f"舵机驱动异常: {e}"}, ensure_ascii=False)
        log.info("🦾 舵机(GPIO) 转到 %s°", angle)
    else:
        log.info("🦾 舵机(模拟) 转到 %s°", angle)
    return _ret(servo=angle, mode=_servo_mode, detail=f"舵机已转到 {angle}°")


@mcp.tool(name="led.set", description="控制 LED 灯。on=true 点亮，false 熄灭。（本版未接 LED 硬件，为模拟实现）")
def led_set(on: bool) -> str:
    STATE["led"] = bool(on)
    log.info("⚡ LED(模拟) %s", "点亮" if on else "熄灭")
    return _ret(led=STATE["led"], mode="simulated",
                detail="LED 已点亮" if on else "LED 已熄灭")


@mcp.tool(name="fan.set", description="控制风扇（继电器驱动）。state=true 转动，false 停止。（本版为模拟实现）")
def fan_set(state: bool) -> str:
    STATE["fan"] = bool(state)
    log.info("🌀 风扇(模拟) %s", "开始转动" if state else "停止")
    return _ret(fan=STATE["fan"], mode="simulated",
                detail="风扇已打开" if state else "风扇已关闭")


@mcp.tool(name="relay.set",
          description="控制继电器通道（高危：可能连接大电流负载）。ch 通道号 0-3，state true=闭合。（本版为模拟实现）")
def relay_set(ch: int, state: bool) -> str:
    if not 0 <= ch <= 3:
        return json.dumps({"ok": False, "error": f"通道号 {ch} 越界（0-3）"}, ensure_ascii=False)
    STATE["relay"][ch] = bool(state)
    log.info("🔌 继电器(模拟) ch%s %s", ch, "闭合" if state else "断开")
    return _ret(ch=ch, state=STATE["relay"][ch], mode="simulated")


@mcp.tool(name="dht.read", description="读取温湿度传感器。返回当前温度（°C）与相对湿度（%）。（本版为模拟读数）")
def dht_read() -> str:
    t = round(26.0 + 1.5 * math.sin(time.time() / 600) + random.uniform(-0.3, 0.3), 1)
    h = round(55 + 5 * math.sin(time.time() / 900) + random.uniform(-2, 2), 1)
    log.info("🌡 读数(模拟) temp=%s hum=%s", t, h)
    return json.dumps({"temp": t, "hum": h}, ensure_ascii=False)


@mcp.tool(name="devices.status", description="读取全部设备的当前状态（含舵机硬件模式，调试用）。")
def devices_status() -> str:
    return json.dumps({**STATE, "servo_mode": _servo_mode}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
