"""MCP 客户端池：按配置拉起若干设备 MCP Server（stdio），动态发现工具。

Agent 对具体硬件零感知：加设备 = 在配置里加一个 [[mcp.servers]]。
"""
import asyncio
import json
import logging
import os
import re
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("piagent.mcp")

# 部分 LLM 提供商（如 DeepSeek）严格要求函数名匹配 ^[a-zA-Z0-9_-]+$，
# 而本项目工具契约是 <设备>.<动作>（如 fan.set），发给 LLM 前做名称净化，
# 执行时再映射回真实 MCP 工具名。
_LLM_INVALID = re.compile(r"[^a-zA-Z0-9_-]")


class MCPCallError(RuntimeError):
    """保留设备失败JSON，使HTTP和聊天得到一致失败状态"""

    # __init__ 接收失败payload，保留原字段并生成JSON文本
    def __init__(self, payload: dict):
        self.payload = dict(payload, ok=False)
        super().__init__(json.dumps(self.payload, ensure_ascii=False))


def llm_tool_name(name: str) -> str:
    """fan.set -> fan_set（仅用于发给 LLM 的 schema / 回传的 tool_calls）。"""
    return _LLM_INVALID.sub("_", name)


class MCPPool:
    def __init__(self, servers_cfg: list) -> None:
        self._cfgs = servers_cfg
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}   # server name → session
        self._owner: dict[str, str] = {}                # tool name → server name
        self._tools: dict[str, dict] = {}               # tool name → {description, inputSchema}
        self._real_by_llm: dict[str, str] = {}          # 净化名 → 真实工具名

    async def start(self) -> None:
        for cfg in self._cfgs:
            env = dict(os.environ)
            env.update(cfg.env or {})
            params = StdioServerParameters(
                command=cfg.command, args=cfg.args, env=env)
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            resp = await session.list_tools()
            names = []
            for t in resp.tools:
                # mcp SDK 2.x 字段为 input_schema，1.x 为 inputSchema
                schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
                if t.name in self._tools:
                    raise RuntimeError(f"MCP工具重名：{t.name}，请使用统一设备入口")
                if llm_tool_name(t.name) in self._real_by_llm:
                    raise RuntimeError(f"LLM净化后的工具名冲突：{t.name}")
                self._tools[t.name] = {
                    "description": t.description or "",
                    "inputSchema": dict(schema) if schema
                    else {"type": "object", "properties": {}},
                }
                self._owner[t.name] = cfg.name
                self._real_by_llm[llm_tool_name(t.name)] = t.name
                names.append(t.name)
            self._sessions[cfg.name] = session
            log.info("MCP server '%s' 已启动，工具: %s", cfg.name, names)
        if not self._tools:
            log.warning("没有发现任何 MCP 工具，Agent 将只能对话不能操作硬件")

    async def stop(self) -> None:
        await self._stack.aclose()

    # -------- 工具视图 --------

    def tool_names(self) -> list[str]:
        return list(self._tools)

    def openai_tools(self) -> list[dict]:
        """转换为 OpenAI function-calling 的 tools schema（工具名做净化）。"""
        return [
            {"type": "function", "function": {
                "name": llm_tool_name(name),
                "description": meta["description"] or name,
                "parameters": meta["inputSchema"],
            }}
            for name, meta in self._tools.items()
        ]

    def real_name(self, llm_name: str) -> str:
        """把 LLM 回传的净化名（fan_set）映射回真实 MCP 工具名（fan.set）。"""
        return self._real_by_llm.get(llm_name, llm_name)

    def schema_of(self, name: str) -> dict | None:
        meta = self._tools.get(name)
        return meta["inputSchema"] if meta else None

    # -------- 调用 --------

    # call 调用一个工具，name为真实MCP名，args为已校验对象
    # 同时检查协议isError和设备ok，超时后不重发动作
    async def call(self, name: str, args: dict) -> str:
        owner = self._owner.get(name)
        if owner is None:
            raise MCPCallError({"ok": False, "error": "UNKNOWN_TOOL", "tool": name})
        session = self._sessions[owner]
        result = await asyncio.wait_for(session.call_tool(name, args), 15)
        parts = getattr(result, "content", None) or []
        texts = [c.text for c in parts if hasattr(c, "text")]
        text = "\n".join(texts)
        if getattr(result, "isError", False):
            raise MCPCallError({"ok": False, "error": "MCP_PROTOCOL_ERROR", "detail": text})
        try:
            payload = json.loads(text)
        except ValueError as error:
            raise MCPCallError({"ok": False, "error": "INVALID_TOOL_RESULT", "detail": text[:300]}) from error
        if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
            raise MCPCallError({"ok": False, "error": "MISSING_BOOLEAN_OK"})
        if payload["ok"] is False:
            raise MCPCallError(payload)
        return text
