import time
import asyncio
from datetime import datetime, timedelta

from nonebot.adapters.onebot.v11 import MessageSegment

from . import config as cfg
from .config import (
    log,
    unreplied_customers,
    processed_friend_requests,
    friend_approve_time,
    delayed_notifications,
)
from .models import CustomerData
from .message_sender import send_reminder_with_at, pop_tracked_forward
from .state import save_state
from . import mute
from . import storage
from .watch_chart import build_watch_chart


async def _sample_group(group_id: int, day: str) -> storage.GroupDailyStats | None:
    try:
        recent = await storage.recent_group_stats(group_id, 1)
        if recent and recent[0].day >= day:
            return recent[0] if recent[0].day == day else None

        async with asyncio.timeout(5):
            info = await cfg.client.call_api("get_group_info", group_id=group_id, no_cache=True)
        total = info.get("member_count") if isinstance(info, dict) else None
        if type(total) is not int or total < 0:
            raise ValueError("群人数返回格式错误")
        return await storage.record_group_total(group_id, day, total)
    except Exception as e:
        log.warning("群 %s 人数采样失败，将在下次巡检重试: %s", group_id, e)
        return None


async def _deliver_morning_report(day: str, message: str | list[MessageSegment]) -> bool:
    delivered = await storage.delivered_morning_report_groups(day)
    complete = True
    for group_id in cfg.MORNING_REPORT_GROUP_IDS:
        if group_id in delivered:
            continue
        try:
            await cfg.client.call_api("send_group_msg", group_id=str(group_id), message=message)
            await storage.record_morning_report_delivery(day, group_id)
            log.info("晨报已发送至群 %s，汇总日期 %s", group_id, day)
        except Exception as e:
            log.warning("群 %s 晨报发送未完成，将在下次巡检重试: %s", group_id, e)
            complete = False
    return complete


async def _update_friend_summary(now: datetime) -> None:
    summary_time = datetime.strptime(cfg.NIGHT_SUMMARY_TIME, "%H:%M").time()
    summary_day = now.date() if now.time() >= summary_time else now.date() - timedelta(days=1)
    summary_date = summary_day.strftime("%Y%m%d")
    group_ids = cfg.MONITORED_GROUP_IDS
    group_stats = await asyncio.gather(*(_sample_group(group_id, summary_date) for group_id in group_ids))

    try:
        latest = await storage.latest_friend_snapshot()
        if latest is None:
            baseline = cfg.friend_summary_baseline
            if baseline is not None and summary_date <= cfg.last_friend_summary_date:
                return
        else:
            stats, baseline, pending = latest
            if summary_date < stats.day:
                return

        if latest is None or summary_date > stats.day:
            current = await cfg.client.get_friend_ids()
            stats = storage.FriendDailyStats(
                summary_date,
                len(current - baseline) if baseline is not None else None,
                len(baseline - current) if baseline is not None else None,
                len(current),
            )
            pending = baseline is not None and cfg.FRIEND_NOTIFICATION_ENABLED
            if not await storage.record_friend_snapshot(stats, current, pending):
                return
            cfg.friend_summary_baseline = None
            cfg.last_friend_summary_date = ""

        if not pending:
            return
        if cfg.FRIEND_NOTIFICATION_ENABLED and cfg.MORNING_REPORT_GROUP_IDS:
            message = (
                "早上好！今天也辛苦大家了！\n"
                f"新增好友：{stats.added} 人\n"
                f"减少好友：{stats.removed} 人\n"
                f"当前好友：{stats.total} 人"
            )
            if stats.total >= cfg.FRIEND_COUNT_LIMIT:
                message += f"\n⚠️ 好友数量已达 {stats.total}/{cfg.FRIEND_COUNT_LIMIT} 上限，请及时清理！"
            if group_ids:
                lines = ["", "群聊人数"]
                for group_id, group in zip(group_ids, group_stats, strict=True):
                    if group is None:
                        lines.append(f"{group_id}：暂未获取")
                    elif group.change is None:
                        lines.append(f"{group_id}：{group.total} 人（首次记录）")
                    else:
                        lines.append(f"{group_id}：{group.total} 人（{group.change:+d}）")
                message += "\n" + "\n".join(lines)
            report: str | list[MessageSegment] = message
            if cfg.MORNING_REPORT_CHART_ENABLED:
                try:
                    chart = await build_watch_chart(7)
                    if chart is not None:
                        report = [MessageSegment.text(message + "\n"), chart]
                except Exception:
                    log.exception("晨报图片生成失败，本次发送文字晨报")
            if not await _deliver_morning_report(stats.day, report):
                return
        await storage.mark_friend_summary_sent(stats.day)
    except Exception as e:
        log.warning("好友晨报处理失败，将在下次巡检重试: %s", e)


