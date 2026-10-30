"""OneBot v11 接入：通信、消息与标准事件使用 NoneBot 官方适配器。

适配器的生命周期由现有 asyncio 入口管理，不启动另一套框架主循环。
历史消息、合并转发和表情回应直接调用服务端扩展 API。
"""

import asyncio
import logging
from typing import Any, Literal

from nonebot.adapters import Adapter as BaseAdapter
from nonebot.adapters.onebot.v11 import Adapter, Message, MessageSegment
from nonebot.adapters.onebot.v11.config import Config as OneBotConfig
from nonebot.adapters.onebot.v11.event import (
    GroupMessageEvent as BaseGroupMessageEvent,
    LifecycleMetaEvent,
    NoticeEvent,
    PrivateMessageEvent as BasePrivateMessageEvent,
)
from nonebot.config import Config, Env
from nonebot.drivers.aiohttp import Driver
from pydantic import Field

from .messages import message_segments

log = logging.getLogger("Notify-Bridge-Bot")
_STOP = object()

# 协议字段核对基线（2026-10-31）：
# SnowLuma/SnowLuma v2.2.1 @8ffd314260e9e2b6967eeca494d87aa5be046c34，已实测；
# NapNeko/NapCatQQ main @98d781d19815d1625b24d717ce280ecf0a4f6d0c，仅源码核对，待实测。
# 下方 SnowLuma 路径相对 packages/onebot/src/，NapCat 路径相对 packages/napcat-onebot/。


class PrivateMessageEvent(BasePrivateMessageEvent):
    """接收私聊及自身发出的私聊，保留回复对象。"""

    post_type: Literal["message", "message_sent"]
    # 两端均以 user_id 表示发送者、target_id 表示私聊对象：
    # SnowLuma event-converter/to-message.ts::convertPrivateMessage、
    # modules/message-actions.ts::cacheSelfSentMessage；NapCat index.ts::handleMsg。
    # SnowLuma 发给自己的 QQ 回推可缺 target_id（已实测），此时不能猜测为某位客户。
    target_id: int | None = None


class GroupMessageEvent(BaseGroupMessageEvent):
    post_type: Literal["message", "message_sent"]


class GroupMsgEmojiLikeEvent(NoticeEvent):
    """群消息表情回应扩展。"""

    notice_type: Literal["group_msg_emoji_like"]
    group_id: int
    user_id: int
    message_id: int
    likes: list[dict[str, Any]] = Field(default_factory=list)
    is_add: bool = True


def normalize_event(data: dict[str, Any]) -> dict[str, Any]:
    """统一消息格式和不同协议端的事件字段。"""
    result = dict(data)
    # 通用 OneBot 输入处理：统一 ID、数组/CQ 消息和标准字段，不依赖具体协议端。
    for key in ("self_id", "user_id", "group_id", "target_id", "sender_id", "message_id"):
        if result.get(key) is not None:
            result[key] = int(result[key])
    post_type = result.get("post_type")
    if post_type in ("message", "message_sent"):
        result["message"] = message_segments(result.get("message", []))
        if "raw_message" not in result:
            result["raw_message"] = str(Message([
                MessageSegment(**segment) for segment in result["message"]
            ]))
        result.setdefault("font", 0)
        result.setdefault("sub_type", "friend" if result.get("message_type") == "private" else "normal")
        sender = dict(result.get("sender") or {})
        sender.setdefault("user_id", result["user_id"])
        sender.setdefault("nickname", "")
        result["sender"] = sender
    if result.get("notice_type") == "group_msg_emoji_like":
        # SnowLuma event-converter/to-notice.ts::convertGroupMsgEmojiLike 用 sub_type=add/remove；
        # NapCat event/notice/OB11MsgEmojiLikeEvent.ts 直接给 is_add，必须保留其 False 值。
        result.setdefault("is_add", result.get("sub_type", "add") == "add")
    if result.get("notice_type") == "notify" and result.get("sub_type") == "poke":
        if result.get("group_id") is None:
            # 两端私聊戳一戳的 user_id 是会话对象，sender_id 才是实际操作者：
            # SnowLuma event-converter/to-notice.ts::convertFriendPoke；
            # NapCat api/friend.ts::parsePrivatePokeEvent。
            # 转成官方 PokeNotifyEvent 的 user_id；无 sender_id 的输入沿用官方字段语义。
            result["user_id"] = result.get("sender_id") or result.get("user_id", 0)
    return result


