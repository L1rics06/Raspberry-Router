"""树莓派侧 WebSocket 通道 /ws/device：实现 docs/接口文档/protocol_pi_vps.md。"""
import asyncio
import json
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import auth, db
from .state import hub, complete_result

log = logging.getLogger("piagent.wsdev")
router = APIRouter()


async def _send(ws: WebSocket, msg: dict) -> None:
    await ws.send_text(json.dumps(msg, ensure_ascii=False))


@router.websocket("/ws/device")
async def device_ws(ws: WebSocket) -> None:
    await ws.accept()

    # 1) 连接后 10s 内必须完成 register
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        msg = json.loads(raw)
    except Exception:
        await ws.close(code=4400)
        return
    if msg.get("type") != "register":
        await _send(ws, {"type": "error", "code": "expected_register",
                         "message": "连接建立后第一条消息必须是 register"})
        await ws.close(code=4400)
        return

    device_id = str(msg.get("device_id", ""))
    token = str(msg.get("token", ""))
    caps = msg.get("caps") or []
    if not isinstance(caps, list):
        caps = []

    # 2) 设备鉴权
    if not device_id or not auth.verify_device_token(device_id, token):
        await _send(ws, {"type": "error", "code": "auth_failed",
                         "message": "device_id 不存在或 token 错误"})
        await ws.close(code=4401)
        return

    from . import config
    conn = await hub.register_device(device_id, ws, caps)
    db.device_seen(device_id, caps)
    await _send(ws, {"type": "register_ack", "ok": True,
                     "heartbeat_interval": config.HEARTBEAT_INTERVAL,
                     "server_time": time.time()})
    await hub.push_device_status(device_id, online=True)
    log.info("设备上线: %s caps=%s", device_id, caps)

    # 3) 消息循环
    try:
        while True:
            raw = await ws.receive_text()
            conn.last_seen = time.time()
            db.device_seen(device_id)
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                await _send(ws, {"type": "error", "code": "bad_json"})
                continue

            mtype = msg.get("type")
            if mtype == "heartbeat":
                telemetry = msg.get("telemetry")
                if telemetry is not None:
                    db.save_telemetry(device_id, telemetry)
                    dev = db.get_device(device_id)
                    if dev and dev["owner_user_id"]:
                        await hub.push_to_user(
                            dev["owner_user_id"],
                            {"type": "telemetry", "device_id": device_id,
                             "data": telemetry, "ts": time.time()},
                        )
                await _send(ws, {"type": "heartbeat_ack", "ts": time.time()})

            elif mtype == "result":
                await complete_result(msg)

            else:
                await _send(ws, {"type": "error", "code": "unknown_type",
                                 "message": f"不支持的消息类型 {mtype!r}"})

    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("设备 %s 连接异常", device_id)
    finally:
        if hub.unregister_device(conn):
            db.device_seen(device_id)
            await hub.push_device_status(device_id, online=False)
            log.info("设备离线: %s", device_id)
