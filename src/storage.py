"""对话周期历史存储层（SQLite）。

会话（sessions）与周期事件（session_events）的表结构和读写全部集中在本模块。
内部使用同步 sqlite3 短连接，对外提供 async 接口（经 asyncio.to_thread），
同步接口供独立脚本（如存量导入工具）复用。

会话时间段语义：
- started_at：对话周期起点（客户进入待回复队列的时间）
- ended_at：周期终点（会话关闭时间）；started_at/ended_at 是历史对账的权威窗口
"""

import asyncio
import json
import os
import sqlite3

from . import config as cfg
from .config import log


def _default_db_path() -> str:
    return os.path.join(cfg.ARCHIVE_DIR, "archive.db")


# 外部脚本可覆盖（如 --db 参数）；为 None 时按 cfg.ARCHIVE_DIR 动态解析
DB_PATH: str | None = None


def get_db_path() -> str:
    return DB_PATH if DB_PATH is not None else _default_db_path()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_uid INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    started_at REAL NOT NULL,
    ended_at REAL,
    duration REAL,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_uid_time ON sessions(customer_uid, started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    time REAL NOT NULL,
    event_type TEXT NOT NULL,
    actor_uid INTEGER,
    message_id INTEGER,
    payload TEXT NOT NULL DEFAULT '{}'
);
-- message_id 全表唯一：同一条 QQ 消息多来源上报（实时采集 / 历史拉取对账）时自动去重；
-- 对账审计等无消息 ID 的事件不建索引（partial index 允许多个 NULL）
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_message_id ON session_events(message_id) WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_session ON session_events(session_id, time);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ======================= 同步核心接口 =======================

def init_db_sync() -> None:
    path = get_db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)
    log.info("会话库已就绪: %s", path)


def open_session_sync(customer_uid: int, started_at: float) -> int:
    """创建一个打开状态的会话周期，返回 session_id。"""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (customer_uid, status, started_at) VALUES (?, 'open', ?)",
            (customer_uid, started_at),
        )
        return int(cur.lastrowid)


def get_open_session_sync(customer_uid: int) -> int | None:
    """查询客户最新一个打开的会话周期。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE customer_uid = ? AND status = 'open' ORDER BY started_at DESC LIMIT 1",
            (customer_uid,),
        ).fetchone()
    return int(row[0]) if row else None


def close_session_by_window_sync(session_id: int, started_at: float, ended_at: float, close_reason: str) -> None:
    """关闭会话周期（耗时由 started_at 计算），写入终点与关闭原因。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET status = 'closed', ended_at = ?, duration = ?, close_reason = ? WHERE id = ?",
            (ended_at, round(ended_at - started_at, 3), close_reason, session_id),
        )


def close_stale_open_sessions_sync(exclude_ids: set[int], ended_at: float, reason: str) -> int:
    """关闭指定集合之外的所有 open 会话（重启孤儿清理），返回关闭数量。"""
    with _connect() as conn:
        rows = conn.execute("SELECT id FROM sessions WHERE status = 'open'").fetchall()
        stale = [int(r[0]) for r in rows if int(r[0]) not in exclude_ids]
        for sid in stale:
            conn.execute(
                "UPDATE sessions SET status = 'closed', ended_at = ?, close_reason = ? WHERE id = ?",
                (ended_at, reason, sid),
            )
    return len(stale)


def record_event_sync(
    session_id: int,
    event_type: str,
    *,
    time: float,
    actor_uid: int | None = None,
    message_id: int | None = None,
    payload: dict | None = None,
) -> bool:
    """写入一条周期事件。message_id 撞唯一索引（重复上报）时忽略并返回 False。"""
    encoded = json.dumps(payload or {}, ensure_ascii=False, default=str)
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO session_events (session_id, time, event_type, actor_uid, message_id, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, time, event_type, actor_uid, message_id, encoded),
        )
        return cur.rowcount > 0


def get_session_message_ids_sync(session_id: int) -> set[int]:
    """查询会话内已有 message_id 的事件集合（用于对账核对）。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT message_id FROM session_events WHERE session_id = ? AND message_id IS NOT NULL",
            (session_id,),
        ).fetchall()
    return {int(r[0]) for r in rows}


def load_recent_durations_sync(limit: int) -> list[float]:
    """按时间正序返回最近 limit 个已完结会话的耗时（用于重启后恢复统计）。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT duration FROM sessions WHERE status = 'closed' AND duration IS NOT NULL "
            "ORDER BY ended_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [float(r[0]) for r in reversed(rows)]


def cleanup_expired_sync(retention_days: int, now: float) -> int:
    """删除超出保留期的已完结会话及其事件，返回删除的会话数。"""
    cutoff = now - retention_days * 86400
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM session_events WHERE session_id IN "
            "(SELECT id FROM sessions WHERE status = 'closed' AND ended_at IS NOT NULL AND ended_at < ?)",
            (cutoff,),
        )
        cur = conn.execute(
            "DELETE FROM sessions WHERE status = 'closed' AND ended_at IS NOT NULL AND ended_at < ?",
            (cutoff,),
        )
        return cur.rowcount


def find_session_id_sync(customer_uid: int, started_at: float) -> int | None:
    """按（客户，起点时间）查找会话，供导入工具幂等判断。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE customer_uid = ? AND started_at = ? LIMIT 1",
            (customer_uid, started_at),
        ).fetchone()
    return int(row[0]) if row else None


# ======================= 异步封装 =======================

async def init_db() -> None:
    await asyncio.to_thread(init_db_sync)


async def open_session(customer_uid: int, started_at: float) -> int:
    return await asyncio.to_thread(open_session_sync, customer_uid, started_at)


async def get_open_session(customer_uid: int) -> int | None:
    return await asyncio.to_thread(get_open_session_sync, customer_uid)


async def close_session_record(session_id: int, started_at: float, ended_at: float, close_reason: str) -> None:
    await asyncio.to_thread(close_session_by_window_sync, session_id, started_at, ended_at, close_reason)


async def record_event(
    session_id: int,
    event_type: str,
    *,
    time: float,
    actor_uid: int | None = None,
    message_id: int | None = None,
    payload: dict | None = None,
) -> bool:
    return await asyncio.to_thread(
        record_event_sync, session_id, event_type,
        time=time, actor_uid=actor_uid, message_id=message_id, payload=payload,
    )


async def get_session_message_ids(session_id: int) -> set[int]:
    return await asyncio.to_thread(get_session_message_ids_sync, session_id)


async def close_stale_open_sessions(exclude_ids: set[int], ended_at: float, reason: str) -> int:
    return await asyncio.to_thread(close_stale_open_sessions_sync, exclude_ids, ended_at, reason)


async def load_recent_durations(limit: int) -> list[float]:
    return await asyncio.to_thread(load_recent_durations_sync, limit)


async def cleanup_expired(retention_days: int, now: float) -> int:
    return await asyncio.to_thread(cleanup_expired_sync, retention_days, now)
