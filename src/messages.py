"""消息格式转换与展示名称；CQ 字符串解析复用官方适配器。"""

from collections.abc import Mapping

from nonebot.adapters.onebot.v11 import Message, MessageSegment

_LABELS = {
    "image": "图片", "record": "语音", "video": "视频", "file": "文件",
    "forward": "合并转发", "node": "合并转发", "json": "卡片", "xml": "卡片",
    "music": "音乐", "markdown": "图文消息", "miniapp": "小程序",
    "onlinefile": "在线文件", "flashtransfer": "闪传", "mface": "商城表情",
    "face": "表情", "at": "提及", "reply": "引用消息",
    "dice": "骰子", "rps": "猜拳", "poke": "戳一戳",
}


def message_label(kind: str) -> str:
    return _LABELS.get(kind, "消息")


def message_segments(message, *, tolerant: bool = False) -> list[dict]:
    """统一字符串、字典与 SDK 消息段；容错模式为损坏的历史消息保留占位。"""
    if isinstance(message, str):
        message = Message(message)
    elif isinstance(message, Mapping):
        message = [message]
    if message is None:
        return []
    result = []
    for segment in message:
        try:
            raw = (
                {"type": segment.type, "data": segment.data}
                if isinstance(segment, MessageSegment) else dict(segment)
            )
            if not isinstance(raw.get("type"), str) or not isinstance(raw.get("data"), dict):
                raise ValueError("Invalid message segment")
            result.append({"type": raw["type"], "data": dict(raw["data"])})
        except (TypeError, ValueError):
            if not tolerant:
                raise
            result.append({"type": "text", "data": {"text": "【消息内容暂时无法显示】"}})
    return result
