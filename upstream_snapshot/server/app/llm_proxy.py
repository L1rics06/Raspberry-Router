"""LLM 出口代理：树莓派 Agent 经此调用大模型，密钥只存 VPS。

转发 OpenAI 兼容的 /chat/completions 请求体；鉴权用设备 token
（Header: X-Device-Id + Authorization: Bearer <device_token>）。
"""
import logging

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response

from . import auth, config

log = logging.getLogger("piagent.llm")
router = APIRouter()


@router.post("/api/llm/chat/completions")
async def llm_chat_completions(
    request: Request,
    x_device_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    device_id = x_device_id
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not device_id or not auth.verify_device_token(device_id, token):
        raise HTTPException(401, "设备鉴权失败：需要 X-Device-Id 与 Bearer 设备 token")

    if not config.LLM_BASE_URL or not config.LLM_API_KEY:
        raise HTTPException(503, "LLM 未配置：请在服务端设置 PIAGENT_LLM_BASE_URL / PIAGENT_LLM_API_KEY")

    body = await request.json()
    body.setdefault("model", config.LLM_MODEL)
    body.setdefault("stream", False)
    if body.get("stream"):
        raise HTTPException(400, "暂不支持 stream=true")

    url = f"{config.LLM_BASE_URL}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            upstream = await client.post(
                url, json=body,
                headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
            )
    except httpx.HTTPError as e:
        log.exception("LLM 上游请求失败")
        raise HTTPException(502, f"LLM 上游请求失败: {e}") from e

    if upstream.status_code != 200:
        log.warning("LLM 上游 %s: %s", upstream.status_code, upstream.text[:300])
    return Response(content=upstream.text, status_code=upstream.status_code,
                    media_type="application/json")
