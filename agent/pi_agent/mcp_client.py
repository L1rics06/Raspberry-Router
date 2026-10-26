"""MCP 客户端池：按配置拉起若干设备 MCP Server（stdio），动态发现工具。

Agent 对具体硬件零感知：加设备 = 在配置里加一个 [[mcp.servers]]。
"""
import logging
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("piagent.mcp")


class MCPPool:
    def __init__(self, servers_cfg: list) -> None:
        self._cfgs = servers_cfg
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}   # server name → session
        self._owner: dict[str, str] = {}                # tool name → server name
        self._tools: dict[str, dict] = {}               # tool name → {description, inputSchema}

    async def start(self) -> None:
        for cfg in self._cfgs:
            params = StdioServerParameters(
                command=cfg.command, args=cfg.args, env=cfg.env or None)
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            resp = await session.list_tools()
            names = []
            for t in resp.tools:
                # mcp SDK 2.x 字段为 input_schema，1.x 为 inputSchema
                schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
                if t.name in self._tools:
                    log.warning("工具重名 %s（%s 与先前 server 冲突，后者覆盖前者）",
                                t.name, cfg.name)
                self._tools[t.name] = {
                    "description": t.description or "",
                    "inputSchema": dict(schema) if schema
                    else {"type": "object", "properties": {}},
                }
                self._owner[t.name] = cfg.name
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
        """转换为 OpenAI function-calling 的 tools schema。"""
        return [
            {"type": "function", "function": {
                "name": name,
                "description": meta["description"] or name,
                "parameters": meta["inputSchema"],
            }}
            for name, meta in self._tools.items()
        ]

    def schema_of(self, name: str) -> dict | None:
        meta = self._tools.get(name)
        return meta["inputSchema"] if meta else None

    # -------- 调用 --------

    async def call(self, name: str, args: dict) -> str:
        owner = self._owner.get(name)
        if owner is None:
            return f"错误：工具 {name} 不存在"
        session = self._sessions[owner]
        result = await session.call_tool(name, args)
        parts = getattr(result, "content", None) or []
        texts = [c.text for c in parts if hasattr(c, "text")]
        return "\n".join(texts) if texts else f"(无文本输出: {result})"
