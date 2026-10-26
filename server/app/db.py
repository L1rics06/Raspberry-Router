"""SQLite 持久层：用户 / 设备 / 指令历史（审计日志）。

量级很小（课程项目），用 stdlib sqlite3 + 线程锁足够，不引入 ORM。
"""
import json
import sqlite3
import threading
import time

from .config import DB_PATH

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id             TEXT PRIMARY KEY,          -- 如 "pi-dorm-01"
    token_hash     TEXT NOT NULL,             -- sha256(设备token)
    owner_user_id  INTEGER,                   -- 绑定的用户，NULL=未绑定
    caps           TEXT NOT NULL DEFAULT '[]',-- 设备上报的工具能力清单
    created_at     REAL NOT NULL,
    last_seen      REAL,                      -- 最后心跳/消息时间戳
    last_telemetry TEXT                       -- 最近一次遥测 JSON
);

CREATE TABLE IF NOT EXISTS history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cmd_id       TEXT NOT NULL,
    user_id      INTEGER NOT NULL,
    device_id    TEXT NOT NULL,
    kind         TEXT NOT NULL,               -- chat | tool
    payload      TEXT NOT NULL,               -- 原始文本 或 {"tool":..,"args":..}
    ok           INTEGER,                     -- NULL=进行中 1=成功 0=失败
    result_text  TEXT,
    actions      TEXT,                        -- JSON 数组，如 ["fan.set(true)"]
    created_at   REAL NOT NULL,
    finished_at  REAL
);

CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_cmd  ON history(cmd_id);
"""


def init_db() -> None:
    global _conn
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    _conn.commit()


def _exec(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def _query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return _conn.execute(sql, params).fetchall()


# ---------------- users ----------------

def create_user(username: str, password_hash: str, salt: str) -> int:
    cur = _exec(
        "INSERT INTO users(username, password_hash, salt, created_at) VALUES(?,?,?,?)",
        (username, password_hash, salt, time.time()),
    )
    return int(cur.lastrowid)


def get_user_by_name(username: str) -> sqlite3.Row | None:
    rows = _query("SELECT * FROM users WHERE username=?", (username,))
    return rows[0] if rows else None


def get_user_by_id(user_id: int) -> sqlite3.Row | None:
    rows = _query("SELECT * FROM users WHERE id=?", (user_id,))
    return rows[0] if rows else None


# ---------------- devices ----------------

def create_device(device_id: str, token_hash: str, owner_user_id: int | None = None) -> None:
    _exec(
        "INSERT INTO devices(id, token_hash, owner_user_id, created_at) VALUES(?,?,?,?)",
        (device_id, token_hash, owner_user_id, time.time()),
    )


def get_device(device_id: str) -> sqlite3.Row | None:
    rows = _query("SELECT * FROM devices WHERE id=?", (device_id,))
    return rows[0] if rows else None


def list_devices_by_user(user_id: int) -> list[sqlite3.Row]:
    return _query(
        "SELECT * FROM devices WHERE owner_user_id=? ORDER BY created_at", (user_id,)
    )


def bind_device(device_id: str, user_id: int) -> None:
    _exec("UPDATE devices SET owner_user_id=? WHERE id=?", (user_id, device_id))


def device_seen(device_id: str, caps: list[str] | None = None) -> None:
    if caps is None:
        _exec("UPDATE devices SET last_seen=? WHERE id=?", (time.time(), device_id))
    else:
        _exec(
            "UPDATE devices SET last_seen=?, caps=? WHERE id=?",
            (time.time(), json.dumps(caps, ensure_ascii=False), device_id),
        )


def save_telemetry(device_id: str, telemetry: dict) -> None:
    _exec(
        "UPDATE devices SET last_seen=?, last_telemetry=? WHERE id=?",
        (time.time(), json.dumps(telemetry, ensure_ascii=False), device_id),
    )


def all_device_ids() -> list[str]:
    return [r["id"] for r in _query("SELECT id FROM devices")]


# ---------------- history ----------------

def insert_history(cmd_id: str, user_id: int, device_id: str, kind: str, payload) -> None:
    _exec(
        "INSERT INTO history(cmd_id, user_id, device_id, kind, payload, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (cmd_id, user_id, device_id, kind,
         payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
         time.time()),
    )


def finish_history(cmd_id: str, ok: bool | None, result_text: str, actions: list) -> None:
    _exec(
        "UPDATE history SET ok=?, result_text=?, actions=?, finished_at=? WHERE cmd_id=?",
        (None if ok is None else int(ok), result_text,
         json.dumps(actions, ensure_ascii=False), time.time(), cmd_id),
    )


def list_history(user_id: int, device_id: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    if device_id:
        return _query(
            "SELECT * FROM history WHERE user_id=? AND device_id=?"
            " ORDER BY id DESC LIMIT ?", (user_id, device_id, limit),
        )
    return _query(
        "SELECT * FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
    )