async def monitor_loop():
    log.info("巡检任务已启动，每 60 秒执行一次")
    last_archive_cleanup = 0.0
    client_was_down = False
    while True:
        await asyncio.sleep(60)

        if not cfg.client.is_running:
            client_was_down = True
            continue

        # 客户端恢复后：静音已解除且不在夜间时，补发积压的延后通知
        if client_was_down:
            client_was_down = False
            if cfg.delayed_notifications and not mute.should_defer_notification():
                try:
                    flushed = await mute.flush_delayed_notifications(reason="client_reconnect")
                    if flushed > 0:
                        log.info("客户端恢复后补发延后通知，涉及 %d 名客户", flushed)
                except Exception as e:
                    log.error("客户端恢复后补发延后通知失败: %s", e, exc_info=True)

        # 检测明文配置是否被远端解密更新
        try:
            cfg.maybe_reload_config_from_disk()
        except Exception as e:
            log.error("巡检配置热重载异常: %s", e, exc_info=True)

        # ===== 临时静音到期自动解除并汇总 =====
        try:
            if mute.expire_if_due():
                await mute.flush_delayed_notifications(reason="expire")
        except Exception as e:
            log.error("静音到期处理失败: %s", e, exc_info=True)

        # ===== 清理超时的监听消息 =====
        now = time.time()
        to_remove = [mid for mid, data in cfg.monitored_forwards.items() if now - data["created_at"] > cfg.MAX_LISTEN_AGE]
        for mid in to_remove:
            pop_tracked_forward(mid)
        if to_remove:
            log.debug("已清理 %d 条过期监听消息", len(to_remove))

        # 清理过期的可撤回发送记录（窗口 + 宽限期，避免延迟点击被静默忽略）
        expired_recalls = [
            mid for mid, data in cfg.recallable_sends.items()
            if now - data["sent_at"] > cfg.RECALL_WINDOW_SECONDS + cfg.RECALL_CLEANUP_GRACE_SECONDS
        ]
        for mid in expired_recalls:
            cfg.recallable_sends.pop(mid, None)
        if expired_recalls:
            log.debug("已清理 %d 条过期可撤回记录", len(expired_recalls))

        # ===== 清理超出保留期的会话周期（每小时一次） =====
        if cfg.ARCHIVE_RETENTION_DAYS > 0 and now - last_archive_cleanup > 3600:
            try:
                removed = await storage.cleanup_expired(cfg.ARCHIVE_RETENTION_DAYS, now)
                if removed:
                    log.info("已清理 %d 个超出保留期(%d天)的会话周期", removed, cfg.ARCHIVE_RETENTION_DAYS)
            except Exception as e:
                log.error("会话周期过期清理失败: %s", e, exc_info=True)
            last_archive_cleanup = now

        # 清理过期好友申请缓存（经 cfg 读取，保证热重载后生效）
        expired_flags = [flag for flag, ts in processed_friend_requests.items() if now - ts > cfg.PROCESSED_FRIEND_REQUESTS_EXPIRE]
        for flag in expired_flags:
            del processed_friend_requests[flag]

        # 清理过期的好友通过时间记录
        expired_approves = [uid for uid, ts in friend_approve_time.items() if now - ts > cfg.PROCESSED_FRIEND_REQUESTS_EXPIRE]
        for uid in expired_approves:
            del friend_approve_time[uid]

        def handle_notification(notify_type: str, customers: list[tuple[int, CustomerData]], milestone: int | None = None) -> None:
            if mute.should_defer_notification():
                mute.queue_delayed_notification(notify_type, customers, milestone=milestone)
            else:
                if notify_type == "new_customers":
                    summary = f"📢 刚刚有 {len(customers)} 名客户发来消息，请及时回复！"
                    asyncio.create_task(send_reminder_with_at(cfg.INTERNAL_GROUP_ID, summary, customers, notice_type=notify_type))
                elif notify_type == "milestone":
                    if milestone is None:
                        log.error("里程碑通知缺少 milestone 参数，跳过")
                        return
                    unit = f"{milestone}分钟" if milestone < 60 else f"{milestone // 60}小时"
                    summary = f"⚠️ 以下 {len(customers)} 名客户已等待长达 {unit}！"
                    asyncio.create_task(send_reminder_with_at(cfg.INTERNAL_GROUP_ID, summary, customers, notice_type=notify_type, milestone=milestone))

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

            # ===== 阶段 2：迟滞里程碑通报 =====
            milestone_groups: dict[int, list[tuple[int, CustomerData]]] = {m: [] for m in cfg.MILESTONES}
            for qq, data in unreplied_customers.items():
                elapsed_min = (now - data["last_active"]) / 60.0
                for m in sorted(cfg.MILESTONES, reverse=True):
                    if elapsed_min >= m:
                        if m not in data["reported_milestones"]:
                            data["reported_milestones"].add(m)
                            milestone_groups[m].append((qq, data))
                        break

            for m, delay_list in milestone_groups.items():
                if delay_list:
                    delay_list.sort(key=lambda x: x[1]["last_active"], reverse=True)
                    handle_notification("milestone", delay_list, milestone=m)

        # ===== 夜间汇总发送检查 =====
        summary_time = datetime.strptime(cfg.NIGHT_SUMMARY_TIME, "%H:%M").time()
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

        if in_summary_window and cfg.last_night_summary_sent_date != today_str:
            if delayed_notifications:
                customers_aggregated: dict[int, CustomerData] = {}
                for notif in delayed_notifications:
                    for qq, data in notif["customers"]:
                        customers_aggregated[qq] = data
                if customers_aggregated:
                    customers_list = list(customers_aggregated.items())
                    summary_text = f"🌙 夜间免打扰时段汇总：共有 {len(customers_list)} 名客户发来消息，请及时处理。"

                    night_start_t = datetime.strptime(cfg.NIGHT_START, "%H:%M").time()
                    night_summary_t = datetime.strptime(cfg.NIGHT_SUMMARY_TIME, "%H:%M").time()
                    night_start_dt = datetime.combine(datetime.today(), night_start_t)
                    night_summary_dt = datetime.combine(datetime.today(), night_summary_t)
                    if night_summary_dt <= night_start_dt:
                        night_summary_dt += timedelta(days=1)
                    night_max_age = int((night_summary_dt - night_start_dt).total_seconds()) + 300

                    asyncio.create_task(send_reminder_with_at(cfg.INTERNAL_GROUP_ID, summary_text, customers_list, max_age_seconds=night_max_age, notice_type="night_summary"))
                    log.info("夜间汇总已发送：%s", summary_text)
                delayed_notifications.clear()

            cfg.last_night_summary_sent_date = today_str

        await _update_friend_summary(datetime.now())
        save_state()
