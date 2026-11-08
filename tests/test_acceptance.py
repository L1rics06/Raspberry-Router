"""覆盖聊天方向误判、虚假工具轨迹及合并状态错误丢失的回归测试"""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from devices.wall_light_mcp import merge_status
from task4.common import finish
from task4.gateway_checks import run_gateway, validate_chat_effect, validate_chat_trace


class ChatTraceTests(unittest.TestCase):
    """工具名、参数类型、方向及动作次数必须全部符合约定"""

    # valid 生成指定轨迹的校验结果，actions为Agent实际返回的字符串数组
    def valid(self, actions, on=True):
        return validate_chat_trace({"ok": True, "actions": actions}, "light.set", "devices.status", on)[0]

    # test_correct_directions 开和关使用独立布尔值，状态查询可以穿插
    def test_correct_directions(self):
        self.assertTrue(self.valid(['devices.status({})', 'light.set({"on":true})']))
        self.assertTrue(self.valid(['light.set({"on":false})', 'devices.status({})'], False))

    # test_wrong_direction 原先两次关灯也通过的情形必须被拒绝
    def test_wrong_direction(self):
        self.assertFalse(self.valid(['light.set({"on":false})']))
        self.assertFalse(self.valid(['light.set({"on":true})'], False))

    # test_tool_name_exact 不能把包含工具名称的其他字符串认作真实调用
    def test_tool_name_exact(self):
        for name in ("fake.light.set", "light.set.extra", "I called light.set", "servo.set"):
            with self.subTest(name=name):
                self.assertFalse(self.valid([name + '({"on":true})']))

    # test_no_boolean_coercion 字符串和数字不能当作布尔值
    def test_no_boolean_coercion(self):
        for value in ('"true"', '1', '0', 'null', '[]'):
            with self.subTest(value=value):
                self.assertFalse(self.valid(['light.set({"on":' + value + '})']))

    # test_extra_and_duplicate_keys 防止重复字段或额外位置参数混入轨迹
    def test_extra_and_duplicate_keys(self):
        for args in ('{"on":true,"angle":90}', '{"on":false,"on":true}', '{}', '[]'):
            with self.subTest(args=args):
                self.assertFalse(self.valid(['light.set(' + args + ')']))

    # test_malformed_actions 无动作或畸形动作不能通过
    def test_malformed_actions(self):
        for actions in ([], None, "light.set", [1], ["light.set(true"], ['light.set({on:true})']):
            with self.subTest(actions=actions):
                self.assertFalse(self.valid(actions))

    # test_one_press_only 重复按压及先开后关都不是一次正确聊天动作
    def test_one_press_only(self):
        self.assertFalse(self.valid(['light.set({"on":true})'] * 2))
        self.assertFalse(self.valid(['light.set({"on":true})', 'light.set({"on":false})']))
        self.assertFalse(self.valid(['devices.status({})']))

    # test_unexpected_side_effect 不允许其他设备动作或带参数状态查询
    def test_unexpected_side_effect(self):
        for action in ('relay.set({"on":true})', 'devices.status({"on":true})'):
            with self.subTest(action=action):
                self.assertFalse(self.valid(['light.set({"on":true})', action]))

    # test_failed_result 即使含正确轨迹，外层失败也不通过
    def test_failed_result(self):
        self.assertFalse(validate_chat_trace({"ok": False, "actions": ['light.set({"on":true})']}, "light.set", "devices.status", True)[0])

    # test_effect_counter_and_state 核对真实执行计数和状态，不依赖聊天文字
    def test_effect_counter_and_state(self):
        before = {"command_count": 4}
        after = {"ok": True, "mode": "sim", "ready": True, "calibrated": True, "errors": {},
                 "lamp_state": "unknown", "requested_on": True, "command_count": 5}
        self.assertTrue(validate_chat_effect(before, after, True, "sim"))
        for change in ({"command_count": 4}, {"command_count": 6}, {"command_count": True}, {"requested_on": False},
                       {"requested_on": 1}, {"mode": "gpio"}, {"ok": False}, {"ready": False},
                       {"errors": {"fault": "jam"}}, {"lamp_state": "on"}, {"calibrated": False}):
            with self.subTest(change=change):
                self.assertFalse(validate_chat_effect(before, dict(after, **change), True, "sim"))


