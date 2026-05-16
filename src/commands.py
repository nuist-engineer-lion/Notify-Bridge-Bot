import time
import asyncio

from napcat import (
    Message,
    Reply,
    Text,
)

from .config import (
    log,
    CLOSING_MESSAGE,
    DEBOUNCE_SECONDS,
    EMOJI_MAPPING,
    monitored_forwards,
    last_command_time,
    client,
)
from .forward import (
    track_forward_message,
    send_forward_from_message_ids,
    add_emoji_to_message,
)
from .monitor import close_session


async def resolve_target_from_reply(reply_id: int) -> tuple[list[int] | None, str | None]:
    """
    根据被引用的消息ID解析对应的客户列表。
    返回 (customer_ids, error_message)，若成功则 error_message 为 None。
    """
    # 先从监听的合并转发中查找
    data = monitored_forwards.get(reply_id)
    if data is not None:
        return data["customer_ids"], None

    # 未找到，尝试查询消息详情
    try:
        msg_detail = await client.get_msg(message_id=str(reply_id))
        sender_id = int(msg_detail.get("user_id", 0))
        self_id = int(client.self_id)
        if sender_id == self_id:
            return None, "操作已过期"
        else:
            return None, "无法识别的消息"
    except Exception as e:
        log.error("查询消息详情失败: reply_id=%s, err=%s", reply_id, e)
        return None, "查询消息失败"


async def handle_bye_command(gid: int, reply_id: int, customer_id: int) -> str:
    """执行 .bye 命令：发送结束语并关闭会话，返回反馈文本"""
    try:
        await client.send_private_msg(
            user_id=str(customer_id),
            message=CLOSING_MESSAGE,
        )
        closed = await close_session(customer_id, send_closing=False)
        return f"✅ 已向客户 {customer_id} 发送结束语。" + ("（客户已在待回复队列）" if closed else "（客户不在待回复队列）")
    except Exception as e:
        log.error("发送结束语失败: customer=%s, err=%s", customer_id, e, exc_info=True)
        return f"❌ 发送失败：{e}"


async def handle_close_command(gid: int, reply_id: int, customer_id: int) -> str:
    """执行 .close 命令：仅关闭会话，不发送结束语，返回反馈文本"""
    try:
        closed = await close_session(customer_id, send_closing=False)
        return f"✅ 已关闭客户 {customer_id} 的会话（未发送结束语）。" + ("（客户已在待回复队列）" if closed else "（客户不在待回复队列）")
    except Exception as e:
        log.error("关闭会话失败: customer=%s, err=%s", customer_id, e, exc_info=True)
        return f"❌ 关闭失败：{e}"


async def handle_more_command(gid: int, reply_id: int, customer_id: int) -> tuple[bool, str | None, int | None]:
    """
    执行 .more 命令：获取历史消息并发送合并转发。
    返回 (成功标志, 反馈文本或 None, 新合并转发的消息ID或 None)
    """
    try:
        resp = await client.get_friend_msg_history(
            user_id=str(customer_id),
            count=100,
            parse_mult_msg=True,
        )
        messages = resp.get("messages", [])
        if not messages:
            return True, f"客户 {customer_id} 暂无更多历史消息", None

        messages.sort(key=lambda m: m.get("time", 0))
        msg_ids = [m["message_id"] for m in messages if "message_id" in m]
        title = f"客户 {customer_id} 的最近 {len(msg_ids)} 条消息"
        new_fwd_id = await send_forward_from_message_ids(
            group_id=gid,
            title=title,
            user_id=customer_id,
            message_ids=msg_ids,
        )
        if new_fwd_id:
            return True, None, new_fwd_id
        else:
            return False, f"❌ 构造合并转发失败", None
    except Exception as e:
        log.error("获取历史消息失败: customer=%s, err=%s", customer_id, e, exc_info=True)
        return False, f"❌ 获取历史消息失败：{e}", None


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
