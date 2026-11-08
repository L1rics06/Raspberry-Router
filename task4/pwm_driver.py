"""Pi5 RP1硬件PWM驱动，固定BCM12物理32脚，通道0"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from .servo_specs import validate_pulse

PERIOD_NS = 20_000_000     # 50Hz对应20ms，sysfs单位为纳秒
LOCK_PATH = Path("/run/lock/maker-servo-gpio12.lock")
STOP_PATH = Path("/run/lock/maker-servo-gpio12.stop")


# find_chip 查找RP1 PWM0，root参数只供文件系统夹具测试
# 识别98000控制器，避免编号变化和误用9c000风扇控制器
def find_chip(root: Path = Path("/sys/class/pwm")) -> Path:
    found = []
    for chip in sorted(Path(root).glob("pwmchip*")):
        name = (chip / "device").resolve().name.lower()
        compatible = chip / "device/of_node/compatible"
        if re.fullmatch(r"[0-9a-f]*98000\.pwm", name) and compatible.is_file():
            if b"raspberrypi,rp1-pwm" in compatible.read_bytes():
                found.append(chip)
    if len(found) != 1:
        raise RuntimeError("未找到唯一RP1 PWM0，请先启用dtoverlay=pwm,pin=12,func=4并重启")
    return found[0]


# doctor 只读检查实际Pi5、引脚复用和PWM权限，无参数
# 不导出通道、不写寄存器、不输出舵机脉冲
def doctor() -> dict:
    errors = []
    model_file = Path("/proc/device-tree/model")
    model = model_file.read_text(errors="replace").replace("\0", "") if model_file.exists() else ""
    if platform.system() != "Linux" or not model.startswith("Raspberry Pi 5"):
        errors.append("真实驱动要求Raspberry Pi 5与Linux")
    mux = ""
    if shutil.which("pinctrl"):
        result = subprocess.run(["pinctrl", "get", "12"], capture_output=True, text=True, timeout=5)
        mux = result.stdout.strip()
        if result.returncode or "PWM0_CHAN0" not in mux or not re.search(r"\ba0\b", mux):
            errors.append("BCM12未复用为a0/PWM0_CHAN0，请检查overlay并重启")
    else:
        errors.append("缺少pinctrl，请安装raspi-utils")
    chip_path = ""
    try:
        chip = find_chip()
        chip_path = str(chip)
        channel = chip / "pwm0"
        for name in ("enable", "period", "duty_cycle"):
            if not (channel / name).exists() or not os.access(channel / name, os.R_OK | os.W_OK):
                errors.append(f"pwm0/{name}不存在或无读写权限，请运行PWM准备服务")
        if (channel / "period").exists() and (channel / "period").read_text().strip() != str(PERIOD_NS):
            errors.append("PWM周期不是20ms，请在设备空闲时重新运行PWM准备服务")
    except (OSError, RuntimeError) as error:
        errors.append(str(error))
    if not LOCK_PATH.exists() or not os.access(LOCK_PATH, os.R_OK | os.W_OK):
        errors.append("缺少舵机锁权限，请重新登录或使用PWM准备服务")
    if not STOP_PATH.is_file() or not os.access(STOP_PATH, os.R_OK | os.W_OK):
        errors.append("缺少停止标记权限，请运行PWM准备服务")
    elif STOP_PATH.read_text().strip() != "0":
        errors.append("紧急停止已锁定，请先停服务、检查机构，再执行reset-stop")
    return {"ok": not errors, "mode": "gpio", "model": model, "gpio_bcm": 12, "physical_pin": 32,
            "channel": 0, "frequency_hz": 50, "pwmchip": chip_path, "pinmux": mux,
            "errors": errors, "hardware_verified": False}


class HardwarePWM:
    """硬件PWM只负责脉宽输出，不推断真实位置或灯光状态"""
    mode = "gpio"

    # __init__ 建立真实后端，无参数，不自动发送任何脉冲
    # 全机文件锁防止MCP、命令行和重复Agent同时控制同一舵机
    def __init__(self):
        import fcntl
        state = doctor()
        if not state["ok"]:
            raise RuntimeError("；".join(state["errors"]))
        self.channel = Path(state["pwmchip"]) / "pwm0"
        self.lock = LOCK_PATH.open("r+")
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if (self.channel / "enable").read_text().strip() != "0":
                raise RuntimeError("PWM已在运行，拒绝接管其他控制者，请先停止已有实例")
        except BaseException:
            self.lock.close()
            raise
        self.enabled = False

    # pulse_us 持续输出指定脉宽，pulse为1000到2000之间的整数微秒
    # 第一帧先写占空时间再使能，后续由硬件持续产生50Hz脉冲
    def pulse_us(self, pulse: int) -> None:
        validate_pulse(pulse)
        if STOP_PATH.read_text().strip() != "0":
            raise RuntimeError("紧急停止已锁定")
        (self.channel / "duty_cycle").write_text(str(pulse * 1000))
        if not self.enabled:
            (self.channel / "enable").write_text("1")
            self.enabled = True

    # release 停止PWM，无参数，不表示机械一定卸力
    # 先产生零占空周期再禁用，断电或外部强杀时仍需现场处理
    def release(self) -> None:
        if self.enabled:
            try:
                (self.channel / "duty_cycle").write_text("0")
                time.sleep(0.025)
            finally:
                (self.channel / "enable").write_text("0")
                self.enabled = False

    # close 释放PWM和文件锁，无参数，正常退出与异常清理均调用
    def close(self) -> None:
        try:
            self.release()
        finally:
            self.lock.close()


# emergency_stop 锁定停止并关闭PWM，无参数，可在另一个终端调用
# 锁定标记阻止仍在运行的控制进程重新使能，不代表已切断舵机电源
def emergency_stop() -> dict:
    if platform.system() != "Linux":
        raise RuntimeError("真实PWM停止命令只能在Pi5 Linux上执行")
    STOP_PATH.write_text("1")
    channel = find_chip() / "pwm0"
    (channel / "duty_cycle").write_text("0")
    (channel / "enable").write_text("0")
    return {"ok": True, "mode": "gpio", "emergency_stop": True, "detail": "PWM已停止并锁定，卡住时关闭Pi电源后再拆SG90供电线"}


# reset_stop 清除停止锁定，无参数，要求控制进程已退出
# 只清标记，不输出任何脉冲
def reset_stop() -> dict:
    import fcntl
    with LOCK_PATH.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        channel = find_chip() / "pwm0"
        if (channel / "enable").read_text().strip() != "0":
            raise RuntimeError("PWM仍在使能，不能清除停止标记")
        STOP_PATH.write_text("0")
    return {"ok": True, "mode": "gpio", "emergency_stop": False, "detail": "仅清除停止标记，未执行动作"}


class SimPWM:
    """显式仿真后端，不访问sysfs，不伪装为gpio"""
    mode = "sim"

    # __init__ 初始化脉冲记录，无参数，仅用于软件验证
    def __init__(self):
        self.pulses = []
        self.released = True

    # pulse_us 记录脉宽，pulse为整数微秒，不产生电信号
    def pulse_us(self, pulse: int) -> None:
        validate_pulse(pulse)
        self.pulses.append(pulse)
        self.released = False

    # release 标记模拟PWM停止，无参数
    def release(self) -> None:
        self.released = True

    # close 结束仿真后端，无参数
    def close(self) -> None:
        self.release()