def _forward_nodes(messages: Any) -> list[dict[str, Any]]:
    """递归转换转发节点，并省略未设置的可选字段。"""
    # 统一发送 nickname/user_id：SnowLuma modules/message-actions.ts::parseForwardNodes
    # 与 NapCat action/msg/SendMsg.ts::uploadForwardedNodesPacket 均直接接受这组字段。
    result = []
    for segment in message_segments(messages):
        if segment["type"] == "node":
            # SnowLuma assertForwardNodeMetadata 拒绝 source 等字符串元数据为 null，
            # 已实测会返回 1400；NapCat types/message.ts 同样将这些字段声明为可选字符串。
            data = segment["data"] = {
                key: value for key, value in segment["data"].items() if value is not None
            }
            if "content" in data:
                data["content"] = _forward_nodes(data["content"])
        result.append(segment)
    return result


class _BridgeAdapter(Adapter):
    """复用官方连接、重连、鉴权、echo 和 API 实现，事件交给现有业务队列。"""

    def __init__(self, driver: Driver, owner: "OneBotClient"):
        # 不依赖 NoneBot 全局插件配置，允许在现有 asyncio 应用中独立使用。
        BaseAdapter.__init__(self, driver)
        self.owner = owner
        self.onebot_config = OneBotConfig(
            onebot_ws_urls={owner.ws_url},
            onebot_access_token=owner.token or None,
        )
        self.connections = {}
        self.tasks = set()
        self._setup()

    def json_to_event(self, json_data: Any):
        if not isinstance(json_data, dict) or "post_type" not in json_data:
            return super().json_to_event(json_data)
        # 只把建连事件交回官方连接流程，避免框架预处理移除业务所需的 Reply。
        event = (
            super().json_to_event(dict(json_data))
            if json_data.get("meta_event_type") == "lifecycle" else None
        )
        try:
            self.owner._receive(json_data)
        except (TypeError, ValueError, KeyError):
            log.warning("忽略无法解析的 OneBot 事件", exc_info=True)
        return event if isinstance(event, LifecycleMetaEvent) else None

    def bot_connect(self, bot):
        # 连接流程只需消费 lifecycle；客服事件已经按原始顺序进入业务队列。
        bot.handle_event = self._ignore_lifecycle
        super().bot_connect(bot)
        self.owner.self_id = int(bot.self_id)
        self.owner._bot = bot

    async def _ignore_lifecycle(self, event):
        pass

    def bot_disconnect(self, bot):
        self.owner._bot = None
        super().bot_disconnect(bot)


class OneBotClient:
    """将官方适配器嵌入现有异步入口，按顺序分发原始消息事件。"""

    def __init__(self, ws_url: str, token: str = ""):
        self.ws_url = ws_url
        self.token = token
        self.self_id = 0
        self._bot = None
        self._worker: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._queue: asyncio.Queue = asyncio.Queue()

    @property
    def is_running(self) -> bool:
        return self._bot is not None

    def _receive(self, raw: dict[str, Any]) -> None:
        data = normalize_event(raw)
        if data["post_type"] in ("message", "message_sent"):
            model = {
                "private": PrivateMessageEvent,
                "group": GroupMessageEvent,
            }.get(data.get("message_type"))
            event = model.model_validate(data) if model else Adapter.json_to_event(data)
        elif data.get("notice_type") == "group_msg_emoji_like":
            event = GroupMsgEmojiLikeEvent.model_validate(data)
        else:
            event = Adapter.json_to_event(data)
        if event is not None:
            self._queue.put_nowait(event)

    async def _run(self) -> None:
        try:
            driver = Driver(
                Env(_env_file=None),
                Config(_env_file=None, driver="~aiohttp", api_timeout=10.0),
            )
            _BridgeAdapter(driver, self)
            # 将库的生命周期放在同一个任务内，保留现有入口的信号和关停处理。
            async with driver._lifespan:
                await self._stop.wait()
        finally:
            self._bot = None
            self._queue.put_nowait(_STOP)

    def __aiter__(self):
        return self.events()

    async def events(self):
        if self._worker is not None:
            raise RuntimeError("OneBot 事件循环已启动")
        self._stop.clear()
        self._queue = asyncio.Queue()
        worker = self._worker = asyncio.create_task(self._run(), name="onebot-adapter")
        try:
            while True:
                event = await self._queue.get()
                if event is _STOP:
                    await worker
                    break
                yield event
        finally:
            await self.close()

    async def close(self) -> None:
        self._stop.set()
        worker = self._worker
        if worker is not None:
            try:
                await worker
            finally:
                self._worker = None

    async def call_api(self, action: str, **params: Any) -> Any:
        """直接使用官方 API 调用与异常语义，扩展接口同样按 action 调用。"""
        bot = self._bot
        if bot is None:
            raise RuntimeError("OneBot 客户端未连接")
        if action == "send_group_forward_msg":
            params["messages"] = _forward_nodes(params.get("messages", []))
        return await bot.call_api(action, **params)
