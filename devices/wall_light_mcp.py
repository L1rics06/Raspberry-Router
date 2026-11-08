"""完整墙灯MCP入口，可选合并上游已有LED、风扇、继电器和传感器"""
from __future__ import annotations

import argparse
import importlib.util
import json
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP
from pydantic import StrictBool
from task4.pwm_driver import HardwarePWM, SimPWM
from task4.servo_control import ServoControl, load_settings
from task4.layout import device_entry


# merge_status 合并墙灯及原有设备状态，state和base分别为两个状态对象
# 原有设备错误保留在顶层，舵机错误在重名时添加servo前缀，输入对象不被改写
def merge_status(state: dict, base: dict) -> dict:
    if not isinstance(base, dict):
        raise ValueError("原有设备状态必须是JSON对象")
    result = dict(state)
    errors = base.get("errors", {})
    if not isinstance(errors, dict):
        errors = {"other_devices": "原有设备errors不是对象"}
    else:
        errors = dict(errors)
    if base.get("ok") is not True:
        errors.setdefault("other_devices", "原有设备状态返回失败")
    other_ready = base.get("ok") is True and not errors
    for key, value in state.get("errors", {}).items():
        name = key
        while name in errors:
            name = "servo." + name
        errors[name] = value
    result.update({"other_devices": base, "errors": errors,
                   "other_devices_ready": other_ready})
    for key in ("led", "fan", "relay", "pins"):
        if key in base:
            result[key] = base[key]
    return result


# make_server 创建MCP工具，control为舵机控制器，existing是可选上游模块
# 默认只控制寝室灯，合并模式沿用已有四类设备工具并统一状态入口
def make_server(control: ServoControl, existing=None):
    mcp = existing.mcp if existing is not None else FastMCP("pi5-wall-light")
    if existing is not None:
        mcp.remove_tool("devices.status")

    # light_set 接收布尔on执行一次完整按压，失败不会伪报亮灭
    @mcp.tool(name="light.set", description="控制寝室普通单控翘板开关，true按开灯端，false按关灯端，完成后退回；没有灯光传感器反馈")
    def light_set(on: StrictBool) -> str:
        return json.dumps(control.set_light(on), ensure_ascii=False)

    # devices_status 查询驱动和标定，无参数，不移动舵机
    @mcp.tool(name="devices.status", description="查询寝室灯舵机模式、就绪、标定和错误，lamp_state=unknown表示没有真实灯光反馈")
    def devices_status() -> str:
        state = control.status()
        if existing is not None:
            base = json.loads(existing.devices_status())
            state = merge_status(state, base)
        return json.dumps(state, ensure_ascii=False)

    return mcp


# load_existing 按显式模式加载已有设备，mode为sim或gpio
# 不启用时不导入GPIO库，真机加载失败直接报错，不回落模拟
def load_existing(mode: str):
    path = device_entry()
    spec = importlib.util.spec_from_file_location("maker_existing_devices", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._CFG = module.DeviceConfig.from_env(force_sim=mode == "sim", force_gpio=mode == "gpio")
    module._HW = module._build_backend(module._CFG)
    return module


# terminate 触发正常退出清理，参数由操作系统信号传入
def terminate(_signum, _frame):
    raise KeyboardInterrupt


# main 读取显式模式并运行stdio服务，argv可覆盖终端参数
# 构造控制器不发送PWM，Ctrl+C和SIGTERM会释放后端
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pi5寝室开关MCP")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--gpio", action="store_true")
    modes.add_argument("--sim", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--with-existing-devices", action="store_true")
    args = parser.parse_args(argv)
    mode = "gpio" if args.gpio else "sim"
    config_path = args.config or ROOT / ("configs/servo-simulation.toml" if args.sim else "configs/servo.local.toml")
    control = existing = None
    try:
        cfg = load_settings(config_path, mode)
        control = ServoControl(cfg, HardwarePWM() if args.gpio else SimPWM())
        existing = load_existing(mode) if args.with_existing_devices else None
        make_server(control, existing).run(transport="stdio")
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"墙灯MCP启动失败：{error}", file=sys.stderr)
        return 1
    finally:
        if control is not None:
            control.close()
        if existing is not None:
            existing._shutdown()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(main())
