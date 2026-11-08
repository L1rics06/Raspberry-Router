"""REST：用户认证。

安全模型（2026-11 起）：**仅访问令牌登录**。
- 注册与用户名/密码登录均已关闭——开放注册会让任何访客拿到账号；
  弱密码共享（演示账号）也等于人人可操控设备。
- 访问令牌 `pat-<hex32>` 由运维在服务端 manage.py 签发/轮换，只显示一次，
  服务端只存 sha256。凭令牌换取 JWT（7 天），后续请求走 JWT 不变。
- 令牌为 128bit 随机数不可爆破；失败响应固定延时挫败撞库枚举。
"""
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import auth, db

router = APIRouter(prefix="/api/auth")


class TokenLoginBody(BaseModel):
    token: str = Field(min_length=8, max_length=128)


@router.post("/token-login")
async def token_login(body: TokenLoginBody):
    user = db.get_user_by_access_token(auth.hash_access_token(body.token))
    if user is None:
        await asyncio.sleep(0.6)   # 固定失败延时，挫败令牌枚举
        raise HTTPException(401, "访问令牌无效")
    return {"id": user["id"], "username": user["username"], "token": auth.make_token(user["id"])}


class LegacyBody(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=64)


@router.post("/register", status_code=201)
def register(body: LegacyBody):
    raise HTTPException(403, "注册已关闭：访问令牌由运维签发，请联系管理员")


@router.post("/login")
def login(body: LegacyBody):
    raise HTTPException(403, "密码登录已停用：请使用访问令牌登录")
