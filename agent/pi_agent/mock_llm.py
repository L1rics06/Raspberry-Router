"""mock 模式 LLM：无 API Key 时的规则引擎。

返回与真实 LLM 完全相同的 OpenAI 消息结构（含 tool_calls），
因此走的是同一条 Agent 循环，链路验证有效。
"""
import json
import re


def _tool_call(call_id: str, name: str, args: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }],
    }


def _final(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _turn_on(text: str) -> bool:
    """判断开还是关：默认开；出现关/闭/停/灭/off 时为关。"""
    return not re.search(r"关|闭|停|灭|off|false", text.lower())


async def mock_chat(messages: list, tools: list | None = None) -> dict:
    available = {t["function"]["name"] for t in (tools or [])}

    # 上一条是工具结果 → 总结收尾
    if messages and messages[-1]["role"] == "tool":
        name_by_id = {}
        for m in messages:
            for tc in m.get("tool_calls") or []:
                name_by_id[tc["id"]] = tc["function"]["name"]
        summaries = []
        for m in messages:
            if m["role"] != "tool":
                continue
            name = name_by_id.get(m.get("tool_call_id"), "?")
            content = m.get("content", "")
            try:
                data = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                data = None
            if isinstance(data, dict) and "temp" in data and "hum" in data:
                summaries.append(f"当前温度 {data['temp']}°C，湿度 {data['hum']}%。")
            elif isinstance(data, dict) and "ok" in data:
                summaries.append(f"{name} 执行成功。")
            else:
                summaries.append(f"{name} 返回: {content}")
        return _final("".join(summaries) or "已完成。")

    user_text = ""
    for m in reversed(messages):
        if m["role"] == "user":
            user_text = str(m.get("content", ""))
            break

    low = user_text.lower()

    def want(name: str) -> bool:
        return name in available

    if re.search(r"风扇|吹风|凉快", low) and want("fan.set"):
        return _tool_call("call_mock_1", "fan.set", {"state": _turn_on(low)})
    if re.search(r"灯|led|亮", low) and want("led.set"):
        return _tool_call("call_mock_1", "led.set", {"on": _turn_on(low)})
    if re.search(r"继电器|relay", low) and want("relay.set"):
        return _tool_call("call_mock_1", "relay.set",
                          {"ch": 0, "state": _turn_on(low)})
    if re.search(r"温|湿度|热|冷|多少度|sensor", low) and want("dht.read"):
        return _tool_call("call_mock_1", "dht.read", {})

    return _final(
        "（mock 模式）当前是离线规则模式，我只能识别风扇/灯/继电器/温湿度相关指令。"
        "在 VPS 配置 LLM API Key 后即可获得完整的自然语言能力。"
    )
