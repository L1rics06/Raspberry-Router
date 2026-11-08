#!/usr/bin/env python3
"""PiAgent 设备 MCP Server —— 分工4（硬件适配组）正式实现。

把树莓派的物理设备封装成 MCP 工具，通过 **stdio** 由 Agent（分工3）拉起：

    led.set(on)            控制 LED                    GPIO 数字输出
    fan.set(state)         控制风扇（经继电器/MOS）      GPIO 数字输出
    relay.set(ch, state)   控制继电器通道（高危）        GPIO 数字输出
    dht.read()             读取温湿度                    AHT20（I2C）
    devices.status()       全部设备状态快照（调试用）    只读

设计要点
--------
1. **契约优先**：工具名、参数名、参数类型与 `docs/接口文档/mcp_tools_schema.md`
   完全一致，分工3 的 Agent 代码零改动即可从 mock 切换到本文件。
2. **双模式**：树莓派上走 `gpiozero`（Pi 5 用 lgpio 后端）；没有硬件的电脑上
   自动进入仿真模式（内存态 + 缓慢漂移的温湿度），用于联调与自测。
   显式切换：`--sim` / `--gpio`，或环境变量 `PIAGENT_MCP_SIM=1/0`。
3. **永不把异常抛给协议层**：所有工具都返回 JSON 字符串，
   成功 `{"ok": true, ...}`，失败 `{"ok": false, "error": "..."}`。
4. **stdout 归 MCP 协议所有**：日志一律写 stderr（`logging` 默认即 stderr）。
5. **单设备故障不拖垮整体**：某个引脚初始化失败只禁用该设备，`devices.status`
   会列出 `errors`，其余工具继续可用。

运行方式（通常由 Agent 以子进程拉起，不需要手动运行）：

    python device_mcp_server.py --sim      # 无硬件，仿真
    python device_mcp_server.py            # 树莓派真机（自动识别）

环境变量见 devices/README.md；工具契约见 docs/接口文档/mcp_tools_schema.md。
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import math
import os
import platform
import random
import sys
import threading
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# MCP SDK 双版本兼容：2.x 叫 MCPServer，1.x 叫 FastMCP
# ---------------------------------------------------------------------------
try:  # mcp SDK >= 2.0
    from mcp.server.mcpserver import MCPServer as FastMCP
except ModuleNotFoundError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP

log = logging.getLogger("device-mcp")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def _env_int(name: str, default: int | None, base: int = 10) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip(), base)
    except ValueError:
        log.warning("环境变量 %s=%r 不是整数，改用默认值 %s", name, raw, default)
        return default


def _env_pins(name: str, default: list[int]) -> list[int]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return list(default)
    try:
        pins = [int(x.strip(), 10) for x in raw.split(",") if x.strip() != ""]
        return pins or list(default)
    except ValueError:
        log.warning("环境变量 %s=%r 格式错误（应为 5,6,13,19），改用默认值", name, raw)
        return list(default)


def _has_gpiozero() -> bool:
    try:
        import gpiozero  # noqa: F401
        return True
    except Exception:  # noqa: BLE001  导入失败可能不只是 ModuleNotFoundError
        return False


@dataclass
class DeviceConfig:
    """引脚与后端配置（全部可由环境变量覆盖）。"""

    sim: bool = False
    led_pin: int | None = 17
    fan_pin: int | None = 27
    relay_pins: list[int] = field(default_factory=lambda: [5, 6, 13, 19])
    active_high: bool = True          # LED/风扇：高电平有效
    relay_active_high: bool = True    # 继电器模块：低电平触发模块设 0
    dht_backend: str = "aht20"        # aht20 | none（可扩展 dht22）
    dht_bus: int = 1                  # I2C 总线号
    dht_addr: int = 0x38              # AHT20 默认地址

    @classmethod
    def from_env(cls, force_sim: bool = False, force_gpio: bool = False) -> "DeviceConfig":
        if force_sim and force_gpio:
            raise SystemExit("--sim 与 --gpio 不能同时使用")

        if force_sim:
            sim = True
        elif force_gpio:
            sim = False
        elif os.getenv("PIAGENT_MCP_SIM") is not None:
            sim = _env_bool("PIAGENT_MCP_SIM", False)
        else:
            # 非 Linux（Windows/macOS 开发机）默认仿真；树莓派上默认真机
            sim = platform.system() != "Linux" or not _has_gpiozero()

        return cls(
            sim=sim,
            led_pin=_env_int("PIAGENT_LED_PIN", 17),
            fan_pin=_env_int("PIAGENT_FAN_PIN", 27),
            relay_pins=_env_pins("PIAGENT_RELAY_PINS", [5, 6, 13, 19]),
            active_high=_env_bool("PIAGENT_ACTIVE_HIGH", True),
            relay_active_high=_env_bool("PIAGENT_RELAY_ACTIVE_HIGH", True),
            dht_backend=(os.getenv("PIAGENT_DHT_BACKEND", "aht20").strip().lower() or "none"),
            dht_bus=_env_int("PIAGENT_DHT_BUS", 1) or 1,
            dht_addr=_env_int("PIAGENT_DHT_ADDR", 0x38, base=0) or 0x38,
        )

    def summary(self) -> dict:
        return {
            "led_pin": self.led_pin,
            "fan_pin": self.fan_pin,
            "relay_pins": self.relay_pins,
            "active_high": self.active_high,
            "relay_active_high": self.relay_active_high,
            "dht_backend": self.dht_backend,
            "dht_bus": self.dht_bus,
            "dht_addr": hex(self.dht_addr),
        }


# ---------------------------------------------------------------------------
# I2C 温湿度：AHT20（项目推荐，比 DHT22 稳定且 Pi 5 可用）
# ---------------------------------------------------------------------------


class AHT20:
    """AHT20 / AHT21 温湿度传感器的最小驱动（smbus2）。

    数据手册流程：上电等待 40ms → 状态字节 bit3=0 时写 0xBE 校准
    → 写 0xAC 0x33 0x00 触发测量 → 等 80ms → 读 7 字节（状态 + 6 字节数据）。
    """

    def __init__(self, bus_num: int = 1, addr: int = 0x38) -> None:
        from smbus2 import SMBus  # 延迟导入：仿真模式无需该依赖

        self._addr = addr
        self._bus = SMBus(bus_num)
        time.sleep(0.05)
        status = self._bus.read_byte(addr)
        if not status & 0x08:  # 未校准
            self._bus.write_byte(addr, 0xBE)
            time.sleep(0.01)
        log.info("AHT20 就绪 (i2c-%s addr=0x%02X)", bus_num, addr)

    def read(self) -> tuple[float, float]:
        self._bus.write_i2c_block_data(self._addr, 0xAC, [0x33, 0x00])
        time.sleep(0.08)
        data = self._bus.read_i2c_block_data(self._addr, 0x00, 7)
        if data[0] & 0x80:
            raise RuntimeError("AHT20 忙（状态位 bit7=1），请稍后重试")

        raw_h = (data[1] << 12) | (data[2] << 8) | data[3]
        raw_t = ((data[3] & 0x0F) << 16) | (data[4] << 8) | data[5]
        hum = raw_h / 1048576.0 * 100.0
        temp = raw_t / 1048576.0 * 200.0 - 50.0
        return round(temp, 1), round(hum, 1)


# ---------------------------------------------------------------------------
# 硬件后端 1：真实 GPIO
# ---------------------------------------------------------------------------


class GpioBackend:
    """树莓派 GPIO 实现：gpiozero + lgpio（Pi 5 兼容，禁用 RPi.GPIO）。"""

    mode = "gpio"

    def __init__(self, cfg: DeviceConfig) -> None:
        from gpiozero import DigitalOutputDevice, LED

        self.cfg = cfg
        self.errors: dict[str, str] = {}

        # 逐个设备初始化：一个引脚有问题只禁用该设备，不拖垮整个 Server
        self._led = self._make("led", lambda: LED(
            cfg.led_pin, active_high=cfg.active_high, initial_value=False))
        self._fan = self._make("fan", lambda: DigitalOutputDevice(
            cfg.fan_pin, active_high=cfg.active_high, initial_value=False))
        self._relays = []
        for idx, pin in enumerate(cfg.relay_pins):
            dev = self._make(f"relay{idx}", lambda p=pin: DigitalOutputDevice(
                p, active_high=cfg.relay_active_high, initial_value=False))
            self._relays.append(dev)

        self._dht = None
        if cfg.dht_backend == "aht20":
            try:
                self._dht = AHT20(cfg.dht_bus, cfg.dht_addr)
            except Exception as e:  # noqa: BLE001
                self.errors["dht"] = f"AHT20 初始化失败: {e}"
                log.warning("温湿度传感器不可用：%s（dht.read 将返回错误，其余工具正常）", e)
        elif cfg.dht_backend != "none":
            self.errors["dht"] = f"不支持的温湿度后端 {cfg.dht_backend!r}（可选 aht20 / none）"

        if self.errors:
            log.warning("部分设备初始化失败：%s", self.errors)

    def _make(self, name: str, factory):
        try:
            return factory()
        except Exception as e:  # noqa: BLE001
            self.errors[name] = f"{type(e).__name__}: {e}"
            log.error("设备 %s 初始化失败：%s", name, e)
            return None

    # ---- 执行器 ----

    def set_led(self, on: bool) -> bool:
        if self._led is None:
            raise RuntimeError(self.errors.get("led", "LED 未接线或初始化失败"))
        self._led.value = 1 if on else 0
        return bool(self._led.value)

    def set_fan(self, state: bool) -> bool:
        if self._fan is None:
            raise RuntimeError(self.errors.get("fan", "风扇未接线或初始化失败"))
        self._fan.value = 1 if state else 0
        return bool(self._fan.value)

    def set_relay(self, ch: int, state: bool) -> bool:
        if ch >= len(self._relays) or self._relays[ch] is None:
            detail = self.errors.get(f"relay{ch}", f"继电器通道 {ch} 未接线")
            raise RuntimeError(detail)
        self._relays[ch].value = 1 if state else 0
        return bool(self._relays[ch].value)

    # ---- 传感器 ----

    def read_dht(self) -> tuple[float, float]:
        if self._dht is None:
            raise RuntimeError(self.errors.get(
                "dht", f"温湿度传感器未配置（PIAGENT_DHT_BACKEND={self.cfg.dht_backend}）"))
        return self._dht.read()

    # ---- 状态快照 ----

    def snapshot(self) -> dict:
        return {
            "led": bool(self._led.value) if self._led is not None else None,
            "fan": bool(self._fan.value) if self._fan is not None else None,
            "relay": [bool(d.value) if d is not None else None for d in self._relays],
        }

    def close(self) -> None:
        for dev in [self._led, self._fan, *self._relays]:
            try:
                if dev is not None:
                    dev.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._dht is not None:
                self._dht._bus.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 硬件后端 2：仿真（无树莓派时的联调/自测）
# ---------------------------------------------------------------------------


class SimBackend:
    """内存态仿真：与 mock_mcp_server.py 行为一致，另加 mode/pins 便于排查。"""

    mode = "sim"

    def __init__(self, cfg: DeviceConfig) -> None:
        self.cfg = cfg
        self.errors: dict[str, str] = {}
        self._led = False
        self._fan = False
        self._relay = [False] * len(cfg.relay_pins)

    def set_led(self, on: bool) -> bool:
        self._led = bool(on)
        return self._led

    def set_fan(self, state: bool) -> bool:
        self._fan = bool(state)
        return self._fan

    def set_relay(self, ch: int, state: bool) -> bool:
        self._relay[ch] = bool(state)
        return self._relay[ch]

    def read_dht(self) -> tuple[float, float]:
        # 缓慢正弦漂移 + 小噪声：让前端曲线看起来像真的
        t = round(26.0 + 1.5 * math.sin(time.time() / 600) + random.uniform(-0.3, 0.3), 1)
        h = round(55 + 5 * math.sin(time.time() / 900) + random.uniform(-2, 2), 1)
        return t, h

    def snapshot(self) -> dict:
        return {"led": self._led, "fan": self._fan, "relay": list(self._relay)}

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# MCP Server 与工具定义
# ---------------------------------------------------------------------------

mcp = FastMCP("piagent-devices")

_HW: GpioBackend | SimBackend | None = None
_CFG: DeviceConfig | None = None

# 硬件操作串行化：MCP 同步工具在线程池执行，避免并发写 GPIO / I2C
_LOCK = threading.RLock()


def _ok(**kw) -> str:
    return json.dumps({"ok": True, **kw}, ensure_ascii=False)


def _err(message: str, **kw) -> str:
    return json.dumps({"ok": False, "error": message, **kw}, ensure_ascii=False)


def _hw() -> GpioBackend | SimBackend:
    if _HW is None:
        raise RuntimeError("硬件后端未初始化（Server 尚未完成启动）")
    return _HW


@mcp.tool(name="led.set", description="控制 LED 灯。on=true 点亮，false 熄灭。")
def led_set(on: bool) -> str:
    if not isinstance(on, bool):
        return _err("参数 on 必须是布尔值 true/false")
    try:
        hw = _hw()
        with _LOCK:
            actual = hw.set_led(on)
    except Exception as e:  # noqa: BLE001
        log.error("led.set 失败: %s", e)
        return _err(f"LED 控制失败: {e}")

    log.info("LED %s", "点亮" if actual else "熄灭")
    return _ok(led=actual, detail="LED 已点亮" if actual else "LED 已熄灭")


@mcp.tool(name="fan.set", description="控制风扇（经继电器或 MOS 管驱动）。state=true 转动，false 停止。")
def fan_set(state: bool) -> str:
    if not isinstance(state, bool):
        return _err("参数 state 必须是布尔值 true/false")
    try:
        hw = _hw()
        with _LOCK:
            actual = hw.set_fan(state)
    except Exception as e:  # noqa: BLE001
        log.error("fan.set 失败: %s", e)
        return _err(f"风扇控制失败: {e}")

    log.info("风扇 %s", "开始转动" if actual else "停止")
    return _ok(fan=actual, detail="风扇已打开" if actual else "风扇已关闭")


@mcp.tool(
    name="relay.set",
    description=(
        "控制继电器通道（高危：可能连接大电流负载）。ch 为通道号（0 起），"
        "state=true 表示闭合（通电）。聊天指令默认被安全闸门拦截，"
        "仅手动控制台 confirmed=true 时执行。"
    ),
)
def relay_set(ch: int, state: bool) -> str:
    if not isinstance(state, bool):
        return _err("参数 state 必须是布尔值 true/false")
    hw = _hw() if _HW is not None else None
    if hw is None:
        return _err("硬件后端未初始化")
    max_ch = len(hw.cfg.relay_pins)
    if isinstance(ch, bool) or not isinstance(ch, int) or not 0 <= ch < max_ch:
        return _err(f"通道号 {ch!r} 越界：本机已配置 {max_ch} 路（0-{max_ch - 1}）",
                    channels=max_ch)
    try:
        with _LOCK:
            actual = hw.set_relay(ch, state)
    except Exception as e:  # noqa: BLE001
        log.error("relay.set 失败: %s", e)
        return _err(f"继电器控制失败: {e}")

    log.info("继电器 ch%s %s", ch, "闭合" if actual else "断开")
    return _ok(ch=ch, state=actual, detail=f"继电器通道 {ch} 已{'闭合' if actual else '断开'}")


@mcp.tool(
    name="dht.read",
    description="读取温湿度传感器。返回当前温度（°C，temp）与相对湿度（%，hum）。",
)
def dht_read() -> str:
    try:
        with _LOCK:
            temp, hum = _hw().read_dht()
    except Exception as e:  # noqa: BLE001
        log.error("dht.read 失败: %s", e)
        return _err(f"温湿度读取失败: {e}")

    log.info("温湿度 temp=%s hum=%s", temp, hum)
    return json.dumps({"ok": True, "temp": temp, "hum": hum}, ensure_ascii=False)


@mcp.tool(
    name="devices.status",
    description="读取全部设备的当前状态快照（调试用）：led / fan / relay / 运行模式 / 引脚 / 错误。",
)
def devices_status() -> str:
    try:
        hw = _hw()
        cfg = _CFG or hw.cfg
        with _LOCK:
            snap = hw.snapshot()
    except Exception as e:  # noqa: BLE001
        return _err(f"状态读取失败: {e}")

    return json.dumps({
        "ok": True,
        **snap,
        "mode": hw.mode,
        "pins": cfg.summary(),
        "errors": getattr(hw, "errors", {}),
        "ts": round(time.time(), 3),
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 启动 / 关闭
# ---------------------------------------------------------------------------


def _setup_logging(level: str) -> None:
    """日志必须写 stderr —— stdout 是 MCP 的 JSON-RPC 通道。"""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [device-mcp] %(levelname)-5s %(message)s"))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def _shutdown() -> None:
    global _HW
    if _HW is not None:
        try:
            _HW.close()
            log.info("已释放 GPIO / I2C 资源")
        except Exception:  # noqa: BLE001
            pass
        _HW = None


def _build_backend(cfg: DeviceConfig) -> GpioBackend | SimBackend:
    if cfg.sim:
        log.info("运行在【仿真模式】——不驱动真实 GPIO（电脑联调/无硬件时正常）")
        return SimBackend(cfg)
    try:
        backend = GpioBackend(cfg)
        log.info("运行在【真机模式】——gpiozero/lgpio 已就绪")
        return backend
    except ImportError as e:
        log.error("缺少 gpiozero：在树莓派上执行 pip install -r devices/requirements.txt")
        raise SystemExit(f"GPIO 依赖缺失: {e}") from e
    except Exception as e:  # noqa: BLE001
        log.error("GPIO 初始化失败：%s", e)
        log.error("请检查接线、/dev/gpiochip 权限，或先用 --sim 仿真运行")
        raise SystemExit(f"GPIO 初始化失败: {e}") from e


def main(argv: list[str] | None = None) -> None:
    global _HW, _CFG

    parser = argparse.ArgumentParser(description="PiAgent 设备 MCP Server（stdio）")
    parser.add_argument("--sim", action="store_true", help="强制仿真模式（不碰 GPIO）")
    parser.add_argument("--gpio", action="store_true", help="强制真机模式（GPIO 不可用则报错退出）")
    parser.add_argument("--log-level", default=os.getenv("PIAGENT_LOG_LEVEL", "INFO"),
                        help="DEBUG / INFO / WARNING（默认 INFO）")
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)
    _CFG = DeviceConfig.from_env(force_sim=args.sim, force_gpio=args.gpio)
    _HW = _build_backend(_CFG)
    _CFG.sim = _HW.mode == "sim"
    atexit.register(_shutdown)

    log.info("设备 MCP Server 已启动 | mode=%s | pins=%s", _HW.mode,
             json.dumps(_CFG.summary(), ensure_ascii=False))
    log.info("工具: led.set / fan.set / relay.set / dht.read / devices.status")
    # stdio：由 Agent 作为子进程拉起，一行一个 JSON-RPC 消息
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
