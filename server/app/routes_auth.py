"""REST：用户注册 / 登录。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import auth, db

router = APIRouter(prefix="/api/auth")


class RegisterBody(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=64)


@router.post("/register", status_code=201)
def register(body: RegisterBody):
    if db.get_user_by_name(body.username):
        raise HTTPException(409, "用户名已被占用")
    pw_hash, salt = auth.hash_password(body.password)
    uid = db.create_user(body.username, pw_hash, salt)
    return {"id": uid, "username": body.username, "token": auth.make_token(uid)}


@router.post("/login")
def login(body: RegisterBody):
    user = db.get_user_by_name(body.username)
    if not user or not auth.verify_password(body.password, user["salt"], user["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    return {"id": user["id"], "username": user["username"], "token": auth.make_token(user["id"])}
