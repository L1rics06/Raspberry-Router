"""舵机限幅、逐步移动、标定和寝室开关动作"""
from __future__ import annotations

import json
import os
import threading
import time
import tomllib
from pathlib import Path

from .common import now_text
from .profile import validate_calibration
from .servo_specs import SERVO_MODEL, settings_errors


# load_settings 读取舵机TOML，path为配置路径，mode为sim或gpio
# 真机拒绝仿真标定，初始窗口保留为小范围，不自行扩大行程
def load_settings(path: Path, mode: str) -> dict:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))
    for key, value in (("gpio_bcm", 12), ("physical_pin", 32), ("pwm_channel", 0), ("frequency_hz", 50)):
        if type(data.get(key)) is not int or data[key] != value:
            raise ValueError(f"本驱动固定{key}={value}，不能仅改配置而不改硬件复用")
    if mode == "gpio" and data.get("fixture") is True:
        raise ValueError("仿真标定不能用于真实GPIO")
    errors = settings_errors(data)
    if errors:
        raise ValueError("；".join(errors))
    for key in ("min_us", "max_us", "home_us", "step_us"):
        if type(data.get(key)) is not int:
            raise ValueError(f"{key}必须是整数")
    if not data["min_us"] < data["max_us"] or not data["min_us"] <= data["home_us"] <= data["max_us"]:
        raise ValueError("脉宽范围或home_us不合法")
    if not 1 <= data["step_us"] <= 20:
        raise ValueError("step_us只允许1～20微秒")
    if type(data.get("frame_s")) not in (int, float) or not 0.02 <= data["frame_s"] <= 0.05:
        raise ValueError("frame_s只允许0.02～0.05秒")
    if type(data.get("wait_s")) not in (int, float) or not 0.05 <= data["wait_s"] <= 1.5:
        raise ValueError("wait_s只允许0.05～1.5秒")
    travel_s = 2 * (data["max_us"] - data["min_us"]) / data["step_us"] * data["frame_s"] + 3 * data["wait_s"]
    if travel_s > 4.5:
        raise ValueError("当前步进/等待配置超过4.5秒预算，请缩小范围以满足上游5秒工具约定")
    return data


# save_settings 原子保存标定，path为目标，data为舵机配置
# 只保存简单字段，拒绝覆盖仿真夹具或配置丢失时随意驱动
def save_settings(path: Path, data: dict) -> None:
    lines = ["# 现场测得的舵机标定，禁止把测试夹具参数用于真机"]
    for key, value in data.items():
        if type(value) in (str, int, float, bool):
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=False, allow_nan=False)}")
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temp, path)


# calibration_errors 返回标定缺口，data为配置
# 普通单控翘板开关才可用两个固定位置表达开和关
def calibration_errors(data: dict) -> list[str]:
    errors = validate_calibration(data) + settings_errors(data)
    if data.get("switch_type") != "single_rocker":
        errors.append("尚未确认普通单控翘板开关")
    return errors


