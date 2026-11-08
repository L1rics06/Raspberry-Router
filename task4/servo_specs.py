"""SG90当前方案的固定配置和现场记录校验"""
from __future__ import annotations

SERVO_MODEL = "SG90"
SERVO_PROFILE = "sg90_5v_v1"
PULSE_MIN_US = 1000     # 本包采用保守的1ms起点，不声称对应实际角度端点
PULSE_MAX_US = 2000     # 本包不开放旧舵机的500～2500us全范围
VOLTAGE_MIN_V = 4.8
VOLTAGE_MAX_V = 5.25     # 本项目5V电源验收上限，兼容Pi常见5.1V输入


# validate_pulse 校验SG90脉宽，pulse为整数微秒
# 限制本包的软件工作范围，不代表所有SG90的机械端点相同
def validate_pulse(pulse: int) -> None:
    if type(pulse) is not int or not PULSE_MIN_US <= pulse <= PULSE_MAX_US:
        raise ValueError("SG90本包只允许1000～2000微秒，请从1500附近小步校准")


# settings_errors 检查型号、电压标记与范围，data为舵机TOML对象
# 拒绝沿用旧大扭矩舵机配置，换型号后必须重新标定
def settings_errors(data: dict) -> list[str]:
    errors = []
    if data.get("servo_model") != SERVO_MODEL or data.get("servo_profile") != SERVO_PROFILE:
        errors.append("当前已改用SG90，请先运行servo init备份旧参数并重新标定")
    voltage = data.get("power_nominal_v")
    if type(voltage) not in (int, float) or voltage != 5:
        errors.append("当前SG90方案固定5V供电，旧供电配置不能继续使用")
    for key in ("min_us", "max_us", "home_us"):
        try:
            validate_pulse(data.get(key))
        except ValueError:
            errors.append(f"{key}必须是1000～2000范围内的整数")
    return errors


# electrical_errors 核对操作员填写的供电记录，data为JSON对象
# 仅检查记录完整性，不自动测量电压、确认扭矩或保证Pi供电余量
def electrical_errors(data: dict) -> list[str]:
    errors = []
    if data.get("servo_model") != SERVO_MODEL or data.get("servo_profile") != SERVO_PROFILE:
        errors.append("电气记录不是当前SG90方案，请重新填写")
    for key in ("approved", "common_ground_checked", "signal_compatible", "power_test_passed", "pin_conflict_checked"):
        if data.get(key) is not True:
            errors.append(key + "尚未实测确认")
    if not str(data.get("operator", "")).strip() or not str(data.get("tested_at", "")).strip():
        errors.append("缺少操作员或测试时间")
    voltage = data.get("measured_voltage_v")
    if type(voltage) not in (int, float) or not VOLTAGE_MIN_V <= voltage <= VOLTAGE_MAX_V:
        errors.append("SG90本项目要求舵机端实测4.8～5.25V，不接受旧6V记录")
    nominal = data.get("power_design_voltage_v")
    if type(nominal) not in (int, float) or nominal != 5:
        errors.append("设计供电必须为5V")
    if data.get("servo_gpio_bcm") != 12 or data.get("physical_signal_pin") != 32:
        errors.append("信号应为BCM12/物理32脚")
    source = data.get("power_source")
    if source == "pi5_5v_header":
        if data.get("physical_power_pin") != 2 or data.get("physical_ground_pin") != 6:
            errors.append("树莓派取电应记录物理2脚5V与物理6脚GND")
    elif source == "external_5v":
        if data.get("physical_power_pin") is not None:
            errors.append("独立5V供电时红线不再接Pi的5V脚，不能并联两个正极")
    else:
        errors.append("power_source应明确为pi5_5v_header或external_5v")
    return errors
