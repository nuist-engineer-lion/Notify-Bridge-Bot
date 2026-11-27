"""临时静音：暂停向内部群推送提醒，解除时汇总发出。

mute_until 语义：
  0  → 未静音
  -1 → 不限时静音（直到手动 unmute）
  >0 → 静音截止时间戳（epoch 秒）
"""

from __future__ import annotations

import asyncio
import time

from nonebot.adapters.onebot.v11.exception import ActionFailed

from . import config as cfg
from .config import log
from .models import CustomerData, DelayedNotification
from .state import save_state
from .utils import format_duration, is_night_time

# 不限时静音哨兵值
MUTE_UNLIMITED = -1.0
_flush_lock = asyncio.Lock()


def is_muted() -> bool:
    if cfg.mute_until == MUTE_UNLIMITED:
        return True
    return cfg.mute_until > 0 and time.time() < cfg.mute_until


def is_unlimited() -> bool:
    return cfg.mute_until == MUTE_UNLIMITED


def get_mute_remaining() -> float:
    """限时静音返回剩余秒数；不限时返回 inf；未静音返回 0。"""
    if is_unlimited():
        return float("inf")
    if not is_muted():
        return 0.0
    return max(0.0, cfg.mute_until - time.time())


def describe_mute_status() -> str:
    if is_unlimited():
        return "静音中（不限时）"
    if not is_muted():
        return "未静音"
    remaining = get_mute_remaining()
    return f"静音中（剩余 {format_duration(remaining)}）"


def set_mute(minutes: float | None = None) -> float:
    """开启静音。minutes 为 None 或省略时不限时，直到手动解除。返回 mute_until 值。"""
    if minutes is None:
        cfg.mute_until = MUTE_UNLIMITED
        save_state()
        log.info("已开启静音")
        return MUTE_UNLIMITED

    if minutes <= 0:
        raise ValueError("静音时长必须大于 0")

    until = time.time() + minutes * 60.0
    cfg.mute_until = until
    save_state()
    log.info("已开启临时静音: %.1f 分钟，截止 %s", minutes, time.strftime("%H:%M:%S", time.localtime(until)))
    return until


def clear_mute() -> bool:
    """解除静音。返回解除前是否处于静音。"""
    was_active = cfg.mute_until != 0
    cfg.mute_until = 0.0
    if was_active:
        save_state()
        log.info("已解除临时静音")
    return was_active


def expire_if_due() -> bool:
    """巡检调用：限时静音到期则自动解除。不限时静音不会到期。"""
    if cfg.mute_until > 0 and time.time() >= cfg.mute_until:
        log.info("临时静音已到期，自动解除")
        cfg.mute_until = 0.0
        save_state()
        return True
    return False


def should_defer_notification() -> bool:
    """提醒是否应延后：夜间模式或临时静音。"""
    return is_night_time() or is_muted()


async def unmute_and_flush() -> tuple[bool, int, str]:
    """
    解除静音并汇总延后通知（群内 .unmute 与终端 unmute 共用）。
    返回 (原先是否静音, 汇总发出的客户数, 结果描述)。
    命令回执文案由调用方决定发到群还是仅本地打印。
    """
    was = clear_mute()
    pending_before = len(cfg.delayed_notifications)
    client_up = bool(cfg.client.is_running)
    flushed = await flush_delayed_notifications(reason="unmute")
    pending_after = len(cfg.delayed_notifications)

    if flushed > 0:
        text = f"已解除静音，并汇总发出 {flushed} 名客户的延后提醒。"
    elif any(notif.get("forward_unconfirmed") for notif in cfg.delayed_notifications):
        text = "延后转发的发送结果待确认。请核对群记录，确需补发时，在连接恢复后的非夜间时段执行 unmute。"
    elif was and not client_up:
        text = (
            f"已解除静音；客户端未运行，仍有 {pending_before} 条延后通知，"
            "将在客户端恢复后补发。"
        )
    elif was and is_night_time():
        text = "已解除静音；当前仍在夜间模式，延后通知将在次日汇总发送。"
    elif was and pending_after > 0:
        text = f"已解除静音；仍有 {pending_after} 条延后通知未能发出，请稍后重试 unmute。"
    elif not was and pending_before > 0 and not client_up:
        text = (
            f"当前未处于静音状态；客户端未运行，仍有 {pending_before} 条延后通知，"
            "将在客户端恢复后补发。"
        )
    elif not was and pending_before > 0:
        text = f"当前未处于静音状态；仍有 {pending_before} 条延后通知待处理。"
    elif was:
        text = "已解除静音；暂无延后通知。"
    else:
        text = "当前未处于静音状态。"
    return was, flushed, text


def queue_delayed_notification(
    notify_type: str,
    customers: list[tuple[int, CustomerData]],
    milestone: int | None = None,
) -> None:
    notif: DelayedNotification = {
        "type": notify_type,
        "customers": customers,
        "milestone": milestone,
        "timestamp": time.time(),
    }
    cfg.delayed_notifications.append(notif)
    reason = "静音" if is_muted() and not is_night_time() else "夜间模式"
    log.info("%s：%s 通知已延后，涉及 %d 名客户", reason, notify_type, len(customers))