class GatewayRegressionTests(unittest.IsolatedAsyncioTestCase):
    """通过完整run_gateway路径复现方向错误和只有轨迹没有动作"""

    # exercise 驱动真实验收函数，direction错误或advance关闭时模拟故障Agent
    # HTTP与聊天只使用内存夹具，绝不连接真实网关或舵机
    async def exercise(self, *, direction=True, advance=True, wrong_state=False, actuate=True):
        profile = {"expected_mode": "sim", "gateway": {}, "contract": {"action_tool": "light.set", "status_tool": "devices.status"}}
        state = {"ok": True, "mode": "sim", "ready": True, "calibrated": True, "errors": {},
                 "lamp_state": "unknown", "command_count": 0, "requested_on": None}

        # response 将夹具包装为httpx响应所需的最小接口，data为响应JSON
        def response(data):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

        # get 返回指定设备或历史记录，url决定只读资源
        async def get(url, **_kwargs):
            data = [{"device_id": "unit", "online": True, "caps": ["light.set", "devices.status"]}] if url.endswith("/api/devices") else [{"device_id": "unit"}]
            return response(data)

        # manual 返回设备状态或执行直控，args中的on必须为布尔值
        async def manual(_client, _base, _jwt, _device, tool, args):
            if tool == "light.set":
                state["command_count"] += 1
                state["requested_on"] = args["on"]
            return {"ok": True, "text": json.dumps(state)}

        # chat 模拟Agent工具轨迹及独立设备变化，text决定预期方向
        async def chat(_base, _jwt, _device, text):
            on = text.startswith("打开") if direction else False
            if advance:
                state["command_count"] += 1
                state["requested_on"] = not on if wrong_state else on
            return {"ok": True, "actions": ["light.set(" + json.dumps({"on": on}) + ")"]}

        client = SimpleNamespace(get=get)
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch("httpx.AsyncClient", return_value=context), patch("task4.gateway_checks.gateway_settings", return_value=("http://fixture", "pat-fixture", "unit")), \
             patch("task4.gateway_checks.login", AsyncMock(return_value="fixture-jwt")), \
             patch("task4.gateway_checks.manual", side_effect=manual), patch("task4.gateway_checks.chat_once", side_effect=chat) as chats:
            result = finish(await run_gateway(profile, actuate=actuate, chat=actuate))
            return result, chats.await_count

    # test_chat_success 真正一次开和一次关均执行才通过
    async def test_chat_success(self):
        report, count = await self.exercise()
        self.assertEqual(report["status"], "SIMULATION_PASS")
        self.assertTrue(report["chat_direction_checked"])
        self.assertTrue(report["chat_device_state_checked"])
        self.assertEqual(count, 2)

    # test_wrong_chat_direction 两条聊天均关灯时第一条即失败，不补发动作
    async def test_wrong_chat_direction(self):
        report, count = await self.exercise(direction=False)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(count, 1)

    # test_trace_without_execution 只有正确轨迹却没有增加执行计数时失败
    async def test_trace_without_execution(self):
        report, count = await self.exercise(advance=False)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(count, 1)

    # test_wrong_status_after_chat 返回轨迹正确但设备目标相反时失败
    async def test_wrong_status_after_chat(self):
        report, _ = await self.exercise(wrong_state=True)
        self.assertEqual(report["status"], "FAIL")

    # test_default_read_only 只读验收不会调用聊天或产生墙灯动作
    async def test_default_read_only(self):
        report, count = await self.exercise(actuate=False)
        self.assertEqual(report["status"], "SIMULATION_PASS")
        self.assertEqual(count, 0)
        self.assertNotIn("action_checks_completed", report)


class CombinedStatusTests(unittest.TestCase):
    """合并入口保留上游五个工具需要的顶层状态及错误字段"""

    # setUp 准备独立的舵机与原有设备状态，所有数据都是模拟值
    def setUp(self):
        self.wall = {"ok": True, "ready": True, "mode": "sim", "errors": {}, "requested_on": None}
        self.base = {"ok": True, "mode": "sim", "errors": {}, "led": {"on": False},
                     "fan": {"on": False}, "relay": {"on": False}, "pins": {"led": 17}}

    # test_healthy 保留原有字段并清晰区分舵机就绪与其他设备就绪
    def test_healthy(self):
        merged = merge_status(self.wall, self.base)
        self.assertTrue(merged["ready"])
        self.assertTrue(merged["other_devices_ready"])
        self.assertEqual(merged["errors"], {})
        for name in ("led", "fan", "relay", "pins"):
            self.assertEqual(merged[name], self.base[name])

    # test_legacy_error_visible 原有设备错误必须继续在顶层可见
    def test_legacy_error_visible(self):
        self.base["errors"] = {"led": "fixture-failure"}
        merged = merge_status(self.wall, self.base)
        self.assertEqual(merged["errors"]["led"], "fixture-failure")
        self.assertFalse(merged["other_devices_ready"])
        self.assertTrue(merged["ready"])

    # test_servo_fault_preserved 原有设备正常时舵机错误仍保留
    def test_servo_fault_preserved(self):
        merged = merge_status(dict(self.wall, ready=False, errors={"fault": "servo-jam"}), self.base)
        self.assertEqual(merged["errors"], {"fault": "servo-jam"})
        self.assertTrue(merged["other_devices_ready"])
        self.assertFalse(merged["ready"])

    # test_collisions 不覆盖原有错误，即使原有键含servo前缀也能保留
    def test_collisions(self):
        self.base["errors"] = {"fault": "legacy", "servo.fault": "legacy-named"}
        merged = merge_status(dict(self.wall, errors={"fault": "servo-jam"}), self.base)
        self.assertEqual(merged["errors"], {"fault": "legacy", "servo.fault": "legacy-named", "servo.servo.fault": "servo-jam"})

    # test_base_failure_without_errors 原有状态ok=false不能被合并入口掩盖
    def test_base_failure_without_errors(self):
        merged = merge_status(self.wall, dict(self.base, ok=False))
        self.assertIn("other_devices", merged["errors"])
        self.assertFalse(merged["other_devices_ready"])

    # test_malformed_errors 原有错误字段类型不正确也不能声明设备健康
    def test_malformed_errors(self):
        for value in ("failure", "", [], None):
            with self.subTest(value=value):
                merged = merge_status(self.wall, dict(self.base, errors=value))
                self.assertTrue(merged["errors"])
                self.assertFalse(merged["other_devices_ready"])

    # test_inputs_unchanged 合并不修改任一来源，便于独立调试
    def test_inputs_unchanged(self):
        before = copy.deepcopy((self.wall, self.base))
        merge_status(self.wall, self.base)
        self.assertEqual((self.wall, self.base), before)

    # test_non_object_status 原有状态不是JSON对象时直接暴露错误
    def test_non_object_status(self):
        with self.assertRaises(ValueError):
            merge_status(self.wall, [])


if __name__ == "__main__":
    unittest.main()
