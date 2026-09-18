"""临时静音：暂停向内部群推送提醒，解除时汇总发出。

mute_until 语义：
  0  → 未静音
  -1 → 不限时静音（直到手动 unmute）
  >0 → 静音截止时间戳（epoch 秒）
"""

from __future__ import annotations

import time

from . import config as cfg
from .config import log
from .models import CustomerData, DelayedNotification
from .state import save_state
from .utils import format_duration, is_night_time

# 不限时静音哨兵值
MUTE_UNLIMITED = -1.0


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
        log.info("已开启不限时静音（直到手动解除）")
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
    flushed = await flush_delayed_notifications(reason="unmute")
    if not was and flushed == 0:
        text = "当前未处于静音状态。"
    elif flushed > 0:
        text = f"已解除静音，并汇总发出 {flushed} 名客户的延后提醒。"
    elif was and is_night_time():
        text = "已解除静音；当前仍在夜间模式，延后通知将在次日汇总发送。"
    else:
        text = "已解除静音；暂无延后通知。"
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


async def flush_delayed_notifications(reason: str = "unmute") -> int:
    """
    汇总并发送延后通知到内部群。
    夜间时段仍保留队列（交给夜间汇总），其他情况立即发出。
    仅由群内命令或巡检到期触发；终端控制台不调用本函数。
    返回发送涉及的客户数；未发送返回 0。
    """
    from .message_sender import send_reminder_with_at  # 延迟导入避免循环依赖

    if is_night_time():
        log.info("当前仍在夜间模式，延后通知保留至次日汇总 (reason=%s)", reason)
        return 0
    if not cfg.delayed_notifications:
        return 0
    if not cfg.client.is_running:
        log.warning("客户端未运行，延后通知暂不发送 (reason=%s)", reason)
        return 0

    customers_aggregated: dict[int, CustomerData] = {}
    for notif in cfg.delayed_notifications:
        for qq, data in notif["customers"]:
            customers_aggregated[qq] = data
    cfg.delayed_notifications.clear()
    save_state()

    if not customers_aggregated:
        return 0

    customers_list = list(customers_aggregated.items())
    summary_text = (
        f"🔕 静音解除，期间共有 {len(customers_list)} 名客户发来消息，请及时处理。"
        if reason == "unmute"
        else f"🔕 静音到期，期间共有 {len(customers_list)} 名客户发来消息，请及时处理。"
    )
    try:
        await send_reminder_with_at(
            cfg.INTERNAL_GROUP_ID,
            summary_text,
            customers_list,
            notice_type="mute_flush",
        )
        log.info("静音延后通知已汇总发送 (reason=%s)：%s", reason, summary_text)
    except Exception as e:
        log.error("静音延后通知发送失败: %s", e, exc_info=True)
        cfg.delayed_notifications.append({
            "type": "mute_flush",
            "customers": customers_list,
            "milestone": None,
            "timestamp": time.time(),
        })
        save_state()
        return 0

    return len(customers_list)
