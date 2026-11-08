"""验证错误传播、真机边界和凭证处理，不驱动GPIO"""
from __future__ import annotations

import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from task4.common import ROOT, check, finish, inspect_gateway_result, new_report, parse_tool_payload, redact, write_json
from task4.deploy import make_agent_config, make_service, toml_string, unit_quote
from task4.evidence import collect_evidence
from task4.mcp_checks import unpack_result, validate_status
from task4.profile import gateway_settings, load_profile, preflight, validate_calibration


class BoundaryTests(unittest.TestCase):
    """重点检查不能被错误标为完成的情况"""

    # setUp 创建包目录内的隔离测试空间，无外部设备或真实令牌
    def setUp(self):
        runtime = ROOT / ".runtime"
        runtime.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=runtime, prefix="unit-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.profile = load_profile(ROOT / "profiles/simulation.toml")

    # calibration 返回纯测试数据，不能用于真实舵机
    def calibration(self):
        return {"calibrated": True, "operator": "unit-fixture", "tested_at": "unit-test",
                "min_us": 1100, "max_us": 1900, "home_us": 1500, "on_us": 1300,
                "off_us": 1700, "wait_s": 0.4, "clearance_verified": True}

    # test_inner_failure 检查外层成功不能掩盖设备失败
    def test_inner_failure(self):
        ok, inner, reason = inspect_gateway_result({"ok": True, "text": '{"ok":false,"error":"motor"}'})
        self.assertFalse(ok)
        self.assertFalse(inner["ok"])
        self.assertIn("外层", reason)

    # test_outer_failure 检查外层失败不能被内层成功覆盖
    def test_outer_failure(self):
        self.assertFalse(inspect_gateway_result({"ok": False, "text": '{"ok":true}'})[0])

    # test_strict_ok 拒绝数字和字符串形式的ok
    def test_strict_ok(self):
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_tool_payload({"ok": value})

    # test_invalid_json 拒绝没有JSON结构的工具回复
    def test_invalid_json(self):
        self.assertFalse(inspect_gateway_result({"ok": True, "text": "done"})[0])

    # test_mcp_error 检查协议isError优先于文本中的ok
    def test_mcp_error(self):
        result = SimpleNamespace(isError=True, content=[SimpleNamespace(text='{"ok":true}')])
        with self.assertRaises(ValueError):
            unpack_result(result)

    # test_redaction 检查常用凭证和嵌套字段的脱敏
    def test_redaction(self):
        value = {"token": "custom-secret", "nested": ["pat-abcdefghijklmnop", "dev-abcdefghijklmnop", "sk-abcdefghijklmnop"],
                 "text": "Bearer test-secret", "jwt_sample": "eyJabc.defgh.ijklmn"}
        result = json.dumps(redact(value))
        for secret in ("custom-secret", "pat-abc", "dev-abc", "sk-abc", "test-secret", "eyJabc"):
            self.assertNotIn(secret, result)

    # test_saved_report_redacted 检查落盘报告不会保留令牌
    def test_saved_report_redacted(self):
        target = write_json(self.folder / "report.json", {"device_token": "anything", "text": "pat-abcdefghijklmnop"})
        self.assertNotIn("anything", target.read_text())
        self.assertNotIn("pat-", target.read_text())

    # test_sim_never_hardware 检查模拟通过没有真机完成标记
    def test_sim_never_hardware(self):
        report = new_report("test", "sim")
        check(report, "unit", True)
        finish(report)
        self.assertEqual(report["status"], "SIMULATION_PASS")
        self.assertIs(report["hardware_verified"], False)

    # test_gpio_needs_evidence 检查GPIO软件通过仍等待实物证据
    def test_gpio_needs_evidence(self):
        report = finish(new_report("test", "gpio"))
        self.assertEqual(report["status"], "HARDWARE_EVIDENCE_PENDING")
        self.assertIs(report["hardware_verified"], False)

    # test_mode_mismatch 阻止sim状态进入gpio验收
    def test_mode_mismatch(self):
        report = new_report("test", "gpio")
        self.assertFalse(validate_status(report, {"ok": True, "mode": "sim"}, wall=False))

    # test_fixture_cannot_be_gpio 阻止明确夹具伪装真机
    def test_fixture_cannot_be_gpio(self):
        self.assertFalse(validate_status(new_report("test", "gpio"), {"ok": True, "mode": "gpio", "fixture": True}, wall=False))

    # test_unready_blocks 阻止未标定和未就绪状态
    def test_unready_blocks(self):
        report = new_report("test")
        data = {"ok": True, "mode": "sim", "ready": False, "calibrated": False, "lamp_state": "unknown"}
        self.assertFalse(validate_status(report, data, wall=True))
        self.assertEqual(finish(report)["status"], "BLOCKED")

    # test_false_lamp_state 拒绝无传感器却宣称灯已打开
    def test_false_lamp_state(self):
        data = {"ok": True, "mode": "sim", "ready": True, "calibrated": True, "lamp_state": "on"}
        self.assertFalse(validate_status(new_report("test"), data, wall=True))

    # test_error_status 拒绝设备错误状态
    def test_error_status(self):
        self.assertFalse(validate_status(new_report("test"), {"ok": True, "mode": "sim", "errors": {"power": "low"}}, wall=False))

    # test_valid_calibration 检查完整的测试标定能被解析
    def test_valid_calibration(self):
        self.assertEqual(validate_calibration(self.calibration()), [])

    # test_uncalibrated_rejected 拒绝未完成的标定记录
    def test_uncalibrated_rejected(self):
        data = self.calibration()
        data["calibrated"] = False
        self.assertTrue(validate_calibration(data))

    # test_bool_not_pulse 拒绝把布尔值当作整数脉宽
    def test_bool_not_pulse(self):
        data = self.calibration()
        data["on_us"] = True
        self.assertTrue(validate_calibration(data))

    # test_pulse_limits 拒绝越界、相同位置及过长等待
    def test_pulse_limits(self):
        for key, value in (("on_us", 2600), ("off_us", 1500), ("wait_s", 99), ("clearance_verified", False)):
            with self.subTest(key=key):
                data = self.calibration()
                data[key] = value
                self.assertTrue(validate_calibration(data))

    # test_example_blocked 检查空白真机模板不会被误判完成
    def test_example_blocked(self):
        report = preflight(load_profile(ROOT / "profiles/hardware.example.toml"))
        self.assertEqual(finish(report)["status"], "BLOCKED")

    # test_explicit_target_required 拒绝空设备ID或URL路径注入
    def test_explicit_target_required(self):
        for device_id in ("", "../other", "a/b", "CHANGE_ME"):
            with self.subTest(device_id=device_id), patch.dict(os.environ, {"TASK4_ACCESS_TOKEN": "pat-localtesttoken"}):
                self.profile["gateway"]["device_id"] = device_id
                with self.assertRaises(ValueError):
                    gateway_settings(self.profile)

    # test_url_credential_rejected 拒绝把凭证直接写入URL
    def test_url_credential_rejected(self):
        self.profile["gateway"]["base_url"] = "https://user:password@example.com"
        with self.assertRaises(ValueError):
            gateway_settings(self.profile)

    # test_generated_config 检查含中文空格路径的配置可被TOML解析
    def test_generated_config(self):
        self.profile["mcp"]["args"] = [str(self.folder / "中文 空格.py")]
        data = tomllib.loads(make_agent_config(self.profile, "dev-unittestfake"))
        self.assertEqual(data["mcp"]["servers"][0]["args"], self.profile["mcp"]["args"])
        self.assertEqual(data["allow_tools"], ["light.set", "devices.status"])
        self.assertEqual(data["llm_mode"], "proxy")

    # test_no_config_cli 检查服务确实通过环境变量传配置
    def test_no_config_cli(self):
        service = make_service(self.folder / "profile.toml", self.folder / "private.toml", user="pi", python=Path("/usr/bin/python3"), repo=Path("/opt/repo"))
        self.assertIn("PIAGENT_CONFIG=", service)
        self.assertNotIn("--config", service)
        self.assertIn("PYTHONPATH=/opt/repo/agent:", service.replace("\\\\", "/").replace("\\", "/"))
        self.assertIn("KillMode=control-group", service)
        self.assertEqual(unit_quote("/opt/$task%"), '"/opt/$task%%"')
        self.assertEqual(unit_quote("/opt/$task%", exec_arg=True), '"/opt/$$task%%"')

    # test_control_character_rejected 拒绝把换行插入服务或配置
    def test_control_character_rejected(self):
        with self.assertRaises(ValueError):
            toml_string("a\nb")

    # test_bad_service_user 拒绝额外服务指令混入用户名
    def test_bad_service_user(self):
        with self.assertRaises(ValueError):
            make_service(Path("/p"), Path("/c"), user="pi\nExecStart=bad", python=Path("/python"), repo=Path("/repo"))

    # test_simulation_evidence_rejected 不允许拿模拟报告提交真机验收
    def test_simulation_evidence_rejected(self):
        technical = write_json(self.folder / "technical.json", {"kind": "gateway", "expected_mode": "sim", "status": "SIMULATION_PASS"})
        evidence = write_json(self.folder / "human.json", {"operator": "unit", "tested_at": "test"})
        report = collect_evidence(technical, evidence)
        self.assertFalse(report["operator_evidence_complete"])
        self.assertIs(report["hardware_verified"], False)

    # test_missing_video_rejected 即使填写观察结果也必须附操作视频
    def test_missing_video_rejected(self):
        technical = write_json(self.folder / "technical.json", {"kind": "gateway", "expected_mode": "gpio", "status": "HARDWARE_EVIDENCE_PENDING",
            "action_checks_completed": True, "cycles": 10, "chat_trace_only": True, "chat_direction_checked": True,
            "chat_device_state_checked": True, "device_id": "unit", "checks": [{"status": "PASS"}]})
        observations = {key: True for key in ("on_verified", "off_verified", "returned_home", "no_reset", "manual_switch_access", "restart_safe", "offline_error_clear")}
        evidence = write_json(self.folder / "human.json", {"operator": "unit", "tested_at": "test", "device_id": "unit", "cycles_observed": 10,
            "observations": observations, "attachments": []})
        report = collect_evidence(technical, evidence)
        self.assertFalse(report["operator_evidence_complete"])
        self.assertEqual(report["checks"][0]["status"], "PASS")
        self.assertEqual(report["checks"][-1]["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
