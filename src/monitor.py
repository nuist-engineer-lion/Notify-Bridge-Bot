import time
import asyncio
import statistics
from datetime import datetime, timedelta

from .config import (
    log,
    STARTED_AT,
    INTERNAL_GROUP_ID,
    CLOSING_MESSAGE,
    MILESTONES,
    MAX_LISTEN_AGE,
    NIGHT_SUMMARY_TIME,
    PROCESSED_FRIEND_REQUESTS_EXPIRE,
    reply_durations,
    unreplied_customers,
    monitored_forwards,
    processed_friend_requests,
    friend_approve_time,
    delayed_notifications,
    last_night_summary_sent_date,
    client,
)
from .models import CustomerData, DelayedNotification
from .utils import format_duration, is_night_time
from .forward import send_reminder_with_at, pop_tracked_forward
from .state import save_state, archive_session


async def close_session(user_id: int, send_closing: bool = False) -> bool:
    """
    从 unreplied_customers 中移除客户，记录耗时（如果存在）。
    如果 send_closing=True，则向该客户发送结束语。
    返回是否成功结束（即客户原本在队列中）。
    """
    data = unreplied_customers.pop(user_id, None)
    if data is None:
        return False

    # 记录回复耗时
    pending = data["pending_since"]
    elapsed = time.time() - pending
    reply_durations.append(elapsed)
    log.info("会话结束: user_id=%s, 耗时=%.1f秒", user_id, elapsed)

    # 异步存档，不阻塞主流程
    asyncio.create_task(archive_session(user_id, data["msg_ids"], pending))

    if send_closing:
        try:
            await client.send_private_msg(
                user_id=str(user_id),
                message=CLOSING_MESSAGE,
            )
            log.info("自动发送结束语成功: user_id=%s", user_id)
        except Exception as e:
            log.error("自动发送结束语失败: user_id=%s, err=%s", user_id, e, exc_info=True)

    save_state()  # 状态变更后持久化
    return True


async def send_status_panel(group_id: int):
    """向指定群发送机器人状态统计面板"""
    uptime = time.time() - STARTED_AT
    uptime_str = format_duration(uptime)

    pending_count = len(unreplied_customers)
    total_replies = len(reply_durations)

    # 计算统计值
    if total_replies > 0:
        min_dur = min(reply_durations)
        max_dur = max(reply_durations)
        avg_dur = statistics.mean(reply_durations)
        median_dur = statistics.median(reply_durations)

        min_str = format_duration(min_dur)
        max_str = format_duration(max_dur)
        avg_str = format_duration(avg_dur)
        median_str = format_duration(median_dur)
    else:
        min_str = max_str = avg_str = median_str = "暂无数据"

    # 当前监听中的合并转发数量
    monitored_count = len(monitored_forwards)

    panel = (
        "🤖 机器人状态面板\n"
        f"• 运行时长：{uptime_str}\n"
        f"• 待回复客户数：{pending_count}\n"
        f"• 已完结会话数：{total_replies}\n"
        f"• 最短回复耗时：{min_str}\n"
        f"• 最长回复耗时：{max_str}\n"
        f"• 平均回复耗时：{avg_str}\n"
        f"• 中位数耗时：{median_str}\n"
        f"• 监听合并转发：{monitored_count}\n"
        f"使用 .help 查看可用命令"
    )

    try:
        await client.send_group_msg(
            group_id=str(group_id),
            message=panel,
        )
        log.info("状态面板已发送至群 %d", group_id)
    except Exception as e:
        log.error("发送状态面板失败: %s", e, exc_info=True)


