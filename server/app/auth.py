"""鉴权：用户密码（PBKDF2）+ JWT；设备 token（sha256，常数时间比较）。"""
import hashlib
import hmac
import secrets
import time

import jwt
from fastapi import Header, HTTPException

from . import db
from .config import JWT_EXPIRE_HOURS, JWT_SECRET

_PBKDF2_ITER = 120_000


# ---------------- 用户密码 ----------------

def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITER)
    return dk.hex(), salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITER)
    return hmac.compare_digest(dk.hex(), expected_hash)


# ---------------- JWT ----------------

def make_token(user_id: int) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + JWT_EXPIRE_HOURS * 3600},
        JWT_SECRET, algorithm="HS256",
    )


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def get_current_user(authorization: str = Header(default="")) -> dict:
    """FastAPI 依赖：从 Authorization: Bearer <jwt> 解析当前用户。"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer Token")
    payload = decode_token(authorization[7:])
    if not payload:
        raise HTTPException(401, "Token 无效或已过期")
    user = db.get_user_by_id(int(payload["sub"]))
    if not user:
        raise HTTPException(401, "用户不存在")
    return {"id": user["id"], "username": user["username"]}


# ---------------- 设备 token ----------------

def hash_device_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_device_token(device_id: str, token: str) -> bool:
    dev = db.get_device(device_id)
    if not dev:
        return False
    return hmac.compare_digest(hash_device_token(token), dev["token_hash"])
