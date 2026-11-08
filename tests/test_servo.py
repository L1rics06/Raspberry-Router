"""真实控制算法与硬件文件接口夹具，不使用实体GPIO"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from task4.common import ROOT
from task4 import pwm_driver
from task4.pwm_driver import HardwarePWM, SimPWM, find_chip
from task4.servo_control import ServoControl, calibration_errors, load_settings, save_settings
from task4.layout import agent_repo

sys.path.insert(0, str(agent_repo() / "agent"))
from pi_agent.agent_loop import run_agent_loop, run_tool_direct
from pi_agent.mcp_client import MCPCallError, MCPPool


class ServoTests(unittest.TestCase):
    """验证限幅、顺序、失败锁定及纳秒换算"""

    # setUp 创建包内隔离文件夹，加载明确的模拟标定
    def setUp(self):
        (ROOT / ".runtime").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".runtime", prefix="servo-unit-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.cfg = load_settings(ROOT / "configs/servo-simulation.toml", "sim")
        self.backend = SimPWM()
        self.control = ServoControl(self.cfg, self.backend)

    # test_start_is_inactive 仅构造和查询时不发送任何脉冲
    def test_start_is_inactive(self):
        self.assertTrue(self.control.status()["ready"])
        self.assertEqual(self.backend.pulses, [])
        self.assertEqual(self.control.command_count, 0)

    # test_on_sequence 检查开灯按压后退回并停止PWM
    def test_on_sequence(self):
        result = self.control.set_light(True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["trace"], ["home", "press_on", "home"])
        self.assertEqual(self.backend.pulses[-1], self.cfg["home_us"])
        self.assertTrue(self.backend.released)
        self.assertEqual(result["lamp_state"], "unknown")

    # test_off_sequence 检查关灯使用独立位置，不以toggle猜测灯状态
    def test_off_sequence(self):
        result = self.control.set_light(False)
        self.assertTrue(result["ok"])
        self.assertIn(self.cfg["off_us"], self.backend.pulses)
        self.assertEqual(result["trace"], ["home", "press_off", "home"])

    # test_slew_and_limits 检查步进限幅，没有越过设定窗口
    def test_slew_and_limits(self):
        self.control.set_light(True)
        self.assertTrue(all(self.cfg["min_us"] <= value <= self.cfg["max_us"] for value in self.backend.pulses))
        self.assertTrue(all(abs(b - a) <= self.cfg["step_us"] for a, b in zip(self.backend.pulses, self.backend.pulses[1:])))

    # test_invalid_target_no_pulse 越界或非整数输入不产生脉冲
    def test_invalid_target_no_pulse(self):
        for value in (1399, 1601, True, 1500.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.control.move_us(value)
        self.assertEqual(self.backend.pulses, [])

    # test_unconfirmed_no_action 未标定时拒绝实际动作流程
    def test_unconfirmed_no_action(self):
        self.control.settings["calibrated"] = False
        self.assertFalse(self.control.set_light(True)["ok"])
        self.assertEqual(self.backend.pulses, [])

    # test_non_boolean_no_action 拒绝数字和字符串开关目标
    def test_non_boolean_no_action(self):
        for value in (1, "true", None):
            self.assertFalse(self.control.set_light(value)["ok"])
        self.assertEqual(self.backend.pulses, [])

    # test_fault_is_latched 写PWM失败后停止并拒绝后续动作
    def test_fault_is_latched(self):
        original = self.backend.pulse_us
        calls = []
        def fail_once(value):
            calls.append(value)
            if len(calls) == 3:
                raise OSError("fixture write failure")
            original(value)
        self.backend.pulse_us = fail_once
        self.assertFalse(self.control.set_light(True)["ok"])
        self.assertTrue(self.backend.released)
        count = len(calls)
        self.assertFalse(self.control.set_light(False)["ok"])
        self.assertEqual(len(calls), count)
        self.assertFalse(self.control.status()["ready"])

    # test_close_prevents_future_motion 关闭后不能继续输出
    def test_close_prevents_future_motion(self):
        self.control.close()
        self.assertFalse(self.control.status()["ready"])
        self.assertFalse(self.control.set_light(True)["ok"])
        self.assertEqual(self.backend.pulses, [])

    # test_sim_config_rejected_for_gpio 防止直接拿模拟标定上真机
    def test_sim_config_rejected_for_gpio(self):
        with self.assertRaises(ValueError):
            load_settings(ROOT / "configs/servo-simulation.toml", "gpio")

    # test_wrong_pwm_pin_rejected 错误GPIO编号不能悄悄改变实际输出脚
    def test_wrong_pwm_pin_rejected(self):
        for key, value in (("gpio_bcm", 17), ("physical_pin", 12), ("pwm_channel", 2), ("frequency_hz", 100)):
            with self.subTest(key=key):
                data = dict(self.cfg)
                data[key] = value
                path = self.folder / "wrong.toml"
                save_settings(path, data)
                with self.assertRaises(ValueError):
                    load_settings(path, "sim")

    # test_motion_budget_rejected 不允许超出工具时限的宽范围慢动作
    def test_motion_budget_rejected(self):
        data = dict(self.cfg, min_us=1000, max_us=2000)
        path = self.folder / "slow.toml"
        save_settings(path, data)
        with self.assertRaises(ValueError):
            load_settings(path, "sim")

    # test_dual_control_rejected 未确认单控时拒绝固定开关位置方案
    def test_dual_control_rejected(self):
        self.assertTrue(calibration_errors(dict(self.cfg, switch_type="dual_control")))

    # test_roundtrip 保存后读回字段保持一致
    def test_roundtrip(self):
        path = self.folder / "roundtrip.toml"
        save_settings(path, self.cfg)
        self.assertEqual(load_settings(path, "sim"), self.cfg)

    # make_channel 创建sysfs文件夹具，无硬件效应
    def make_channel(self):
        channel = self.folder / "pwm0"
        channel.mkdir()
        for name, value in (("enable", "0"), ("period", "20000000"), ("duty_cycle", "0")):
            (channel / name).write_text(value)
        stop = self.folder / "stop"
        stop.write_text("0")
        backend = HardwarePWM.__new__(HardwarePWM)
        backend.channel = channel
        backend.enabled = False
        return channel, stop, backend

    # test_sysfs_nanoseconds 写1500us必须得到1500000ns，周期仍为20ms
    def test_sysfs_nanoseconds(self):
        channel, stop, backend = self.make_channel()
        with patch.object(pwm_driver, "STOP_PATH", stop), patch.object(pwm_driver.time, "sleep"):
            backend.pulse_us(1500)
            self.assertEqual((channel / "duty_cycle").read_text(), "1500000")
            self.assertEqual((channel / "period").read_text(), "20000000")
            self.assertEqual((channel / "enable").read_text(), "1")
            backend.release()
            self.assertEqual((channel / "enable").read_text(), "0")
            self.assertEqual((channel / "duty_cycle").read_text(), "0")

    # test_emergency_flag_prevents_reenable 另一个终端停止后不能被当前进程重新启动
    def test_emergency_flag_prevents_reenable(self):
        channel, stop, backend = self.make_channel()
        stop.write_text("1")
        with patch.object(pwm_driver, "STOP_PATH", stop), self.assertRaises(RuntimeError):
            backend.pulse_us(1500)
        self.assertEqual((channel / "enable").read_text(), "0")

    # test_find_chip_ignores_number_and_fan 查找基于控制器地址，排除风扇通道
    def test_find_chip_ignores_number_and_fan(self):
        for name in ("pwmchip7", "pwmchip0"):
            target = self.folder / name / "device/of_node"
            target.mkdir(parents=True)
            (target / "compatible").write_bytes(b"raspberrypi,rp1-pwm\0")
        def resolved(path, *args, **kwargs):
            return Path("1f00098000.pwm" if path.parent.name == "pwmchip7" else "1f0009c000.pwm")
        with patch.object(Path, "resolve", autospec=True, side_effect=resolved):
            self.assertEqual(find_chip(self.folder).name, "pwmchip7")

    # test_find_chip_missing 拒绝空sysfs，不降级为模拟
    def test_find_chip_missing(self):
        with self.assertRaises(RuntimeError):
            find_chip(self.folder)


class AgentFailureTests(unittest.IsolatedAsyncioTestCase):
    """确保生产Agent不会把硬件或协议错误变成成功"""

    # pool 创建只有一个工具的真实MCPPool，session用协议结果夹具代替
    def pool(self, payload, is_error=False):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        response = SimpleNamespace(isError=is_error, content=[SimpleNamespace(text=text)])
        session = SimpleNamespace(call_tool=AsyncMock(return_value=response))
        pool = MCPPool([])
        pool._owner = {"light.set": "fixture"}
        pool._sessions = {"fixture": session}
        pool._tools = {"light.set": {"description": "fixture", "inputSchema": {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]}}}
        self.cfg = SimpleNamespace(allow_tools=["light.set"], confirm_tools=[], max_steps=3)
        return pool, session

    # test_device_error_propagates 设备失败必须被MCP池抛出并保留JSON
    async def test_device_error_propagates(self):
        pool, _ = self.pool({"ok": False, "error": "POWER_FAILURE", "mode": "gpio"})
        with self.assertRaises(MCPCallError) as caught:
            await pool.call("light.set", {"on": True})
        self.assertEqual(caught.exception.payload["error"], "POWER_FAILURE")

    # test_protocol_error_wins 即使文本声称成功，协议isError仍判失败
    async def test_protocol_error_wins(self):
        pool, _ = self.pool({"ok": True}, is_error=True)
        with self.assertRaises(MCPCallError):
            await pool.call("light.set", {"on": True})

    # test_malformed_results 非JSON、非布尔ok、缺ok均不能通过
    async def test_malformed_results(self):
        for payload in ("done", {"ok": "true"}, {"on": True}, []):
            with self.subTest(payload=payload):
                pool, _ = self.pool(payload)
                with self.assertRaises(MCPCallError):
                    await pool.call("light.set", {"on": True})

    # test_direct_failure_outer_false 直控失败内外层均为false
    async def test_direct_failure_outer_false(self):
        pool, _ = self.pool({"ok": False, "error": "JAMMED"})
        result = await run_tool_direct("light.set", {"on": True}, pool, self.cfg, False)
        self.assertFalse(result["ok"])
        self.assertFalse(json.loads(result["text"])["ok"])

    # test_invalid_arguments_do_not_execute 字符串布尔不触发实际工具调用
    async def test_invalid_arguments_do_not_execute(self):
        pool, session = self.pool({"ok": True})
        result = await run_tool_direct("light.set", {"on": "true"}, pool, self.cfg, False)
        self.assertFalse(result["ok"])
        session.call_tool.assert_not_awaited()

    # test_chat_failure_stops_before_model_claim 故障后不再询问模型，更不自动补发动作
    async def test_chat_failure_stops_before_model_claim(self):
        pool, _ = self.pool({"ok": False, "error": "JAMMED"})
        call = {"id": "fixture", "type": "function", "function": {"name": "light.set", "arguments": '{"on":true}'}}
        llm = SimpleNamespace(chat=AsyncMock(side_effect=[{"tool_calls": [call]}, {"content": "假装成功"}]))
        result = await run_agent_loop("开灯", pool, llm, self.cfg)
        self.assertFalse(result["ok"])
        self.assertEqual(llm.chat.await_count, 1)

    # test_success_preserved 正常JSON工具结果仍可被直控使用
    async def test_success_preserved(self):
        pool, session = self.pool({"ok": True, "mode": "sim", "requested_on": True})
        result = await run_tool_direct("light.set", {"on": True}, pool, self.cfg, False)
        self.assertTrue(result["ok"])
        self.assertEqual(session.call_tool.await_count, 1)


if __name__ == "__main__":
    unittest.main()
