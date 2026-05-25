import time
import asyncio
from datetime import datetime, timedelta

from .config import (
    log,
    INTERNAL_GROUP_ID,
    MILESTONES,
    MAX_LISTEN_AGE,
    NIGHT_START,
    NIGHT_SUMMARY_TIME,
    PROCESSED_FRIEND_REQUESTS_EXPIRE,
    unreplied_customers,
    monitored_forwards,
    processed_friend_requests,
    friend_approve_time,
    delayed_notifications,
    last_night_summary_sent_date,
    client,
)
from .models import CustomerData, DelayedNotification
from .utils import is_night_time
from .message_sender import send_reminder_with_at, pop_tracked_forward
from .state import save_state


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
                customers_aggregated: dict[int, CustomerData] = {}
                for notif in delayed_notifications:
                    for qq, data in notif["customers"]:
                        customers_aggregated[qq] = data
                if customers_aggregated:
                    customers_list = list(customers_aggregated.items())
                    summary_text = f"🌙 夜间免打扰时段汇总：共有 {len(customers_list)} 名客户发来消息，请及时处理。"

                    # 计算覆盖整个夜间窗口所需的 max_age_seconds
                    night_start_t = datetime.strptime(NIGHT_START, "%H:%M").time()
                    night_summary_t = datetime.strptime(NIGHT_SUMMARY_TIME, "%H:%M").time()
                    night_start_dt = datetime.combine(datetime.today(), night_start_t)
                    night_summary_dt = datetime.combine(datetime.today(), night_summary_t)
                    if night_summary_dt <= night_start_dt:
                        night_summary_dt += timedelta(days=1)
                    night_max_age = int((night_summary_dt - night_start_dt).total_seconds()) + 300  # +5分钟缓冲

                    asyncio.create_task(send_reminder_with_at(INTERNAL_GROUP_ID, summary_text, customers_list, max_age_seconds=night_max_age))
                    log.info(f"夜间汇总已发送（带@合并转发）：{summary_text}")
                else:
                    log.debug("夜间汇总时没有有效客户，跳过发送")
                delayed_notifications.clear()
            else:
                log.debug("夜间汇总时间到，但无延后通知")

            import src.config as cfg
            cfg.last_night_summary_sent_date = today_str

            # 夜间模式结束后刷新好友人数
            try:
                friend_list = await client.send(
                    {"action": "get_friend_list", "params": {}},
                    timeout=30.0,
                )
                if friend_list.get("status") == "ok" and friend_list.get("retcode") == 0:
                    cfg.friend_count = len(friend_list.get("data", []))
                    log.info("夜间模式结束后刷新好友人数: %d", cfg.friend_count)
            except Exception as e:
                log.warning("夜间模式结束后刷新好友人数失败: %s", e)

        save_state()
