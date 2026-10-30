"""OneBot v11 接入：NoneBot 官方适配器负责通信，复用 NapCat SDK 的业务类型。

适配器的生命周期由现有 asyncio 入口管理，不启动另一套框架主循环。
历史消息、合并转发和表情回应直接调用服务端扩展 API。
"""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from napcat import NapCatClient, NapCatEvent
from napcat.exceptions import NapCatError, NapCatStateError
from nonebot.adapters import Adapter as BaseAdapter
from nonebot.adapters.onebot.v11 import Adapter, Message, MessageSegment
from nonebot.adapters.onebot.v11.config import Config as OneBotConfig
from nonebot.adapters.onebot.v11.event import LifecycleMetaEvent
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.config import Config, Env
from nonebot.drivers.aiohttp import Driver

from .messages import message_segments

log = logging.getLogger("Notify-Bridge-Bot")
_STOP = object()


def normalize_event(data: dict[str, Any]) -> dict[str, Any]:
    """补齐 SDK 的非标准必填字段，不要求协议端伪造 NapCat 扩展。"""
    result = dict(data)
    for key in ("self_id", "user_id", "group_id", "target_id", "sender_id", "message_id"):
        if result.get(key) is not None:
            result[key] = int(result[key])
    post_type = result.get("post_type")
    if post_type in ("message", "message_sent"):
        result["message"] = message_segments(result.get("message", []))
        result.setdefault("message_seq", result["message_id"])
        result.setdefault("real_id", result["message_id"])
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
        if post_type == "message_sent" and result.get("message_type") == "private":
            # 有些实现把 user_id 作为私聊对象；NapCat 业务类型另用 target_id。
            if result.get("target_id") is None and result["user_id"] != result["self_id"]:
                result["target_id"], result["user_id"] = result["user_id"], result["self_id"]
    if result.get("notice_type") == "group_msg_emoji_like":
        result.setdefault("is_add", result.get("sub_type", "add") == "add")
    if result.get("notice_type") == "notify" and result.get("sub_type") == "poke":
        result.setdefault("raw_info", [])
        if result.get("group_id") is None:
            result.setdefault("sender_id", result.get("user_id", 0))
    return result


def _forward_nodes(messages: Any) -> list[dict[str, Any]]:
    """省略未设置的节点字段，并补齐通用实现使用的 name / uin 别名。"""
    result = []
    for segment in message_segments(messages):
        if segment["type"] == "node":
            data = segment["data"] = {
                key: value for key, value in segment["data"].items() if value is not None
            }
            if "content" in data:
                data["name"] = data.get("name") or data.get("nickname") or "未知发送者"
                data["uin"] = data.get("uin") or data.get("user_id") or "0"
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


class OneBotClient(NapCatClient):
    """保留 SDK 的消息构造和 API 方法，通信交由 NoneBot 官方适配器完成。"""

    def __init__(self, ws_url: str, token: str = ""):
        super().__init__(ws_url, token)
        self._bot = None
        self._worker: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._queue: asyncio.Queue = asyncio.Queue()

    @property
    def is_running(self) -> bool:
        return self._bot is not None

    def _receive(self, raw: dict[str, Any]) -> None:
        data = normalize_event(raw)
        event = NapCatEvent.from_dict(data, client=self)
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

    async def events(self, filter_waiters: bool = False):
        if self._worker is not None:
            raise NapCatStateError("OneBot 事件循环已启动")
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

    async def send(self, data: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        bot = self._bot
        if bot is None:
            raise NapCatStateError("OneBot 客户端未连接")
        try:
            result = await bot.call_api(data["action"], **data.get("params", {}), _timeout=timeout)
            return {"status": "ok", "retcode": 0, "data": result}
        except ActionFailed as exc:
            return dict(exc.info)
        except NetworkError as exc:
            raise NapCatError(f"OneBot API 通信失败: {data['action']}") from exc

    async def call_action(self, action: str, params: Mapping[str, Any] | None = None):
        params = dict(params or {})
        if action == "send_group_forward_msg":
            params["messages"] = _forward_nodes(params.get("messages", []))
        return await super().call_action(action, params)
