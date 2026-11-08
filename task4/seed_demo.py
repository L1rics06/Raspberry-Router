"""为隔离的本机演示创建一次性用户和设备"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from .common import write_json


# main 表示演示数据库初始化入口，无参数
# 仅写入参数指定的新数据目录，凭证只写入私有文件
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--credentials", required=True)
    args = parser.parse_args()
    target = Path(args.data_dir).resolve()
    if target.exists() and any(target.iterdir()):
        raise SystemExit("演示数据目录必须为空，拒绝改动既有数据库")
    target.mkdir(parents=True, exist_ok=True)
    os.environ["PIAGENT_DATA_DIR"] = str(target)
    sys.path.insert(0, str(Path(args.repo).resolve() / "server"))
    from app import auth, db
    db.init_db()
    user = "task4-local"
    password_hash, salt = auth.hash_password(secrets.token_hex(24))
    uid = db.create_user(user, password_hash, salt)
    access = "pat-" + secrets.token_hex(16)
    device = "task4-sim-" + secrets.token_hex(4)
    device_token = "dev-" + secrets.token_hex(16)
    db.set_user_access_token(uid, auth.hash_access_token(access))
    db.create_device(device, auth.hash_device_token(device_token), uid)
    write_json(Path(args.credentials), {"access_token": access, "device_token": device_token, "device_id": device}, private=True)
    print("隔离演示账户和设备已创建，凭证未输出")


if __name__ == "__main__":
    main()
