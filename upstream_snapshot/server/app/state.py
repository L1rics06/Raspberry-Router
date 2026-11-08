"""内存态连接注册表（单事件进程内）+ 指令路由核心。

三类对象：
- DeviceConn   在线的树莓派长连接
- 用户 WS 集合 前端聊天/推送通道
- PendingCmd   已下发待回传的指令（cmd_id 关联发起方，供 result 原路路由）
"""
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket

from . import config, db

log = logging.getLogger("piagent.hub")


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


@dataclass
class DeviceConn:
    device_id: str
    ws: WebSocket
    caps: list
    connected_at: float
    last_seen: float


@dataclass
class PendingCmd:
    cmd_id: str
    user_id: int
    device_id: str
    kind: str            # chat | tool
    created_at: float
    deadline: float
    user_ws: WebSocket | None = None      # WS 发起方（聊天路径）；REST 发起则为 None
    event: asyncio.Event = field(default_factory=asyncio.Event)  # REST wait 用
    result: dict | None = None


class Hub:
    def __init__(self) -> None:
        self.devices: dict[str, DeviceConn] = {}
        self.users: dict[int, set[WebSocket]] = {}
        self.pending: dict[str, PendingCmd] = {}

    # ---------------- 设备连接 ----------------

    async def register_device(self, device_id: str, ws: WebSocket, caps: list) -> DeviceConn:
        old = self.devices.get(device_id)
        if old is not None:
            # 同一设备重复上线：踢旧保新（幂等重连场景）
            log.warning("设备 %s 新连接顶替旧连接", device_id)
            try:
                await old.ws.send_text(jdump({"type": "error", "code": "replaced",
                                              "message": "该设备已被新连接顶替"}))
                await old.ws.close(code=4409)
            except Exception:
                pass
        now = time.time()
        conn = DeviceConn(device_id, ws, caps, now, now)
        self.devices[device_id] = conn
        return conn

    def unregister_device(self, conn: DeviceConn) -> bool:
        """仅当当前注册的就是这个连接时才摘除（防止误摘顶替者的新连接）。"""
        cur = self.devices.get(conn.device_id)
        if cur is conn:
            self.devices.pop(conn.device_id, None)
            return True
        return False

    # ---------------- 用户连接 ----------------

    def add_user(self, user_id: int, ws: WebSocket) -> None:
        self.users.setdefault(user_id, set()).add(ws)

    def drop_user(self, user_id: int, ws: WebSocket) -> None:
        conns = self.users.get(user_id)
        if conns:
            conns.discard(ws)
            if not conns:
                self.users.pop(user_id, None)

    async def push_to_user(self, user_id: int, msg: dict) -> None:
        for ws in list(self.users.get(user_id, ())):
            try:
                await ws.send_text(jdump(msg))
            except Exception:
                self.drop_user(user_id, ws)

    async def push_device_status(self, device_id: str, online: bool) -> None:
        dev = db.get_device(device_id)
        if dev and dev["owner_user_id"]:
            await self.push_to_user(
                dev["owner_user_id"],
                {"type": "device_status", "device_id": device_id, "online": online},
            )


hub = Hub()


# ---------------- 指令下发与结果路由 ----------------

async def dispatch_command(
    user: dict,
    device_id: str | None,
    kind: str,
    payload: dict,
    source_ws: WebSocket | None = None,
    wait: bool = False,
) -> dict:
    """把用户指令路由到其绑定的在线设备。REST 与 WS 两条路径共用。

    返回 {"cmd_id","status":"sent"} 或 {"status":"error","code",...}；
    wait=True 时阻塞直到设备回传 result 或超时。
    """
    devices = db.list_devices_by_user(user["id"])
    if not devices:
        return _err("no_device", "没有绑定任何设备，请先调用 POST /api/devices/bind")

    target = None
    if device_id:
        target = next((d for d in devices if d["id"] == device_id), None)
        if not target:
            return _err("device_not_bound", f"设备 {device_id} 未绑定或不存在")
    else:
        online = [d for d in devices if d["id"] in hub.devices]
        target = online[0] if online else devices[0]

    conn = hub.devices.get(target["id"])
    if conn is None:
        return _err("device_offline", f"设备 {target['id']} 当前离线")

    cmd_id = f"c-{uuid.uuid4().hex[:10]}"
    timeout = config.CMD_TIMEOUT_CHAT if kind == "chat" else config.CMD_TIMEOUT_TOOL
    pending = PendingCmd(
        cmd_id=cmd_id, user_id=user["id"], device_id=target["id"], kind=kind,
        created_at=time.time(), deadline=time.time() + timeout, user_ws=source_ws,
    )
    hub.pending[cmd_id] = pending
    db.insert_history(cmd_id, user["id"], target["id"], kind, payload)

    cmd = {"type": "cmd", "id": cmd_id, "payload": {"kind": kind, **payload}}
    try:
        await conn.ws.send_text(jdump(cmd))
    except Exception:
        hub.pending.pop(cmd_id, None)
        return _err("device_offline", f"设备 {target['id']} 连接已断开")

    log.info("cmd %s -> %s (%s)", cmd_id, target["id"], kind)

    if source_ws is not None:
        await source_ws.send_text(jdump(
            {"type": "cmd_ack", "id": cmd_id, "device_id": target["id"], "kind": kind}
        ))

    if wait:
        try:
            await asyncio.wait_for(pending.event.wait(), timeout=timeout + 5)
        except asyncio.TimeoutError:
            pass
        if pending.result is not None:
            return {"cmd_id": cmd_id, "status": "done", **pending.result}
        return {"cmd_id": cmd_id, "status": "timeout"}

    return {"cmd_id": cmd_id, "status": "sent", "device_id": target["id"]}


def _err(code: str, message: str) -> dict:
    return {"status": "error", "code": code, "message": message}


async def complete_result(msg: dict) -> None:
    """设备回传 result：落库 + 按发起路径回推（WS 推送 / REST event 唤醒）。"""
    cmd_id = str(msg.get("id", ""))
    pending = hub.pending.pop(cmd_id, None)
    ok = bool(msg.get("ok", True))
    text = str(msg.get("text", ""))
    actions = msg.get("actions") or []

    db.finish_history(cmd_id, ok, text, actions)
    log.info("result %s ok=%s text=%r", cmd_id, ok, text[:60])

    out = {"type": "result", "id": cmd_id, "ok": ok, "text": text, "actions": actions}
    if pending:
        if pending.user_ws is not None:
            try:
                await pending.user_ws.send_text(jdump(out))
            except Exception:
                hub.drop_user(pending.user_id, pending.user_ws)
        pending.result = out
        pending.event.set()


async def timeout_pending(now: float) -> None:
    """由后台 sweeper 调用：清理超时指令。"""
    for cmd_id, p in list(hub.pending.items()):
        if now > p.deadline:
            hub.pending.pop(cmd_id, None)
            db.finish_history(cmd_id, False, "设备响应超时", [])
            log.warning("cmd %s 超时", cmd_id)
            if p.user_ws is not None:
                try:
                    await p.user_ws.send_text(jdump(
                        {"type": "cmd_error", "id": cmd_id, "code": "timeout",
                         "message": "设备响应超时"}
                    ))
                except Exception:
                    hub.drop_user(p.user_id, p.user_ws)
            p.result = {"type": "cmd_error", "id": cmd_id, "code": "timeout"}
            p.event.set()
