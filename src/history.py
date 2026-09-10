"""对话周期历史记录：会话生命周期管理、实时事件采集与历史拉取对账。

会话周期从客户进入待回复队列开始，到会话关闭结束。周期内事件实时写入
SQLite（storage.py），写入失败时落盘 JSONL 兜底；会话关闭后按会话的
权威时间段 [started_at, ended_at] 拉取双方历史消息对账补漏。
"""

import asyncio
import json
import os
import time

from . import storage
from .config import log, ARCHIVE_DIR, unreplied_customers, client

# 事件写库失败时的兜底文件（可人工检查/重放）
FAILED_EVENTS_FILE = os.path.join(ARCHIVE_DIR, "failed_events.jsonl")

# QQ 时间戳为秒级整数，对账窗口边界放宽容忍取整偏差
RECONCILE_WINDOW_TOLERANCE = 2.0


def serialize_segments(segments) -> list[dict]:
    """将 napcat 消息段转为可 JSON 序列化的 OB11 结构。"""
    try:
        return [dict(seg) for seg in segments]
    except Exception:
        try:
            return [dict(seg) if not isinstance(seg, str) else {"type": "text", "data": {"text": seg}}
                    for seg in segments]
        except Exception:
            return []


def _text_as_segments(message) -> list[dict]:
    """把字符串或消息段列表统一转为 OB11 结构（用于机器人主动发送的消息）。"""
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}]
    return serialize_segments(message)


def _dump_failed(event_type: str, record: dict) -> None:
    """事件写库失败时落盘，保证数据不丢，可人工重放。"""
    try:
        os.makedirs(os.path.dirname(FAILED_EVENTS_FILE) or ".", exist_ok=True)
        with open(FAILED_EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"failed_at": time.time(), "event_type": event_type, **record},
                               ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        log.error("事件兜底落盘失败: %s", e)


async def _safe_record(
    session_id: int,
    event_type: str,
    *,
    time_: float | None = None,
    actor_uid: int | None = None,
    message_id: int | None = None,
    **payload,
) -> bool:
    ts = time_ if time_ is not None else time.time()
    try:
        inserted = await storage.record_event(
            session_id, event_type, time=ts, actor_uid=actor_uid, message_id=message_id, payload=payload,
        )
        if not inserted:
            log.debug("事件因 message_id 重复被跳过: session=%s type=%s mid=%s", session_id, event_type, message_id)
        return inserted
    except Exception as e:
        log.error("事件写入失败(已落盘兜底): session=%s type=%s err=%s", session_id, event_type, e, exc_info=True)
        _dump_failed(event_type, {
            "session_id": session_id, "time": ts, "actor_uid": actor_uid,
            "message_id": message_id, "payload": payload,
        })
        return False


# ======================= 会话生命周期 =======================

async def ensure_session(uid: int) -> int | None:
    """确保队列中客户拥有打开的会话周期，返回 session_id；客户不在队列时返回 None。"""
    data = unreplied_customers.get(uid)
    if data is None:
        return None
    sid = data.get("session_id")
    if sid is not None:
        return sid
    try:
        sid = await storage.get_open_session(uid)
        if sid is None:
            sid = await storage.open_session(uid, data["pending_since"])
            await _safe_record(sid, "session_open", time_=data["pending_since"], customer_uid=uid)
        data["session_id"] = sid
    except Exception as e:
        log.error("会话周期建立失败: uid=%s, err=%s", uid, e, exc_info=True)
        return None
    return sid


async def close_session_record(
    session_id: int,
    uid: int,
    started_at: float,
    ended_at: float,
    close_reason: str,
    seen_msg_ids: list[int],
) -> None:
    """关闭会话周期并触发历史拉取对账（后台执行）。

    seen_msg_ids 为本运行周期内实时见过的消息 ID（调用时客户刚出队，
    由调用方从 CustomerData 中取出传入），用于对账交叉核对。
    """
    await storage.close_session_record(session_id, started_at, ended_at, close_reason)
    asyncio.create_task(reconcile_session_history(session_id, uid, started_at, ended_at, list(seen_msg_ids)))


# ======================= 实时事件采集 =======================

async def record_customer_message(uid: int, message_id: int, msg_time: float, segments, nickname: str = "") -> None:
    """客户私聊消息：确保会话周期存在后实时落库。"""
    sid = await ensure_session(uid)
    if sid is None:
        return
    await _safe_record(sid, "customer_message", time_=msg_time, actor_uid=uid,
                       message_id=message_id, n=nickname, msg=serialize_segments(segments))


async def record_staff_reply(uid: int, message_id: int | None, msg_time: float, segments,
                             actor_uid: int | None = None, nickname: str = "") -> None:
    """客服侧回复（直接私聊或经机器人发送）。客户周期已结束时跳过。"""
    sid = await ensure_session(uid)
    if sid is None:
        log.debug("客户 %d 无打开的会话周期，跳过 staff_reply 记录", uid)
        return
    await _safe_record(sid, "staff_reply", time_=msg_time, actor_uid=actor_uid,
                       message_id=message_id, n=nickname, msg=serialize_segments(segments))


async def record_bot_send(uid: int, message_id: int | None, kind: str, message, ok: bool = True,
                          session_id: int | None = None) -> None:
    """机器人主动发送给客户的系统消息（结束语/欢迎语等）。

    session_id 可显式传入（如 close_session 中客户已出队的场景），否则自动确保会话周期。
    """
    if session_id is not None:
        sid = session_id
    else:
        sid = await ensure_session(uid)
        if sid is None:
            return
    await _safe_record(sid, "bot_send", actor_uid=int(client.self_id), message_id=message_id,
                       kind=kind, ok=ok, msg=_text_as_segments(message))


