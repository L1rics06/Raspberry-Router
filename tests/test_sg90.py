"""SG90改型的电压、型号、脉宽和旧标定迁移验证"""
import json
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from task4.common import ROOT
from task4 import servo_cli
from task4.servo_control import load_settings, save_settings
from task4.servo_specs import electrical_errors, settings_errors, validate_pulse


class SG90Tests(unittest.TestCase):
    """换舵机后不接受旧6V和270度标定，模板不自动认定实测通过"""

    # setUp 创建包内的隔离测试目录，无实体GPIO操作
    def setUp(self):
        runtime = ROOT / ".runtime"
        runtime.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=runtime, prefix="sg90-unit-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        for name in ("configs", "profiles", "handoff"):
            (self.folder / name).mkdir()
        for name in ("configs/servo.example.toml", "profiles/hardware.example.toml", "handoff/electrical.example.json", "handoff/calibration.example.toml"):
            shutil.copyfile(ROOT / name, self.folder / name)
        self.target = self.folder / "configs/servo.local.toml"

    # electrical 返回仅用于测试的完整SG90电气记录
    def electrical(self):
        data = json.loads((ROOT / "handoff/electrical.example.json").read_text(encoding="utf-8"))
        data.update(operator="unit fixture", tested_at="unit-test", measured_voltage_v=5.1)
        for key in ("approved", "common_ground_checked", "signal_compatible", "power_test_passed", "pin_conflict_checked"):
            data[key] = True
        return data

    # initialize 在隔离目录执行配置初始化，模拟非硬件主机避免碰真实锁
    def initialize(self):
        with patch.object(servo_cli, "ROOT", self.folder), patch.object(servo_cli.platform, "system", return_value="Windows"):
            return servo_cli.initialize(SimpleNamespace(config=self.target))

    # test_direct_header_record 接受SG90三线接法和Pi常见5.1V
    def test_direct_header_record(self):
        self.assertEqual(electrical_errors(self.electrical()), [])

    # test_old_six_volts_rejected 不接受之前6V或更高的设计与实测记录
    def test_old_six_volts_rejected(self):
        for key, value in (("measured_voltage_v", 6.0), ("power_design_voltage_v", 6.0), ("measured_voltage_v", 8.4)):
            with self.subTest(key=key, value=value):
                data = self.electrical()
                data[key] = value
                self.assertTrue(electrical_errors(data))

    # test_voltage_boundaries 拒绝低压、过高电压和布尔伪装
    def test_voltage_boundaries(self):
        for value in (4.7, 5.3, True, "5.0"):
            with self.subTest(value=value):
                data = self.electrical()
                data["measured_voltage_v"] = value
                self.assertTrue(electrical_errors(data))

    # test_bad_power_pin_rejected 电源必须接物理2脚，不能误填信号脚
    def test_bad_power_pin_rejected(self):
        data = self.electrical()
        data["physical_power_pin"] = 32
        self.assertTrue(electrical_errors(data))

    # test_external_fallback_no_parallel 独立5V备用方案不能并联Pi和电源正极
    def test_external_fallback_no_parallel(self):
        data = dict(self.electrical(), power_source="external_5v", physical_power_pin=None)
        self.assertEqual(electrical_errors(data), [])
        data["physical_power_pin"] = 2
        self.assertTrue(electrical_errors(data))

    # test_blank_record_rejected 不把模板当成实际供电验证
    def test_blank_record_rejected(self):
        data = json.loads((ROOT / "handoff/electrical.example.json").read_text(encoding="utf-8"))
        self.assertTrue(electrical_errors(data))

    # test_sg90_pulse_limits 允许保守1～2ms范围，拒绝旧舵机极值
    def test_sg90_pulse_limits(self):
        for value in (1000, 1500, 2000):
            validate_pulse(value)
        for value in (500, 999, 2001, 2500, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_pulse(value)

    # test_old_model_rejected 改型后不能只改供电线却沿用旧配置
    def test_old_model_rejected(self):
        cfg = tomllib.loads((ROOT / "configs/servo.example.toml").read_text(encoding="utf-8"))
        cfg["servo_model"] = "PWM_20kg_5-8.4V"
        self.assertTrue(settings_errors(cfg))

    # test_initialization_creates_unconfirmed 首次生成正确型号但未确认的配置
    def test_initialization_creates_unconfirmed(self):
        result = self.initialize()
        cfg = load_settings(self.target, "gpio")
        self.assertTrue(result["requires_recalibration"])
        self.assertEqual(cfg["servo_model"], "SG90")
        self.assertEqual(cfg["power_nominal_v"], 5.0)
        self.assertFalse(cfg["calibrated"])
        self.assertEqual((cfg["on_us"], cfg["off_us"]), (0, 0))

    # test_old_configuration_backed_up 旧型号的配置与两份记录先备份再清除确认
    def test_old_configuration_backed_up(self):
        old = {"servo_model": "PWM_20kg_5-8.4V", "calibrated": True, "power_nominal_v": 6.0,
               "home_us": 1500, "on_us": 800, "off_us": 2200}
        save_settings(self.target, old)
        save_settings(self.folder / "handoff/calibration.local.toml", old)
        (self.folder / "handoff/electrical.local.json").write_text(json.dumps({"approved": True, "servo_model": "old", "power_design_voltage_v": 6.0}), encoding="utf-8")
        result = self.initialize()
        self.assertEqual(len(result["backups"]), 3)
        self.assertTrue(all(Path(path).exists() for path in result["backups"]))
        self.assertTrue(result["requires_recalibration"])
        self.assertFalse(load_settings(self.target, "gpio")["calibrated"])
        electric = json.loads((self.folder / "handoff/electrical.local.json").read_text(encoding="utf-8"))
        self.assertFalse(electric["approved"])
        self.assertEqual(electric["physical_power_pin"], 2)

    # test_sg90_calibration_preserved 重复初始化不覆盖已经完成的SG90标定
    def test_sg90_calibration_preserved(self):
        self.initialize()
        cfg = load_settings(self.target, "gpio")
        cfg.update(calibrated=True, on_us=1450, off_us=1550, clearance_verified=True,
                   operator="unit fixture", tested_at="unit-test", switch_type="single_rocker")
        save_settings(self.target, cfg)
        save_settings(self.folder / "handoff/calibration.local.toml", cfg)
        before = self.target.read_bytes()
        result = self.initialize()
        self.assertFalse(result["requires_recalibration"])
        self.assertEqual(result["backups"], [])
        self.assertEqual(self.target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