async def flush_delayed_notifications(
    reason: str = "unmute", *, max_age_seconds: int | None = None,
    night_summary_date: str | None = None,
) -> int:
    """
    汇总并发送延后通知到内部群。
    夜间时段保留队列，night_summary 可在汇总窗口发送，但始终遵守临时静音。
    由群内 .unmute、终端 unmute（经 unmute_and_flush）或巡检静音到期触发。
    返回发送涉及的客户数；未发送返回 0。
    """
    async with _flush_lock:
        return await _flush_delayed_notifications(
            reason, max_age_seconds=max_age_seconds, night_summary_date=night_summary_date,
        )


async def _flush_delayed_notifications(
    reason: str, *, max_age_seconds: int | None, night_summary_date: str | None,
) -> int:
    from .message_sender import build_reminder_nodes, mention_available_member, register_reminder, send_reminder_nodes

    if is_muted():
        return 0
    if reason != "night_summary" and is_night_time():
        log.info("当前仍在夜间模式，延后通知保留至次日汇总 (reason=%s)", reason)
        return 0
    if night_summary_date is not None and cfg.last_night_summary_sent_date == night_summary_date:
        return 0
    if not cfg.client.is_running:
        log.warning("客户端未运行，延后通知暂不发送 (reason=%s)", reason)
        return 0
    if not cfg.delayed_notifications:
        if night_summary_date is not None:
            cfg.last_night_summary_sent_date = night_summary_date
            save_state()
        return 0

    # 保留已持久化的待发快照，直到收到发送成功结果；取消与异常均不消耗队列。
    snapshot: list[DelayedNotification] = []
    customers_aggregated: dict[int, CustomerData] = {}
    for notif in cfg.delayed_notifications:
        active = {}
        for qq, data in notif["customers"]:
            current = cfg.unreplied_customers.get(qq)
            if current is None or current["pending_since"] != data["pending_since"]:
                continue
            old_sid, current_sid = data.get("session_id"), current.get("session_id")
            if old_sid is not None and current_sid is not None and old_sid != current_sid:
                continue
            active[qq] = current
        if active and notif.get("forward_unconfirmed") and reason != "unmute":
            continue
        snapshot.append(notif)
        customers_aggregated.update(active)

    snapshot_ids = {id(notif) for notif in snapshot}

    def consume_snapshot() -> None:
        # await 发送期间可能有新通知入队，只移除本次取出的对象。
        cfg.delayed_notifications[:] = [
            notif for notif in cfg.delayed_notifications if id(notif) not in snapshot_ids
        ]
        if night_summary_date is not None and not cfg.delayed_notifications:
            cfg.last_night_summary_sent_date = night_summary_date

    if not customers_aggregated:
        if snapshot:
            consume_snapshot()
            save_state()
        return 0

    customers_list = list(customers_aggregated.items())
    if reason == "unmute":
        summary_text = (
            f"🔕 静音解除，期间共有 {len(customers_list)} 名客户发来消息，请及时处理。"
        )
    elif reason == "expire":
        summary_text = (
            f"🔕 静音到期，期间共有 {len(customers_list)} 名客户发来消息，请及时处理。"
        )
    elif reason == "night_summary":
        summary_text = f"🌙 夜间免打扰时段汇总：共有 {len(customers_list)} 名客户发来消息，请及时处理。"
    else:
        summary_text = (
            f"🔕 延后通知补发，共有 {len(customers_list)} 名客户发来消息，请及时处理。"
        )

    save_state()
    group_id = cfg.INTERNAL_GROUP_ID
    try:
        nodes = await build_reminder_nodes(group_id, customers_list, max_age_seconds)
    except Exception as e:
        log.error("延后通知构造失败 (reason=%s): %s", reason, e, exc_info=True)
        return 0
    if not nodes or not cfg.client.is_running or is_muted():
        return 0
    if any(cfg.unreplied_customers.get(qq) is not data for qq, data in customers_list):
        return 0

    # 请求发出后可能丢回执或重启。提前落盘，自动巡检只重试确定未发送的批次。
    newly_marked = [notif for notif in snapshot if not notif.get("forward_unconfirmed")]
    for notif in newly_marked:
        notif["forward_unconfirmed"] = True
    if not save_state():
        for notif in newly_marked:
            notif.pop("forward_unconfirmed", None)
        return 0
    try:
        message_id = await send_reminder_nodes(group_id, nodes)
    except ActionFailed as e:
        for notif in newly_marked:
            notif.pop("forward_unconfirmed", None)
        save_state()
        log.warning("协议端拒绝延后转发 (reason=%s): %s", reason, e)
        return 0
    except Exception as e:
        log.warning("延后转发回执未确认，保留队列。请核对群记录，需要补发时执行 unmute: %s", e)
        return 0

    consume_snapshot()
    notice_type = "night_summary" if reason == "night_summary" else "mute_flush"
    # register_reminder 在首次 await 前登记消息 ID 并连同队列一起保存。
    await register_reminder(group_id, message_id, customers_list, summary_text, notice_type)
    await mention_available_member(group_id, summary_text)
    log.info(
        "延后通知已汇总发送 (reason=%s, msg_id=%s)：%s",
        reason,
        message_id,
        summary_text,
    )
    return len(customers_list)