async def monitor_loop():
    global last_night_summary_sent_date
    log.info("巡检任务已启动，每 60 秒执行一次")
    while True:
        await asyncio.sleep(60)

        if not client.is_running:
            log.debug("巡检跳过: 客户端未运行")
            continue

        # ===== 清理超时的监听消息 =====
        now = time.time()
        to_remove = [mid for mid, data in monitored_forwards.items() if now - data["created_at"] > MAX_LISTEN_AGE]
        for mid in to_remove:
            pop_tracked_forward(mid)
        if to_remove:
            log.debug("已清理 %d 条过期监听消息", len(to_remove))

        if not unreplied_customers:
            log.debug("巡检跳过: 当前无未回复客户")
        else:
            log.info("===== 巡检开始 =====\n当前未回复客户数: %d", len(unreplied_customers))

        # 清理过期好友申请缓存
        expired_flags = [flag for flag, ts in processed_friend_requests.items() if now - ts > PROCESSED_FRIEND_REQUESTS_EXPIRE]
        for flag in expired_flags:
            del processed_friend_requests[flag]

        # 清理过期的好友通过时间记录
        expired_approves = [uid for uid, ts in friend_approve_time.items() if now - ts > PROCESSED_FRIEND_REQUESTS_EXPIRE]
        for uid in expired_approves:
            del friend_approve_time[uid]

        # 内部函数：处理通报（立即或延后）
        def handle_notification(notify_type: str, customers: list[tuple[int, CustomerData]], milestone: int | None = None) -> None:
            if is_night_time():
                notif: DelayedNotification = {
                    "type": notify_type,
                    "customers": customers,
                    "milestone": milestone,
                    "timestamp": now,
                }
                delayed_notifications.append(notif)
                log.info(f"夜间模式：{notify_type} 通知已延后，涉及 {len(customers)} 名客户")
            else:
                if notify_type == "new_customers":
                    summary = f"📢 刚刚有 {len(customers)} 名客户发来消息，请及时回复！"
                    asyncio.create_task(send_reminder_with_at(INTERNAL_GROUP_ID, summary, customers))
                elif notify_type == "milestone":
                    if milestone is None:
                        log.error("里程碑通知缺少 milestone 参数，跳过")
                        return
                    unit = f"{milestone}分钟" if milestone < 60 else f"{milestone // 60}小时"
                    summary = f"⚠️ 以下 {len(customers)} 名客户已等待长达 {unit}！"
                    asyncio.create_task(send_reminder_with_at(INTERNAL_GROUP_ID, summary, customers))

        if unreplied_customers:
            # ===== 阶段 1：新客户防抖通报 =====
            new_customers_to_report: list[tuple[int, CustomerData]] = []
            for qq, data in unreplied_customers.items():
                elapsed_min = (now - data["last_active"]) / 60.0
                if not data["is_newly_reported"] and elapsed_min >= 1.0:
                    new_customers_to_report.append((qq, data))
                    data["is_newly_reported"] = True

            if new_customers_to_report:
                new_customers_to_report.sort(key=lambda x: x[1]["last_active"], reverse=True)
                handle_notification("new_customers", new_customers_to_report)
            else:
                log.debug("阶段1: 无新客户需要通报")

            # ===== 阶段 2：迟滞里程碑通报 =====
            milestone_groups: dict[int, list[tuple[int, CustomerData]]] = {m: [] for m in MILESTONES}
            for qq, data in unreplied_customers.items():
                elapsed_min = (now - data["last_active"]) / 60.0
                for m in sorted(MILESTONES, reverse=True):
                    if elapsed_min >= m:
                        if m not in data["reported_milestones"]:
                            data["reported_milestones"].add(m)
                            milestone_groups[m].append((qq, data))
                        break

            for m, delay_list in milestone_groups.items():
                if delay_list:
                    delay_list.sort(key=lambda x: x[1]["last_active"], reverse=True)
                    handle_notification("milestone", delay_list, milestone=m)

            log.info("===== 巡检结束 =====")

        # ===== 夜间汇总发送检查 =====
        summary_time = datetime.strptime(NIGHT_SUMMARY_TIME, "%H:%M").time()
        now_time = datetime.now().time()
        today_str = datetime.now().strftime("%Y%m%d")

        summary_dt = datetime.combine(datetime.today(), summary_time)
        lower_bound = (summary_dt - timedelta(minutes=5)).time()
        upper_bound = (summary_dt + timedelta(minutes=5)).time()

        in_summary_window = False
        if lower_bound <= upper_bound:
            in_summary_window = lower_bound <= now_time <= upper_bound
        else:
            in_summary_window = now_time >= lower_bound or now_time <= upper_bound

        if in_summary_window and last_night_summary_sent_date != today_str:
            if delayed_notifications:
                # 合并所有延后通知中的客户（按 QQ 去重，保留后出现的 data）
                customers_aggregated: dict[int, CustomerData] = {}
                for notif in delayed_notifications:
                    for qq, data in notif["customers"]:
                        customers_aggregated[qq] = data
                if customers_aggregated:
                    customers_list = list(customers_aggregated.items())
                    summary_text = f"🌙 夜间免打扰时段汇总：共有 {len(customers_list)} 名客户发来消息，请及时处理。"
                    # 使用带 @ 和合并转发的提醒
                    asyncio.create_task(send_reminder_with_at(INTERNAL_GROUP_ID, summary_text, customers_list))
                    log.info(f"夜间汇总已发送（带@合并转发）：{summary_text}")
                else:
                    log.debug("夜间汇总时没有有效客户，跳过发送")
                delayed_notifications.clear()
            else:
                log.debug("夜间汇总时间到，但无延后通知")

            import src.config as cfg
            cfg.last_night_summary_sent_date = today_str

        # 定期保存状态（每轮巡检后）
        save_state()
