"""手写 tool-use 循环：LLM → 工具调用(MCP) → 观察 → 汇报。"""
import json
import logging

from .safety import check_tool

log = logging.getLogger("piagent.loop")

SYSTEM_PROMPT = """\
你是运行在树莓派上的物联网智能体 PiAgent。
用户通过远程连接向你下达自然语言指令，你通过工具操作真实硬件（灯、风扇、继电器等）或读取传感器。

工作规则：
1. 优先调用工具完成任务，不要只口头描述步骤；
2. 动作类工具执行成功后，用一句简短自然的中文向用户确认（如"已为你打开风扇"）；
3. 查询类问题先读传感器，再用数据回答（如"现在 26.4°C，有点热"）；
4. 工具返回错误或被安全闸门拒绝时，如实向用户说明原因，不要假装成功；
5. 高危工具（如继电器）被拒绝时，提示用户可在手动控制台里确认后直控；
6. 与设备控制无关的请求，礼貌说明你是设备管家，只能帮忙操作硬件和读取传感器。

回答保持简短（一两句话），语气自然。\
"""


async def run_agent_loop(text: str, mcp, llm, cfg, confirmed: bool = False) -> dict:
    """执行一次 chat 指令。返回 {"ok","text","actions"}。"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    tools = mcp.openai_tools()
    actions: list[str] = []

    try:
        for _step in range(cfg.max_steps):
            msg = await llm.chat(messages, tools if tools else None)
            tool_calls = msg.get("tool_calls")

            if not tool_calls:
                return {"ok": True, "text": str(msg.get("content") or "").strip(),
                        "actions": actions}

            messages.append({"role": "assistant", "content": msg.get("content"),
                             "tool_calls": tool_calls})

            for tc in tool_calls:
                fn = tc["function"]
                name = fn["name"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = None

                verdict = check_tool(
                    name, args or {}, mcp.schema_of(name),
                    cfg.allow_tools, cfg.confirm_tools, confirmed,
                )
                if args is None:
                    content = f"错误：工具 {name} 的 arguments 不是合法 JSON"
                elif not verdict.ok:
                    log.warning("安全闸门拒绝 %s: %s", name, verdict.reason)
                    content = f"被安全闸门拒绝：{verdict.reason}"
                else:
                    try:
                        content = await mcp.call(name, args)
                        log.info("工具 %s(%s) -> %s", name, args, content[:80])
                    except Exception as e:  # noqa: BLE001
                        content = f"工具执行异常: {e}"

                actions.append(f"{name}({json.dumps(args or {}, ensure_ascii=False)})")
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": content})

        return {"ok": False, "text": "任务步数达到上限，已中止（可换个说法再试）",
                "actions": actions}

    except Exception as e:  # noqa: BLE001
        log.exception("Agent 循环异常")
        return {"ok": False, "text": f"Agent 内部错误: {e}", "actions": actions}


async def run_tool_direct(tool: str, args: dict, mcp, cfg, confirmed: bool) -> dict:
    """手动控制台路径：绕过 LLM 直控工具。"""
    verdict = check_tool(tool, args, mcp.schema_of(tool),
                         cfg.allow_tools, cfg.confirm_tools, confirmed)
    if not verdict.ok:
        return {"ok": False, "text": verdict.reason, "actions": []}
    if mcp.schema_of(tool) is None:
        return {"ok": False, "text": f"工具 {tool} 不存在。可用: {mcp.tool_names()}",
                "actions": []}
    try:
        content = await mcp.call(tool, args)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "text": f"工具执行异常: {e}", "actions": []}
    return {"ok": True, "text": content, "actions": [f"{tool}({json.dumps(args, ensure_ascii=False)})"]}
