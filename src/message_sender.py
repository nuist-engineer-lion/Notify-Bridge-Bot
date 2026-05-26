import time
import asyncio
import random
import statistics

from napcat import (
    Message,
    NodeInline,
    NodeReference,
    Text,
    At,
    Reply,
)

from .config import (
    log,
    STARTED_AT,
    INTERNAL_GROUP_ID,
    CLOSING_MESSAGE,
    MONITORED_FORWARD_LIMIT,
    EMOJI_MAPPING,
    RECENT_MESSAGE_MAX_AGE,
    reply_durations,
    unreplied_customers,
    monitored_forwards,
    monitored_forward_order,
    last_command_time,
    client,
)
from .models import CustomerData
from .utils import format_duration, get_current_available_members
from .state import save_state, archive_session


# ======================= 消息序列化 =======================

def serialize_message_segments(segments: list[Message]) -> list[dict]:
    """将 SDK 消息段序列化为 NodeInline 所需的原始 OB11 结构。"""
    return [dict(segment) for segment in segments]


# ======================= 历史消息获取 =======================

async def get_recent_message_ids(user_id: int, count: int = 200, max_age_seconds: int = RECENT_MESSAGE_MAX_AGE) -> list[int]:
    """
    通过 API 获取与指定用户的最近消息，筛选出最近 max_age_seconds 秒内的消息（双方消息）。
    返回按时间正序排列的 message_id 列表。
    """
    now = time.time()
    try:
        resp = await client.get_friend_msg_history(
            user_id=str(user_id),
            count=count,
            parse_mult_msg=True,
        )
        messages = resp.get("messages", [])
        if not messages:
            return []
        messages.sort(key=lambda m: m.get("time", 0))
        filtered = [
            m for m in messages
            if (now - m.get("time", 0)) <= max_age_seconds
        ]
        msg_ids = [m["message_id"] for m in filtered if "message_id" in m]
        log.debug("客户 %d 历史消息: 获取到 %d 条，过滤后 %d 条（%d 秒内）",
                  user_id, len(messages), len(msg_ids), max_age_seconds)
        return msg_ids
    except Exception as e:
        log.error("获取客户 %d 历史消息失败: %s", user_id, e, exc_info=True)
        return []


# ======================= 合并转发监听管理 =======================

def track_forward_message(message_id: int, customer_ids: list[int], group_id: int) -> None:
    if message_id in monitored_forwards:
        try:
            monitored_forward_order.remove(message_id)
        except ValueError:
            pass

    monitored_forward_order.append(message_id)
    monitored_forwards[message_id] = {
        "customer_ids": customer_ids,
        "group_id": group_id,
        "created_at": time.time(),
    }

    while len(monitored_forward_order) > MONITORED_FORWARD_LIMIT:
        expired_id = monitored_forward_order.popleft()
        monitored_forwards.pop(expired_id, None)

    log.debug("监听合并转发已更新: message_id=%s, 当前监听数=%d", message_id, len(monitored_forward_order))
    save_state()


def pop_tracked_forward(message_id: int) -> dict | None:
    """移除监听的合并转发，并清理其防抖记录"""
    data = monitored_forwards.pop(message_id, None)
    if data is None:
        return None

    try:
        monitored_forward_order.remove(message_id)
    except ValueError:
        pass

    keys_to_remove = [key for key in last_command_time if key[0] == message_id]
    for key in keys_to_remove:
        del last_command_time[key]

    save_state()
    return data


# ======================= 合并转发构造 =======================

async def send_nested_forward(group_id: int, customer_list: list[tuple[int, CustomerData]], summary_text: str, max_age_seconds: int | None = None) -> int | None:
    """
    构造嵌套合并转发并发送，每个客户的消息通过 get_recent_message_ids 获取最近指定时间内的消息
    """
    if max_age_seconds is None:
        max_age_seconds = RECENT_MESSAGE_MAX_AGE

    if not customer_list:
        log.debug("send_nested_forward: customer_list 为空，跳过发送")
        return None

    log.info("开始构造合并转发 -> 群 %d, 共 %d 名客户", group_id, len(customer_list))
    outer_nodes: list[Message] = [
        NodeInline(
            nickname="快捷传送门",
            user_id=str(client.self_id),
            content=serialize_message_segments([Text(text="https://lion-qq.laysath.cn")]),
        ),
    ]

    for qq, _ in customer_list:
        msg_ids = await get_recent_message_ids(qq, count=200, max_age_seconds=max_age_seconds)
        if not msg_ids:
            log.warning("客户 %d 无%d秒内消息，跳过该客户节点", qq, max_age_seconds)
            continue

        inner_nodes: list[Message] = [
            NodeReference(id=str(msg_id))
            for msg_id in msg_ids
        ]

        outer_nodes.append(
            NodeInline(
                nickname="客户",
                user_id=str(qq),
                content=serialize_message_segments(inner_nodes),
            )
        )

    if len(outer_nodes) <= 1:
        log.warning("所有客户均无%d秒内消息，取消发送合并转发", max_age_seconds)
        return None

    try:
        response = await client.send_group_forward_msg(
            group_id=str(group_id),
            messages=outer_nodes,
        )
        message_id = response["message_id"]
        log.info("合并转发发送成功 -> 群 %d, message_id=%s", group_id, message_id)
        asyncio.create_task(add_emoji_to_message(message_id, list(EMOJI_MAPPING.values())))
        return message_id
    except Exception as e:
        log.error("发送合并转发失败: %s", e, exc_info=True)
        return None


