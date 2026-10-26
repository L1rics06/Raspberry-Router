#!/usr/bin/env python3
"""运维脚本：建用户 / 建设备（打印一次性 token）/ 查看设备。

用法（在 server/ 目录下）：
    python manage.py add-user  <用户名> <密码>
    python manage.py add-device <设备ID> [--owner 用户名]
    python manage.py list-devices
"""
import argparse
import json
import secrets
import sys

from app import auth, db


def cmd_add_user(args):
    if db.get_user_by_name(args.username):
        sys.exit(f"用户 {args.username} 已存在")
    pw_hash, salt = auth.hash_password(args.password)
    uid = db.create_user(args.username, pw_hash, salt)
    print(f"用户创建成功: id={uid} username={args.username}")


def cmd_add_device(args):
    if db.get_device(args.device_id):
        sys.exit(f"设备 {args.device_id} 已存在")
    owner_id = None
    if args.owner:
        u = db.get_user_by_name(args.owner)
        if not u:
            sys.exit(f"用户 {args.owner} 不存在，请先 add-user")
        owner_id = u["id"]
    token = f"dev-{secrets.token_hex(16)}"
    db.create_device(args.device_id, auth.hash_device_token(token), owner_id)
    print(f"设备创建成功: id={args.device_id}")
    if owner_id:
        print(f"已绑定用户: {args.owner}")
    print("设备 token（只显示这一次，请妥善保存，配置到树莓派 agent）:")
    print(f"  {token}")


def cmd_list_devices(args):
    dev = db.all_device_ids()
    if not dev:
        print("（无设备）")
        return
    for d in dev:
        row = db.get_device(d)
        owner = row["owner_user_id"] or "-"
        print(f"  {d}  owner_user_id={owner}  last_seen={row['last_seen']}")


def main():
    db.init_db()
    p = argparse.ArgumentParser(description="PiAgent 网关运维脚本")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add-user"); s.add_argument("username"); s.add_argument("password")
    s.set_defaults(fn=cmd_add_user)

    s = sub.add_parser("add-device"); s.add_argument("device_id")
    s.add_argument("--owner", default=None); s.set_defaults(fn=cmd_add_device)

    s = sub.add_parser("list-devices"); s.set_defaults(fn=cmd_list_devices)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
