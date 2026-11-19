"""对话周期与好友变化历史存储层（SQLite）。

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
from collections.abc import Iterator
from contextlib import contextmanager
from typing import NamedTuple

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

CREATE TABLE IF NOT EXISTS friend_daily_stats (
    day TEXT PRIMARY KEY,
    added INTEGER,
    removed INTEGER,
    total INTEGER NOT NULL,
    friend_ids TEXT,
    pending INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS group_daily_stats (
    group_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    total INTEGER NOT NULL,
    change INTEGER,
    PRIMARY KEY (group_id, day)
);

CREATE TABLE IF NOT EXISTS morning_report_deliveries (
    day TEXT NOT NULL,
    group_id INTEGER NOT NULL,
    PRIMARY KEY (day, group_id)
);
"""


class FriendDailyStats(NamedTuple):
    day: str
    added: int | None
    removed: int | None
    total: int


class GroupDailyStats(NamedTuple):
    day: str
    total: int
    change: int | None
    added: int | None
    removed: int | None


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(get_db_path(), timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        with conn:
            yield conn
    finally:
        conn.close()


# ======================= 同步核心接口 =======================

def init_db_sync() -> None:
    path = get_db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(group_daily_stats)")}
        for column, kind in (("added", "INTEGER"), ("removed", "INTEGER"), ("member_ids", "TEXT")):
            if column not in columns:
                conn.execute(f"ALTER TABLE group_daily_stats ADD COLUMN {column} {kind}")
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


def has_message_sync(message_id: int) -> bool:
    """复用消息唯一索引，识别重连、重启或会话关闭后重投的消息。"""
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM session_events WHERE message_id = ?",
            (message_id,),
        ).fetchone() is not None


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


def _latest_friend_snapshot() -> tuple[FriendDailyStats, set[int], bool] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT day, added, removed, total, friend_ids, pending FROM friend_daily_stats ORDER BY day DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    day, added, removed, total, friend_ids, pending = row
    return FriendDailyStats(day, added, removed, total), set(json.loads(friend_ids)), bool(pending)


def _record_friend_snapshot(stats: FriendDailyStats, friend_ids: set[int], pending: bool) -> bool:
    with _connect() as conn:
        inserted = conn.execute(
            "INSERT INTO friend_daily_stats (day, added, removed, total, friend_ids, pending) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(day) DO NOTHING",
            (*stats, json.dumps(sorted(friend_ids)), pending),
        )
        if not inserted.rowcount:
            return False
        # 历史只保留人数，最新名单与当天统计在同一个事务内提交。
        conn.execute(
            "UPDATE friend_daily_stats SET friend_ids = NULL, pending = 0 WHERE friend_ids IS NOT NULL AND day < ?",
            (stats.day,),
        )
        return True


