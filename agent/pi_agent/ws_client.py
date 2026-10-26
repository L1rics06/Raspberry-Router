"""树莓派侧 WS 客户端：出站长连接 + 心跳 + 断线重连 + 指令处理。"""
import asyncio
import json
import logging

import websockets

from .agent_loop import run_agent_loop, run_tool_direct

log = logging.getLogger("piagent.ws")


class AgentClient:
    def __init__(self, cfg, mcp, llm):
        self.cfg = cfg
        self.mcp = mcp
        self.llm = llm
        self._lock = asyncio.Lock()      # 指令串行化，避免硬件状态竞争
        self._hb_interval = 15           # register_ack 后按服务端要求调整

    # ---------------- 主循环：连了断、断了连 ----------------

    async def run(self) -> None:
        url = self.cfg.server_url.rstrip("/") + "/ws/device"
        backoff = 1
        while True:
            try:
                async with websockets.connect(url, open_timeout=15) as ws:
                    log.info("已连接网关 %s", url)
                    await self._register(ws)
                    await self._session(ws)
                    backoff = 1
                    log.info("连接关闭，准备重连")
            except asyncio.CancelledError:
                log.info("收到退出信号")
                return
            except Exception as e:  # noqa: BLE001
                log.warning("连接异常: %s; %ss 后重连", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _register(self, ws) -> None:
        caps = self.mcp.tool_names()
        await ws.send(json.dumps({
            "type": "register",
            "device_id": self.cfg.device_id,
            "token": self.cfg.device_token,
            "caps": caps,
        }, ensure_ascii=False))
        ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if ack.get("type") == "error":
            raise RuntimeError(f"注册被拒绝: {ack.get('code')} {ack.get('message', '')}")
        if ack.get("type") != "register_ack" or not ack.get("ok"):
            raise RuntimeError(f"注册响应异常: {ack}")
        self._hb_interval = int(ack.get("heartbeat_interval", 15))
        log.info("注册成功，工具能力已上报: %s（心跳 %ss）", caps, self._hb_interval)

    async def _session(self, ws) -> None:
        hb = asyncio.create_task(self._heartbeat_loop(ws))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                mtype = msg.get("type")
                if mtype == "cmd":
                    # 后台处理，不阻塞心跳与后续消息接收
                    asyncio.create_task(self._handle_cmd(ws, msg))
                elif mtype == "heartbeat_ack":
                    pass
                elif mtype == "error":
                    log.warning("网关错误消息: %s", msg)
                else:
                    log.debug("忽略消息: %s", msg)
        finally:
            hb.cancel()

    # ---------------- 心跳（可捎带遥测） ----------------

    async def _heartbeat_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(self._hb_interval)
            telemetry = None
            tool = self.cfg.telemetry_tool
            if tool and tool in self.mcp.tool_names():
                try:
                    content = await self.mcp.call(tool, {})
                    telemetry = json.loads(content)
                except Exception:  # noqa: BLE001
                    telemetry = None
            payload = {"type": "heartbeat", "ts": _now()}
            if telemetry:
                payload["telemetry"] = telemetry
            await ws.send(json.dumps(payload, ensure_ascii=False))

    # ---------------- 指令处理 ----------------

    async def _handle_cmd(self, ws, msg: dict) -> None:
        cmd_id = msg.get("id", "")
        payload = msg.get("payload") or {}
        kind = payload.get("kind")
        confirmed = bool(payload.get("confirmed"))

        async with self._lock:
            log.info("处理指令 %s kind=%s payload=%s", cmd_id, kind, payload)
            try:
                if kind == "chat":
                    res = await run_agent_loop(
                        str(payload.get("text", "")), self.mcp, self.llm,
                        self.cfg, confirmed=False)
                elif kind == "tool":
                    res = await run_tool_direct(
                        str(payload.get("tool", "")), dict(payload.get("args") or {}),
                        self.mcp, self.cfg, confirmed)
                else:
                    res = {"ok": False, "text": f"未知指令类型 {kind!r}", "actions": []}
            except Exception as e:  # noqa: BLE001
                log.exception("指令 %s 处理异常", cmd_id)
                res = {"ok": False, "text": f"Agent 异常: {e}", "actions": []}

        await ws.send(json.dumps({
            "type": "result", "id": cmd_id,
            "ok": res["ok"], "text": res["text"], "actions": res["actions"],
        }, ensure_ascii=False))
        log.info("指令 %s 完成 ok=%s: %s", cmd_id, res["ok"], res["text"][:80])


def _now() -> float:
    import time
    return time.time()
