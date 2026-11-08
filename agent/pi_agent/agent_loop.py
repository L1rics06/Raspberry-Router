"""工具调用循环，设备或协议失败后立即停止，不交给模型掩盖失败"""
import json
import logging

from .mcp_client import MCPCallError
from .safety import check_tool

log = logging.getLogger("piagent.loop")
SYSTEM_PROMPT = """你是树莓派设备管家，通过工具操作已授权设备
控制设备必须实际调用工具，不可只口头宣称完成
墙灯light.set接受布尔on，true为开灯按压，false为关灯按压
工具完成后可说已完成按压流程，但lamp_state=unknown时不可声称已检测到灯光亮灭
工具返回错误、未就绪或被拒绝时明确说明失败，不重试按压
传感器读数使用工具实际返回值，不编造
继电器需手动确认，回答简短明确"""


# failure 统一生成失败结果，message为原因，actions保留已尝试轨迹
# text保存JSON，便于网关验收同时核对内外层ok
def failure(message: str, actions: list[str], payload=None) -> dict:
    body = payload if payload is not None else {"ok": False, "error": message}
    return {"ok": False, "text": json.dumps(body, ensure_ascii=False), "actions": list(actions)}


# run_agent_loop 执行一次聊天，text为指令，mcp/llm/cfg沿用上游接口
# 参数、权限、协议或硬件失败时立即返回，不自动重复物理动作
async def run_agent_loop(text: str, mcp, llm, cfg, confirmed: bool = False) -> dict:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}]
    actions = []
    tools = mcp.openai_tools()
    try:
        for _step in range(cfg.max_steps):
            msg = await llm.chat(messages, tools if tools else None)
            calls = msg.get("tool_calls")
            if not calls:
                return {"ok": True, "text": str(msg.get("content") or "").strip(), "actions": actions}
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for call in calls:
                function = call["function"]
                name = mcp.real_name(function["name"])
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except ValueError:
                    return failure("INVALID_ARGUMENT_JSON", actions)
                if not isinstance(args, dict):
                    return failure("ARGUMENTS_MUST_BE_OBJECT", actions)
                verdict = check_tool(name, args, mcp.schema_of(name), cfg.allow_tools, cfg.confirm_tools, confirmed)
                if not verdict.ok:
                    return failure(verdict.reason, actions)
                actions.append(f"{name}({json.dumps(args, ensure_ascii=False)})")
                try:
                    content = await mcp.call(name, args)
                except MCPCallError as error:
                    return failure("DEVICE_FAILED", actions, error.payload)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        return failure("MAX_STEPS_REACHED", actions)
    except Exception as error:
        log.error("Agent调用失败：%s", error)
        return failure(str(error), actions)


# run_tool_direct 直控一个工具，参数沿用上游网关协议
# 设备ok=false和协议isError都返回外层ok=false
async def run_tool_direct(tool: str, args: dict, mcp, cfg, confirmed: bool) -> dict:
    verdict = check_tool(tool, args, mcp.schema_of(tool), cfg.allow_tools, cfg.confirm_tools, confirmed)
    if not verdict.ok:
        return failure(verdict.reason, [])
    actions = [f"{tool}({json.dumps(args, ensure_ascii=False)})"]
    try:
        content = await mcp.call(tool, args)
    except MCPCallError as error:
        return failure("DEVICE_FAILED", actions, error.payload)
    except Exception as error:
        return failure(str(error), actions)
    return {"ok": True, "text": content, "actions": actions}