async def record_command(uid: int, operator_uid: int, command: str, **payload) -> None:
    """客服在内部群执行的会话操作命令（say/bye/close 等）。"""
    sid = await ensure_session(uid)
    if sid is None:
        return
    await _safe_record(sid, "command", actor_uid=operator_uid, command=command, **payload)


async def record_bot_notice(uid: int, notice_type: str, **payload) -> None:
    """机器人向内部群发送的周期相关通知（新客户提醒/里程碑催办/夜间汇总）。"""
    sid = await ensure_session(uid)
    if sid is None:
        return
    # 群通知消息被多个客户共享，group_msg_id 放 payload，避免占用全局唯一的 message_id
    await _safe_record(sid, "bot_notice", notice_type=notice_type, **payload)


async def record_poke(uid: int, actor_uid: int | None, **payload) -> None:
    """周期内的戳一戳事件。"""
    sid = await ensure_session(uid)
    if sid is None:
        return
    await _safe_record(sid, "poke", actor_uid=actor_uid, **payload)


# ======================= 历史拉取对账 =======================

async def reconcile_session_history(
    session_id: int,
    uid: int,
    window_start: float,
    window_end: float,
    seen_msg_ids: list[int],
) -> None:
    """会话关闭后拉取双方历史消息，按会话权威时间段 [window_start, window_end] 对账补漏。

    实时采集已入库的消息按 message_id 自动去重；拉取与核对结果写入
    history_reconcile 审计事件。拉取失败不影响已在库中的实时事件。
    """
    pulled = inserted = 0
    pulled_ids: set[int] = set()
    msgs: list[dict] | None = None

    try:
        resp = await client.get_friend_msg_history(
            user_id=str(uid),
            count=500,
            parse_mult_msg=True,
        )
        msgs = resp.get("messages", []) or []
    except Exception as e:
        log.error("会话 %d 历史拉取失败（实时事件已在库中，跳过补录）: uid=%s, err=%s",
                  session_id, uid, e, exc_info=True)

    if msgs is not None:
        msgs.sort(key=lambda m: m.get("time", 0))
        self_id = int(client.self_id)
        for msg in msgs:
            t = float(msg.get("time", 0) or 0)
            if t < window_start - RECONCILE_WINDOW_TOLERANCE or t > window_end + RECONCILE_WINDOW_TOLERANCE:
                continue
            pulled += 1
            mid = msg.get("message_id")
            if mid is not None:
                pulled_ids.add(int(mid))
            sender = msg.get("sender", {}) or {}
            sender_uid = sender.get("user_id")
            is_staff = sender_uid is not None and int(sender_uid) == self_id
            ok = await _safe_record(
                session_id,
                "staff_reply" if is_staff else "customer_message",
                time_=t,
                actor_uid=int(sender_uid) if sender_uid is not None else None,
                message_id=int(mid) if mid is not None else None,
                n=sender.get("nickname", ""),
                msg=msg.get("message", []),
                source="history_reconcile",
            )
            if ok:
                inserted += 1

    # 交叉核对：实时已见、但既不在拉取结果也不在库中的消息
    missing: list[int] = []
    try:
        db_ids = await storage.get_session_message_ids(session_id)
        missing = [mid for mid in seen_msg_ids if mid not in pulled_ids and mid not in db_ids]
    except Exception as e:
        log.error("会话 %d 对账核对查询失败: %s", session_id, e, exc_info=True)

    if missing:
        log.warning("会话 %d 对账发现 %d 条实时已见消息未入库且未拉取到: %s",
                    session_id, len(missing), missing)

    await _safe_record(
        session_id, "history_reconcile",
        window=[window_start, window_end],
        pulled=pulled, inserted=inserted, missing_seen=missing,
    )
    log.info("会话 %d 历史对账完成: uid=%d, 窗口=[%.0f, %.0f], 拉取 %d 条, 补录 %d 条%s",
             session_id, uid, window_start, window_end, pulled, inserted,
             f", 缺失 {len(missing)} 条" if missing else "")


# ======================= 启动恢复与统计 =======================

async def recover_sessions() -> None:
    """启动恢复：为队列中缺失 session_id 的客户找回/补建会话周期，并清理重启孤儿会话。"""
    recovered = created = 0
    for uid, data in unreplied_customers.items():
        if data.get("session_id") is not None:
            continue
        try:
            sid = await storage.get_open_session(uid)
            if sid is None:
                sid = await storage.open_session(uid, data["pending_since"])
                await _safe_record(sid, "session_open", time_=data["pending_since"],
                                   customer_uid=uid, recovered=True)
                created += 1
            else:
                recovered += 1
            data["session_id"] = sid
        except Exception as e:
            log.error("恢复客户 %d 会话周期失败: %s", uid, e, exc_info=True)

    queue_sids = {d["session_id"] for d in unreplied_customers.values() if d.get("session_id")}
    try:
        orphans = await storage.close_stale_open_sessions(queue_sids, time.time(), "restart_cleanup")
    except Exception as e:
        log.error("清理孤儿会话失败: %s", e, exc_info=True)
        orphans = 0

    if recovered or created or orphans:
        log.info("会话周期恢复完成: 找回 %d 个, 补建 %d 个, 清理孤儿会话 %d 个", recovered, created, orphans)


async def restore_reply_durations() -> None:
    """从会话库恢复回复耗时统计（解决重启后 deque 统计丢失）。"""
    from .config import reply_durations, REPLY_DURATION_MAXLEN
    try:
        durations = await storage.load_recent_durations(REPLY_DURATION_MAXLEN)
    except Exception as e:
        log.error("恢复回复耗时统计失败: %s", e, exc_info=True)
        return
    reply_durations.extend(durations)
    if durations:
        log.info("已从会话库恢复 %d 条回复耗时统计", len(durations))
