import time
import asyncio
from datetime import datetime, timedelta
from unicodedata import combining, east_asian_width

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


async def _sample_group(group_id: int, day: str | None) -> tuple[storage.GroupDailyStats, set[int] | None] | None:
    try:
        if day is not None:
            latest = await storage.latest_group_snapshot(group_id)
            if latest and latest[0].day >= day:
                return latest if latest[0].day == day else None

        async with asyncio.timeout(5):
            entries = await cfg.client.call_api("get_group_member_list", group_id=group_id, no_cache=True)
        if not isinstance(entries, list) or not entries:
            raise ValueError("群成员名单为空或格式错误")
        members = set()
        for entry in entries:
            user_id = entry.get("user_id") if isinstance(entry, dict) else None
            if type(user_id) not in (int, str) or int(user_id) <= 0:
                raise ValueError("群成员名单包含无效 QQ 号")
            members.add(int(user_id))
        if day is not None:
            return await storage.record_group_snapshot(group_id, day, members), members
        return await storage.preview_group_snapshot(group_id, members), members
    except Exception as e:
        log.warning("群 %s 人数采样失败: %s", group_id, e)
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


def _format_headcount_table(rows: list[tuple[str, int | None, int | None, int | None, int | None]]) -> str:
    cells = [("", "新增", "删除", "当前数量", "净改变")]
    for name, added, removed, total, change in rows:
        cells.append((name, *[str(value) if value is not None else "–" for value in (added, removed, total)],
                      f"{change:+d}" if change is not None else "–"))

    def width(text: str) -> int:
        return sum(2 if east_asian_width(char) in ("F", "W") else 1 for char in text if not combining(char))

    def pad(size: int) -> str:
        return "　" * (size // 2) + " " * (size % 2)

    widths = [max(map(width, column)) for column in zip(*cells)]
    lines = []
    for name, *values in cells:
        columns = [name + pad(widths[0] - width(name))]
        columns.extend(pad(size - width(value)) + value for size, value in zip(widths[1:], values, strict=True))
        lines.append("　".join(columns))
    return "\n".join(lines)


async def _collect_group_stats(day: str | None = None) -> list[tuple[str, storage.GroupDailyStats | None]]:
    groups = cfg.MONITORED_GROUPS
    cascades = cfg.MONITORED_GROUP_CASCADES
    group_ids = dict.fromkeys(groups.values())
    samples = await asyncio.gather(*(_sample_group(group_id, day) for group_id in group_ids))
    stats = {}
    member_sets = {}
    for group_id, sample in zip(group_ids, samples, strict=True):
        if sample is not None:
            group_stats, members = sample
            stats[group_id] = group_stats
            if members is not None:
                member_sets[group_id] = members

    rows = [(name, stats.get(group_id)) for name, group_id in groups.items()]
    for name, sources in cascades.items():
        result = None
        if all(group_id in member_sets for group_id in sources):
            members = set().union(*(member_sets[group_id] for group_id in sources))
            key = storage.cascade_key(sources)
            try:
                if day is None:
                    result = await storage.preview_group_snapshot(key, members)
                else:
                    recent = await storage.recent_group_stats(key, 1)
                    if not recent or recent[0].day < day:
                        result = await storage.record_group_snapshot(key, day, members)
                    elif recent[0].day == day:
                        result = recent[0]
            except Exception:
                log.exception("级联人数统计失败: %s", name)
        rows.append((name, result))
    return rows


async def _build_headcount_report(
    title: str,
    friends: storage.FriendDailyStats | None,
    group_stats: list[tuple[str, storage.GroupDailyStats | None]],
) -> str | list[MessageSegment]:
    rows: list[tuple[str, int | None, int | None, int | None, int | None]] = [("好友", None, None, None, None)]
    if friends is not None:
        change = friends.added - friends.removed if friends.added is not None and friends.removed is not None else None
        rows[0] = ("好友", friends.added, friends.removed, friends.total, change)
    for name, group in group_stats:
        rows.append((name, group.added, group.removed, group.total, group.change) if group else
                    (name, None, None, None, None))
    message = title + "\n\n" + _format_headcount_table(rows)
    if friends is not None and friends.total >= cfg.FRIEND_COUNT_LIMIT:
        message += f"\n⚠️ 好友数量已达 {friends.total}/{cfg.FRIEND_COUNT_LIMIT} 上限，请及时清理！"
    if any(value is None for row in rows for value in row[1:]):
        message += "\n\n– 表示暂未获取或缺少对比数据。"
    if cfg.MORNING_REPORT_CHART_ENABLED:
        try:
            chart = await build_watch_chart(7)
            if chart is not None:
                return [MessageSegment.text(message + "\n"), chart]
        except Exception:
            log.exception("人数报表图片生成失败，本次发送文字报表")
    return message


async def send_startup_headcount_report() -> None:
    targets = cfg.MORNING_REPORT_GROUP_IDS
    if not cfg.FRIEND_NOTIFICATION_ENABLED or not targets:
        return

    async def preview_friends() -> storage.FriendDailyStats | None:
        try:
            current = await cfg.client.get_friend_ids()
            latest = await storage.latest_friend_snapshot()
            baseline = latest[1] if latest is not None else cfg.friend_summary_baseline
            return storage.FriendDailyStats(
                "", len(current - baseline) if baseline is not None else None,
                len(baseline - current) if baseline is not None else None, len(current),
            )
        except Exception:
            log.exception("启动报表好友采样失败")
            return None

    friends, group_stats = await asyncio.gather(preview_friends(), _collect_group_stats())
    report = await _build_headcount_report("机器人已启动，当前人数如下（与上次采样比较）", friends, group_stats)
    for group_id in targets:
        try:
            await cfg.client.call_api("send_group_msg", group_id=str(group_id), message=report)
        except Exception:
            log.exception("启动人数报表发送失败: group_id=%s", group_id)


async def _update_friend_summary(now: datetime) -> None:
    summary_time = datetime.strptime(cfg.NIGHT_SUMMARY_TIME, "%H:%M").time()
    summary_day = now.date() if now.time() >= summary_time else now.date() - timedelta(days=1)
    summary_date = summary_day.strftime("%Y%m%d")
    group_stats = await _collect_group_stats(summary_date)

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
            report = await _build_headcount_report("早上好！今天也辛苦大家了！", stats, group_stats)
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
