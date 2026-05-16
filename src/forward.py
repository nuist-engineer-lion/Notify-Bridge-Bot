import time
import asyncio
import random

from napcat import (
    Message,
    NodeInline,
    NodeReference,
    Text,
    At,
)

from .config import (
    log,
    MONITORED_FORWARD_LIMIT,
    INTERNAL_GROUP_ID,
    EMOJI_MAPPING,
    RECENT_MESSAGE_MAX_AGE,
    monitored_forwards,
    monitored_forward_order,
    last_command_time,
    client,
)
from .models import CustomerData
from .utils import serialize_message_segments, get_recent_message_ids, get_current_available_members
from .state import save_state


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
    save_state()  # 状态变更后持久化


def pop_tracked_forward(message_id: int) -> dict | None:
    """移除监听的合并转发，并清理其防抖记录"""
    data = monitored_forwards.pop(message_id, None)
    if data is None:
        return None

    try:
        monitored_forward_order.remove(message_id)
    except ValueError:
        pass

    # 清理该消息相关的所有防抖记录
    keys_to_remove = [key for key in last_command_time if key[0] == message_id]
    for key in keys_to_remove:
        del last_command_time[key]

    save_state()  # 状态变更后持久化
    return data


async def send_nested_forward(group_id: int, customer_list: list[tuple[int, CustomerData]], summary_text: str) -> int | None:
    """
    构造嵌套合并转发并发送，每个客户的消息通过 get_recent_message_ids 获取最近指定时间内的消息
    """
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
        # 获取最近指定时间内的消息ID
        msg_ids = await get_recent_message_ids(qq, count=200, max_age_seconds=RECENT_MESSAGE_MAX_AGE)
        if not msg_ids:
            log.warning("客户 %d 无%d秒内消息，跳过该客户节点", qq, RECENT_MESSAGE_MAX_AGE)
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
        log.warning("所有客户均无%d秒内消息，取消发送合并转发", RECENT_MESSAGE_MAX_AGE)
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


async def send_reminder_with_at(group_id: int, summary: str, customer_list: list[tuple[int, CustomerData]]) -> int | None:
    """
    发送提醒：先尝试 @ 一位当前可用成员，再发送合并转发。
    若无可用成员，则直接发送合并转发（无 @）。
    """
    available = get_current_available_members()
    if available:
        chosen = random.choice(available)
        # 构造 @ 消息，使用库提供的消息段类
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

    # 发送合并转发
    message_id = await send_nested_forward(group_id, customer_list, summary)
    if message_id is not None:
        track_forward_message(
            message_id=message_id,
            customer_ids=[qq for qq, _ in customer_list],
            group_id=group_id,
        )
    return message_id