async def send_forward_from_message_ids(group_id: int, title: str, user_id: int, message_ids: list[int]) -> int | None:
    """根据消息ID列表发送合并转发到群"""
    if not message_ids:
        return None
    outer_nodes: list[Message] = [
        NodeInline(
            nickname="客服系统提示",
            user_id=str(client.self_id),
            content=serialize_message_segments([Text(text=title)]),
        ),
        NodeInline(
            nickname="消息记录",
            user_id=str(user_id),
            content=serialize_message_segments([
                NodeReference(id=str(msg_id)) for msg_id in message_ids
            ]),
        )
    ]
    try:
        response = await client.send_group_forward_msg(
            group_id=str(group_id),
            messages=outer_nodes,
        )
        return response["message_id"]
    except Exception as e:
        log.error("发送合并转发失败: %s", e, exc_info=True)
        return None


async def add_emoji_to_message(message_id: int, emoji_ids: list[int]) -> None:
    """为指定消息添加多个表情贴纸"""
    for emoji_id in emoji_ids:
        try:
            await client.set_msg_emoji_like(message_id=str(message_id), emoji_id=str(emoji_id))
            log.debug("已为消息 %s 添加表情 %s", message_id, emoji_id)
        except Exception as e:
            log.error("添加表情失败: message_id=%s, emoji_id=%s, err=%s", message_id, emoji_id, e)


async def send_reminder_with_at(group_id: int, summary: str, customer_list: list[tuple[int, CustomerData]], max_age_seconds: int | None = None) -> int | None:
    """
    发送提醒：先尝试 @ 一位当前可用成员，再发送合并转发。
    若无可用成员，则直接发送合并转发（无 @）。
    """
    available = get_current_available_members()
    if available:
        chosen = random.choice(available)
        at_msg: list[Message] = [
            At(qq=str(chosen)),
            Text(text=f" {summary}"),
        ]
        try:
            await client.send_group_msg(group_id=str(group_id), message=at_msg)
            log.info("已发送 @%d 提醒: %s", chosen, summary)
        except Exception as e:
            log.error("发送 @ 提醒失败: %s", e, exc_info=True)
    else:
        log.debug("当前无可用成员，直接发送合并转发")

    message_id = await send_nested_forward(group_id, customer_list, summary, max_age_seconds=max_age_seconds)
    if message_id is not None:
        track_forward_message(
            message_id=message_id,
            customer_ids=[qq for qq, _ in customer_list],
            group_id=group_id,
        )
    return message_id


async def send_and_track_feedback(gid: int, reply_id: int, feedback: str, customer_id: int) -> None:
    """发送带引用的反馈消息，并将其加入监听列表"""
    try:
        resp = await client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(reply_id)), Text(text=feedback)],
        )
        feedback_msg_id = resp.get("message_id")
        if feedback_msg_id:
            track_forward_message(feedback_msg_id, [customer_id], gid)
    except Exception as e:
        log.error("发送反馈消息失败: %s", e)


# ======================= 会话管理 =======================

async def close_session(user_id: int, send_closing: bool = False) -> bool:
    """
    从 unreplied_customers 中移除客户，记录耗时（如果存在）。
    如果 send_closing=True，则向该客户发送结束语。
    返回是否成功结束（即客户原本在队列中）。
    """
    data = unreplied_customers.pop(user_id, None)
    if data is None:
        return False

    pending = data["pending_since"]
    elapsed = time.time() - pending
    reply_durations.append(elapsed)
    log.info("会话结束: user_id=%s, 耗时=%.1f秒", user_id, elapsed)

    asyncio.create_task(archive_session(user_id, pending))

    if send_closing:
        try:
            await client.send_private_msg(
                user_id=str(user_id),
                message=CLOSING_MESSAGE,
            )
            log.info("自动发送结束语成功: user_id=%s", user_id)
        except Exception as e:
            log.error("自动发送结束语失败: user_id=%s, err=%s", user_id, e, exc_info=True)

    save_state()
    return True


async def send_status_panel(group_id: int):
    """向指定群发送机器人状态统计面板"""
    uptime = time.time() - STARTED_AT
    uptime_str = format_duration(uptime)

    pending_count = len(unreplied_customers)
    total_replies = len(reply_durations)

    if total_replies > 0:
        min_str = format_duration(min(reply_durations))
        max_str = format_duration(max(reply_durations))
        avg_str = format_duration(statistics.mean(reply_durations))
        median_str = format_duration(statistics.median(reply_durations))
    else:
        min_str = max_str = avg_str = median_str = "暂无数据"

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