class ServoControl:
    """控制器负责动作流程，PWM驱动负责波形，状态保留为可调试字段"""

    # __init__ 接收配置和PWM后端，sleep仅供单元测试替代等待
    # 构造时不运动，初次中位测试必须拆开舵盘与开关的接触
    def __init__(self, settings: dict, backend, *, sleep=time.sleep):
        self.settings = dict(settings)
        self.backend = backend
        self.sleep = sleep if backend.mode == "gpio" else lambda _seconds: None
        self.lock = threading.RLock()
        self.current_us = None
        self.requested_on = None
        self.command_count = 0
        self.trace = []
        self.fault = ""
        self.stopping = threading.Event()

    # move_us 逐步移动到target脉宽，target单位微秒
    # current_us仅记录已发送指令，没有位置传感器时不等于实测角度
    def move_us(self, target: int) -> None:
        cfg = self.settings
        if self.stopping.is_set():
            raise RuntimeError("控制器正在停止")
        if type(target) is not int or not cfg["min_us"] <= target <= cfg["max_us"]:
            raise ValueError("目标超出当前标定窗口")
        if self.current_us is None or self.current_us == target:
            self.backend.pulse_us(target)
            self.current_us = target
            return
        direction = 1 if target > self.current_us else -1
        while self.current_us != target:
            if self.stopping.is_set():
                raise RuntimeError("控制器正在停止")
            step = min(cfg["step_us"], abs(target - self.current_us))
            value = self.current_us + direction * step
            self.backend.pulse_us(value)
            self.current_us = value
            self.sleep(cfg["frame_s"])

    # status 查询控制器状态，无参数，不发送脉冲
    # 有硬件驱动也只汇报命令，不凭空声称灯光或角度反馈
    def status(self) -> dict:
        with self.lock:
            errors = calibration_errors(self.settings)
            return {"ok": True, "mode": self.backend.mode, "ready": not errors and not self.fault and not self.stopping.is_set(),
                    "servo_model": SERVO_MODEL, "power_nominal_v": 5.0,
                    "calibrated": not errors, "lamp_state": "unknown", "fixture": self.backend.mode == "sim",
                    "gpio_bcm": 12, "physical_pin": 32, "channel": 0, "frequency_hz": 50,
                    "command_count": self.command_count, "requested_on": self.requested_on,
                    "last_command_us": self.current_us, "trace": list(self.trace),
                    "errors": {"fault": self.fault} if self.fault else {}, "calibration_errors": errors}

    # set_light 执行开或关，on必须是真正布尔值
    # 正常流程为回中、按压、退回、停PWM，故障后锁住后续动作
    def set_light(self, on: bool) -> dict:
        with self.lock:
            result = {"ok": False, "mode": self.backend.mode, "requested_on": on, "lamp_state": "unknown"}
            errors = calibration_errors(self.settings)
            if type(on) is not bool or errors or self.fault:
                result["error"] = self.fault or "; ".join(errors) or "on必须为布尔值"
                return result
            try:
                if self.backend.mode == "sim" and os.environ.get("TASK4_MOCK_CONTROL"):
                    fixture = json.loads(Path(os.environ["TASK4_MOCK_CONTROL"]).read_text(encoding="utf-8"))
                    if fixture.get("failure"):
                        result["error"] = "SIMULATED_DEVICE_FAILURE"
                        return result
                self.trace = []
                for name, pulse in (("home", self.settings["home_us"]),
                                    ("press_on" if on else "press_off", self.settings["on_us" if on else "off_us"]),
                                    ("home", self.settings["home_us"])):
                    self.move_us(pulse)
                    self.trace.append(name)
                    self.sleep(self.settings["wait_s"])
                self.backend.release()
                self.command_count += 1
                self.requested_on = on
                result.update(ok=True, trace=list(self.trace), last_command_us=self.current_us, completed_at=now_text())
            except Exception as error:
                self.fault = str(error)
                result["error"] = self.fault
                result["trace"] = list(self.trace)
            finally:
                try:
                    self.backend.release()
                except Exception as error:
                    self.fault = str(error)
                    result.update(ok=False, error="PWM停止失败：" + self.fault)
            return result

    # pulse_once 空载发送一个脉宽，target为目标，hold_s为保持秒数
    # 不要求已完成机构标定，仅供明确启用的现场调试
    def pulse_once(self, target: int, hold_s: float) -> dict:
        with self.lock:
            if type(hold_s) not in (int, float) or not 0.05 <= hold_s <= 1.5:
                raise ValueError("保持时间只允许0.05～1.5秒")
            try:
                self.move_us(target)
                self.sleep(hold_s)
                return {"ok": True, "mode": self.backend.mode, "last_command_us": target, "lamp_state": "unknown"}
            finally:
                self.backend.release()

    # close 关闭后端，无参数，程序退出时调用
    def close(self) -> None:
        self.stopping.set()
        with self.lock:
            self.backend.close()
