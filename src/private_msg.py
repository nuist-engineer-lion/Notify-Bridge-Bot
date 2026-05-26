import time
from typing import Iterable, Union

from napcat import (
    PrivateMessageEvent,
    FriendPokeEvent,
    Text,
    Message,
    UnknownMessageSegment,
)

from .config import (
    log,
    WHITELIST,
    PROCESSED_FRIEND_REQUESTS_EXPIRE,
    friend_approve_time,
    unreplied_customers,
    client,
)
from .state import save_state
from .message_sender import close_session


def extract_message_text(segments: Iterable[Union[Message, UnknownMessageSegment]]) -> str:
    """
    从消息段可迭代对象中提取纯文本，用于日志记录。
    只提取 Text 段中的文本，忽略其他类型。
    """
    texts: list[str] = []
    for seg in segments:
        if isinstance(seg, Text):
            texts.append(seg.text)
    full = ''.join(texts).strip()
    if len(full) > 100:
        full = full[:100] + '…'
    return full or '<无文本内容>'


async def handle_private_msg(event: PrivateMessageEvent) -> bool:
    """处理客户发来的私聊消息：加入/更新待回复队列"""
    uid = int(event.user_id)

    if uid in WHITELIST:
        return False

    now = time.time()

    # 检查用户是否刚通过好友申请（窗口期内忽略其消息）
    if uid in friend_approve_time and (now - friend_approve_time[uid]) < PROCESSED_FRIEND_REQUESTS_EXPIRE:
        log.info("忽略刚通过好友申请的用户 %d 的消息（窗口期 %d 秒），不加入队列",
                 uid, PROCESSED_FRIEND_REQUESTS_EXPIRE)
        return True

    if uid not in unreplied_customers:
        msg_text = extract_message_text(event.message)
        unreplied_customers[uid] = {
            "last_active": now,
            "msg_ids": [event.message_id],
            "is_newly_reported": False,
            "reported_milestones": set(),
            "pending_since": now,
        }
        log.info("新增客户 %d 进入待回复队列 (msg_id=%s, 内容: %s)。当前队列长度: %d",
                 uid, event.message_id, msg_text, len(unreplied_customers))
    else:
        unreplied_customers[uid]["last_active"] = now
        unreplied_customers[uid]["msg_ids"].append(event.message_id)
        unreplied_customers[uid]["is_newly_reported"] = False
        unreplied_customers[uid]["reported_milestones"].clear()
        log.info("客户 %d 追加消息 (msg_id=%s)，累计 %d 条，重置通报倒计时。",
                 uid, event.message_id, len(unreplied_customers[uid]["msg_ids"]))

    save_state()
    return True


async def handle_sent_msg(event: PrivateMessageEvent) -> bool:
    """处理客服发出的私聊消息：若目标在队列中则结束会话"""
    tid = event.target_id
    if tid in unreplied_customers:
        unreplied_customers[tid]["msg_ids"].append(event.message_id)
        await close_session(tid, send_closing=False)
        log.info("客服已回复 %s，移除提醒并记录耗时。剩余未回复: %d", tid, len(unreplied_customers))
        return True
    return False


async def handle_friend_poke(event: FriendPokeEvent) -> bool:
    """处理私聊戳一戳：自动发送结束语并从队列移除"""
    sid = event.sender_id
    if event.target_id == client.self_id and sid not in WHITELIST:
        closed = await close_session(sid, send_closing=True)
        if closed:
            log.info("私聊戳一戳结束会话: user_id=%s", sid)
        else:
            log.info("私聊戳一戳自动发送结束语（客户不在队列）: user_id=%s", sid)
        return True
    return False
