"""REST：设备绑定 / 列表 / 历史记录 / 手动控制台直控。"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import db
from .auth import get_current_user
from .state import dispatch_command, hub

router = APIRouter(prefix="/api")


class BindBody(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    device_token: str = Field(min_length=1, max_length=128)


class CommandBody(BaseModel):
    tool: str = Field(min_length=1, max_length=64)
    args: dict = Field(default_factory=dict)
    confirmed: bool = False   # 高危工具二次确认标志（由 Agent 闸门校验）
    wait: bool = True         # 阻塞等待设备回传结果（便于 curl/前端直控调试）


def _device_view(d) -> dict:
    return {
        "device_id": d["id"],
        "online": d["id"] in hub.devices,
        "caps": json.loads(d["caps"] or "[]"),
        "last_seen": d["last_seen"],
        "telemetry": json.loads(d["last_telemetry"]) if d["last_telemetry"] else None,
        "bound": d["owner_user_id"] is not None,
    }


@router.post("/devices/bind")
def bind_device(body: BindBody, user: dict = Depends(get_current_user)):
    """用户凭设备 token 认领设备（一个设备只能绑一个用户）。"""
    dev = db.get_device(body.device_id)
    from . import auth
    if not dev or not auth.verify_device_token(body.device_id, body.device_token):
        raise HTTPException(404, "设备不存在或 token 错误")
    if dev["owner_user_id"] and dev["owner_user_id"] != user["id"]:
        raise HTTPException(409, "该设备已被其他用户绑定")
    db.bind_device(body.device_id, user["id"])
    return {"device_id": body.device_id, "owner": user["username"]}


@router.get("/devices")
def list_devices(user: dict = Depends(get_current_user)):
    return [_device_view(d) for d in db.list_devices_by_user(user["id"])]


@router.get("/history")
def history(device_id: str | None = None, limit: int = 50,
            user: dict = Depends(get_current_user)):
    rows = db.list_history(user["id"], device_id, min(limit, 500))
    return [
        {
            "cmd_id": r["cmd_id"], "device_id": r["device_id"], "kind": r["kind"],
            "payload": json.loads(r["payload"]) if r["payload"].startswith("{") else r["payload"],
            "ok": None if r["ok"] is None else bool(r["ok"]),
            "result_text": r["result_text"],
            "actions": json.loads(r["actions"] or "[]"),
            "created_at": r["created_at"], "finished_at": r["finished_at"],
        }
        for r in rows
    ]


@router.post("/devices/{device_id}/command")
async def manual_command(device_id: str, body: CommandBody,
                         user: dict = Depends(get_current_user)):
    """手动控制台：绕过 Agent/LLM，直控设备工具（kind=tool）。"""
    resp = await dispatch_command(
        user, device_id, "tool",
        {"tool": body.tool, "args": body.args, "confirmed": body.confirmed},
        wait=body.wait,
    )
    if resp.get("status") == "error":
        code = resp.get("code", "error")
        status = 503 if code == "device_offline" else 400
        raise HTTPException(status, f"{resp.get('message')} [code={code}]")
    return resp