def _mark_friend_summary_sent(day: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE friend_daily_stats SET pending = 0 WHERE day = ?", (day,))


def _delivered_morning_report_groups(day: str) -> set[int]:
    with _connect() as conn:
        rows = conn.execute("SELECT group_id FROM morning_report_deliveries WHERE day = ?", (day,))
        return {group_id for (group_id,) in rows}


def _record_morning_report_delivery(day: str, group_id: int) -> None:
    with _connect() as conn:
        conn.execute("INSERT OR IGNORE INTO morning_report_deliveries (day, group_id) VALUES (?, ?)", (day, group_id))


def _recent_friend_stats(limit: int) -> list[tuple[str, int, int, int]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT day, added, removed, total FROM friend_daily_stats WHERE added IS NOT NULL ORDER BY day DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return rows


def _recent_group_stats(group_id: int, limit: int) -> list[GroupDailyStats]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT day, total, change, added, removed FROM group_daily_stats WHERE group_id = ? ORDER BY day DESC LIMIT ?",
            (group_id, limit),
        ).fetchall()
    return [GroupDailyStats(*row) for row in rows]


def _compare_group_snapshot(conn: sqlite3.Connection, group_id: int, day: str | None, members: set[int]) -> GroupDailyStats:
    previous = conn.execute(
        "SELECT total, member_ids FROM group_daily_stats WHERE group_id = ? AND (? IS NULL OR day < ?) "
        "ORDER BY day DESC LIMIT 1",
        (group_id, day, day),
    ).fetchone()
    baseline = set(json.loads(previous[1])) if previous and previous[1] is not None else None
    total = len(members)
    return GroupDailyStats(
        day or "", total, total - previous[0] if previous else None,
        len(members - baseline) if baseline is not None else None,
        len(baseline - members) if baseline is not None else None,
    )


def _preview_group_snapshot(group_id: int, members: set[int]) -> GroupDailyStats:
    with _connect() as conn:
        return _compare_group_snapshot(conn, group_id, None, members)


def _record_group_snapshot(group_id: int, day: str, members: set[int]) -> GroupDailyStats:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        stats = _compare_group_snapshot(conn, group_id, day, members)
        conn.execute(
            "INSERT INTO group_daily_stats (group_id, day, total, change, added, removed, member_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(group_id, day) DO NOTHING",
            (group_id, *stats, json.dumps(sorted(members))),
        )
        conn.execute(
            "UPDATE group_daily_stats SET member_ids = NULL WHERE group_id = ? AND day < ? AND member_ids IS NOT NULL",
            (group_id, day),
        )
        row = conn.execute(
            "SELECT day, total, change, added, removed FROM group_daily_stats WHERE group_id = ? AND day = ?",
            (group_id, day),
        ).fetchone()
    return GroupDailyStats(*row)


# ======================= 异步封装 =======================

async def init_db() -> None:
    await asyncio.to_thread(init_db_sync)


async def latest_friend_snapshot() -> tuple[FriendDailyStats, set[int], bool] | None:
    return await asyncio.to_thread(_latest_friend_snapshot)


async def record_friend_snapshot(stats: FriendDailyStats, friend_ids: set[int], pending: bool) -> bool:
    return await asyncio.to_thread(_record_friend_snapshot, stats, friend_ids, pending)


async def mark_friend_summary_sent(day: str) -> None:
    await asyncio.to_thread(_mark_friend_summary_sent, day)


async def delivered_morning_report_groups(day: str) -> set[int]:
    return await asyncio.to_thread(_delivered_morning_report_groups, day)


async def record_morning_report_delivery(day: str, group_id: int) -> None:
    await asyncio.to_thread(_record_morning_report_delivery, day, group_id)


async def recent_friend_stats(limit: int) -> list[tuple[str, int, int, int]]:
    return await asyncio.to_thread(_recent_friend_stats, limit)


async def recent_group_stats(group_id: int, limit: int) -> list[GroupDailyStats]:
    return await asyncio.to_thread(_recent_group_stats, group_id, limit)


async def record_group_snapshot(group_id: int, day: str, members: set[int]) -> GroupDailyStats:
    return await asyncio.to_thread(_record_group_snapshot, group_id, day, members)


async def preview_group_snapshot(group_id: int, members: set[int]) -> GroupDailyStats:
    return await asyncio.to_thread(_preview_group_snapshot, group_id, members)


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


async def has_message(message_id: int) -> bool:
    return await asyncio.to_thread(has_message_sync, message_id)


async def get_session_message_ids(session_id: int) -> set[int]:
    return await asyncio.to_thread(get_session_message_ids_sync, session_id)


async def close_stale_open_sessions(exclude_ids: set[int], ended_at: float, reason: str) -> int:
    return await asyncio.to_thread(close_stale_open_sessions_sync, exclude_ids, ended_at, reason)


async def load_recent_durations(limit: int) -> list[float]:
    return await asyncio.to_thread(load_recent_durations_sync, limit)


async def cleanup_expired(retention_days: int, now: float) -> int:
    return await asyncio.to_thread(cleanup_expired_sync, retention_days, now)
