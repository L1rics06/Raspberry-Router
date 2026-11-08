"""前端用户 WebSocket 通道 /ws/user：聊天指令下发 + 结果/遥测/上下线推送。

消息格式见 docs/接口文档/api_frontend.md。
"""
import json
import logging
import time

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from . import auth, db
from .state import dispatch_command, hub

log = logging.getLogger("piagent.wsuser")
router = APIRouter()


@router.websocket("/ws/user")
async def user_ws(ws: WebSocket, token: str = Query(default="")) -> None:
    payload = auth.decode_token(token)
    user = db.get_user_by_id(int(payload["sub"])) if payload else None
    if user is None:
        await ws.close(code=4401)
        return

    await ws.accept()
    user_d = {"id": user["id"], "username": user["username"]}
    hub.add_user(user["id"], ws)
    log.info("用户 %s WS 上线", user["username"])

    # hello：附带当前绑定设备状态快照，前端可立即渲染
    devices = []
    for d in db.list_devices_by_user(user["id"]):
        telem = json.loads(d["last_telemetry"]) if d["last_telemetry"] else None
        devices.append({
            "device_id": d["id"],
            "online": d["id"] in hub.devices,
            "caps": json.loads(d["caps"] or "[]"),
            "last_seen": d["last_seen"],
            "telemetry": telem,
        })
    await ws.send_text(json.dumps({
        "type": "hello", "user": user_d, "devices": devices, "ts": time.time(),
    }, ensure_ascii=False))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                await ws.send_text(json.dumps(
                    {"type": "error", "code": "bad_json"}, ensure_ascii=False))
                continue

            mtype = msg.get("type")
            if mtype == "chat":
                text = str(msg.get("text", "")).strip()
                if not text:
                    await ws.send_text(json.dumps(
                        {"type": "error", "code": "empty_text"}, ensure_ascii=False))
                    continue
                resp = await dispatch_command(
                    user_d, msg.get("device_id"), "chat", {"text": text},
                    source_ws=ws,
                )
                if resp.get("status") == "error":
                    await ws.send_text(json.dumps(resp, ensure_ascii=False))
            elif mtype == "ping":
                await ws.send_text(json.dumps({"type": "pong"}, ensure_ascii=False))
            else:
                await ws.send_text(json.dumps(
                    {"type": "error", "code": "unknown_type",
                     "message": f"不支持的消息类型 {mtype!r}"}, ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    finally:
        hub.drop_user(user["id"], ws)
        log.info("用户 %s WS 下线", user["username"])
