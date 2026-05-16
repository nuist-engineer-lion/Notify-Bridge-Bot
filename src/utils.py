import asyncio
import random
from datetime import datetime
from typing import Iterable, Union

from napcat import Message, Text, UnknownMessageSegment

from .config import (
    log,
    AVAILABILITY,
    WEEKDAY_MAP,
    NIGHT_START,
    NIGHT_END,
    RECENT_MESSAGE_MAX_AGE,
    client,
)


def format_duration(seconds: float) -> str:
    """将秒数转换为易读的文本"""
    if seconds < 60:
        return f"{int(seconds)}秒"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}分{secs:02d}秒"
    elif seconds < 86400:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}小时{mins:02d}分"
    else:
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        return f"{days}天{hours}小时"


def serialize_message_segments(segments: list[Message]) -> list[dict]:
    """将 SDK 消息段序列化为 NodeInline 所需的原始 OB11 结构。"""
    return [dict(segment) for segment in segments]


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


async def get_nicknames_batch(user_ids: list[int], delay: float = 0.2) -> dict[int, str]:
    """
    批量获取用户昵称，返回 {qq: nickname} 映射。
    逐次调用 get_stranger_info 并加入延迟以避免限流。
    """
    nicknames: dict[int, str] = {}
    for uid in user_ids:
        try:
            info = await client.get_stranger_info(user_id=str(uid))
            nickname = info.get("nickname", "未知昵称")
            nicknames[uid] = str(nickname)
        except Exception as e:
            log.error("获取用户 %d 昵称失败: %s", uid, e)
            nicknames[uid] = "获取失败"
        await asyncio.sleep(delay)
    return nicknames


async def get_recent_message_ids(user_id: int, count: int = 200, max_age_seconds: int = RECENT_MESSAGE_MAX_AGE) -> list[int]:
    """
    通过 API 获取与指定用户的最近消息，筛选出最近 max_age_seconds 秒内的消息（双方消息）。
    返回按时间正序排列的 message_id 列表。
    """
    import time
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
        # 按时间正序排序
        messages.sort(key=lambda m: m.get("time", 0))
        # 筛选最近 max_age_seconds 秒内的消息
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


def get_current_available_members() -> list[int]:
    """根据当前系统时间，返回所有可用的成员QQ列表"""
    now = datetime.now()
    weekday_name = WEEKDAY_MAP[now.weekday()]
    current_time = now.time()

    available: list[int] = []
    for qq, schedule in AVAILABILITY.items():
        time_slots = schedule.get(weekday_name, [])
        for start_str, end_str in time_slots:
            try:
                start = datetime.strptime(start_str, "%H:%M").time()
                end = datetime.strptime(end_str, "%H:%M").time()
                if start <= current_time <= end:
                    available.append(qq)
                    break
            except Exception as e:
                log.error("解析时间段失败: qq=%s, slot=%s-%s, err=%s", qq, start_str, end_str, e)
                continue
    return available


def is_night_time() -> bool:
    """判断当前时间是否处于夜间免打扰时段"""
    now = datetime.now().time()
    start = datetime.strptime(NIGHT_START, "%H:%M").time()
    end = datetime.strptime(NIGHT_END, "%H:%M").time()

    if start <= end:
        return start <= now <= end
    else:
        return now >= start or now <= end
