"""评审问题的回归验证；业务模块使用隔离配置，不读取本地配置或连接 QQ。"""

import asyncio
import importlib.util
import inspect
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.adapters.onebot.v11.event import (
    FriendAddNoticeEvent,
    FriendRequestEvent,
    LifecycleMetaEvent,
    PokeNotifyEvent,
)

import src
from src.onebot import OneBotClient, PrivateMessageEvent, _BridgeAdapter, _EventBuffer


ROOT = Path(__file__).resolve().parents[1]
BASE = {"time": 100, "self_id": 10001, "user_id": 20002}


def private_message(message_id=123, **extra):
    return dict(BASE, post_type="message", message_type="private",
                message_id=message_id, message="hello", **extra)


def load_module(name):
    spec = importlib.util.spec_from_file_location(f"src._review_{name}", ROOT / "src" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EventTests(unittest.IsolatedAsyncioTestCase):
    async def test_standard_events_use_the_official_classmethod(self):
        self.assertIsInstance(inspect.getattr_static(Adapter, "json_to_event"), classmethod)
        client = OneBotClient("ws://127.0.0.1:6700")
        self.addCleanup(client._queue.close)
        bridge = object.__new__(_BridgeAdapter)
        bridge.owner = client
        cases = [
            (dict(BASE, post_type="request", request_type="friend", comment="", flag="f"), FriendRequestEvent),
            (dict(BASE, post_type="notice", notice_type="friend_add"), FriendAddNoticeEvent),
            (dict(BASE, post_type="notice", notice_type="notify", sub_type="poke", target_id=10001), PokeNotifyEvent),
        ]
        for raw, expected_type in cases:
            bridge.json_to_event(raw)
            self.assertIsInstance(client._parse_event(await client._queue.get()), expected_type)
        unknown = dict(BASE, post_type="message", message_type="other", message_id=42, message="hello")
        bridge.json_to_event(unknown)
        self.assertEqual(client._parse_event(await client._queue.get()).message_id, 42)

    async def test_overflow_preserves_order_during_interleaved_reads_and_writes(self):
        buffer = _EventBuffer(maxsize=2)
        self.addCleanup(buffer.close)
        for i in range(20):
            buffer.put_nowait({"i": i, "text": "中文"})
        self.assertEqual(buffer._queue.qsize(), 2)
        self.assertIsNotNone(buffer._overflow)
        self.assertEqual((await buffer.get())["i"], 0)
        # 内存已有空位，但新事件仍须排在所有溢出事件之后。
        buffer.put_nowait({"i": 20})
        self.assertEqual([(await buffer.get())["i"] for _ in range(20)], list(range(1, 21)))
        buffer.put_nowait({"i": 21})
        self.assertEqual((await buffer.get())["i"], 21)

    async def test_shutdown_drains_full_buffer_without_a_stop_sentinel(self):
        buffer = _EventBuffer(maxsize=1)
        self.addCleanup(buffer.close)
        for i in range(4):
            buffer.put_nowait({"i": i})
        buffer.shutdown()
        self.assertEqual([(await buffer.get())["i"] for _ in range(4)], list(range(4)))
        with self.assertRaises(asyncio.QueueShutDown):
            await buffer.get()
        with self.assertRaises(asyncio.QueueShutDown):
            buffer.put_nowait({})
        self.assertIsNone(buffer._overflow)

    async def test_shutdown_wakes_an_idle_consumer(self):
        buffer = _EventBuffer()
        self.addCleanup(buffer.close)
        waiting = asyncio.create_task(buffer.get())
        await asyncio.sleep(0)
        buffer.shutdown()
        with self.assertRaises(asyncio.QueueShutDown):
            await asyncio.wait_for(waiting, 1)

    async def test_close_releases_overflow_database(self):
        buffer = _EventBuffer(maxsize=1)
        buffer.put_nowait({"i": 1})
        buffer.put_nowait({"i": 2})
        connection = buffer._overflow
        buffer.close()
        buffer.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    async def test_heartbeats_are_filtered_but_connect_event_is_retained(self):
        client = OneBotClient("ws://127.0.0.1:6700")
        self.addCleanup(client._queue.close)
        client._receive(dict(BASE, post_type="meta_event", meta_event_type="heartbeat"))
        self.assertTrue(client._queue._queue.empty())
        client._receive(dict(BASE, post_type="meta_event", meta_event_type="lifecycle", sub_type="connect"))
        self.assertIsInstance(client._parse_event(await client._queue.get()), LifecycleMetaEvent)

    async def test_full_queue_does_not_block_websocket_api_responses(self):
        async def websocket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json(dict(BASE, post_type="meta_event", meta_event_type="lifecycle", sub_type="connect"))
            async for frame in ws:
                call = frame.json()
                # 业务正在等待 API；先发送足以填满内存队列的事件，再返回 echo。
                for i in range(1100):
                    await ws.send_json(private_message(i))
                await ws.send_json({"status": "ok", "retcode": 0, "data": {"message_id": 9999}, "echo": call["echo"]})
            return ws

        app = web.Application()
        app.router.add_get("/", websocket)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        client = OneBotClient(f"ws://127.0.0.1:{port}/")
        events = client.events()
        try:
            self.assertIsInstance(await asyncio.wait_for(anext(events), 5), LifecycleMetaEvent)
            result = await asyncio.wait_for(client.call_api("send_private_msg", user_id=20002, message="test"), 5)
            self.assertEqual(result["message_id"], 9999)
            self.assertEqual(client._queue._queue.qsize(), 1024)
            self.assertIsNotNone(client._queue._overflow)
            received = [await anext(events) for _ in range(1100)]
            self.assertEqual([event.message_id for event in received], list(range(1100)))
        finally:
            await events.aclose()
        self.assertIsNone(client._worker)
        self.assertIsNone(client._queue._overflow)


class BusinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        cfg = self.cfg = ModuleType("src.config")
        cfg.log = Mock(spec=logging.Logger)
        cfg.ARCHIVE_DIR = directory
        cfg.WHITELIST = set()
        cfg.processed_friend_requests = {}
        cfg.friend_approve_time = {}
        cfg.unreplied_customers = {}
        cfg.PROCESSED_FRIEND_REQUESTS_EXPIRE = 300
        cfg.FRIEND_WELCOME_DELAY = 0
        cfg.FRIEND_WELCOME_RETRIES = 1
        cfg.FRIEND_WELCOME_RETRY_INTERVAL = 0
        cfg.FRIEND_COUNT_LIMIT = 100
        cfg.WELCOME_MESSAGE = "welcome"
        cfg.INTERNAL_GROUP_ID = 30003
        cfg.friend_count = 0
        cfg.client = SimpleNamespace(call_api=AsyncMock(return_value={}), self_id=10001)
        self.state = SimpleNamespace(save_state=Mock())
        self.history = SimpleNamespace(record_customer_message=AsyncMock(), record_staff_reply=AsyncMock())
        self.sender = SimpleNamespace(close_session=AsyncMock())
        modules = {"config": cfg, "state": self.state, "history": self.history, "message_sender": self.sender}
        with patch.dict(sys.modules, {f"src.{key}": value for key, value in modules.items()}), patch.multiple(src, create=True, **modules):
            self.storage = load_module("storage")
            self.storage.init_db_sync()
            with patch.dict(sys.modules, {"src.storage": self.storage}), patch.object(src, "storage", self.storage, create=True):
                self.private = load_module("private_msg")
                self.new_user = load_module("new_user")
        self.event = OneBotClient._parse_event(private_message())
        self.request = FriendRequestEvent.model_validate(
            dict(BASE, post_type="request", request_type="friend", comment="", flag="flag")
        )

    async def test_failed_approval_can_retry_and_success_is_not_repeated(self):
        self.cfg.client.call_api.side_effect = [RuntimeError("approval failed"), {}, {}, {}]
        await self.new_user.handle_friend_request(self.request)
        self.assertNotIn("flag", self.cfg.processed_friend_requests)
        await self.new_user.handle_friend_request(self.request)
        await self.new_user.handle_friend_request(self.request)
        actions = [call.args[0] for call in self.cfg.client.call_api.await_args_list]
        self.assertEqual(actions.count("set_friend_add_request"), 2)
        self.assertEqual(actions.count("send_private_msg"), 1)
        self.assertEqual(self.cfg.friend_count, 1)

    async def test_notification_failures_do_not_undo_approval(self):
        self.cfg.client.call_api.side_effect = [{}, RuntimeError("welcome failed"), RuntimeError("notify failed")]
        await self.new_user.handle_friend_request(self.request)
        await self.new_user.handle_friend_request(self.request)
        self.assertIn("flag", self.cfg.processed_friend_requests)
        self.assertEqual(self.cfg.client.call_api.await_count, 3)
        self.assertEqual(self.cfg.friend_count, 1)

    async def test_duplicate_private_message_does_not_change_reminder_or_history(self):
        await self.private.handle_private_msg(self.event)
        data = self.cfg.unreplied_customers[20002]
        data["is_newly_reported"] = True
        data["reported_milestones"] = {5}
        before = {**data, "msg_ids": list(data["msg_ids"]), "reported_milestones": {5}}
        await self.private.handle_private_msg(self.event)
        self.assertEqual(data, before)
        self.history.record_customer_message.assert_awaited_once()
        self.state.save_state.assert_called_once()

    async def test_persisted_message_does_not_reopen_a_closed_session(self):
        session = self.storage.open_session_sync(20002, 100)
        self.storage.record_event_sync(session, "customer_message", time=100, message_id=123)
        self.storage.close_session_by_window_sync(session, 100, 110, "test")
        await self.private.handle_private_msg(self.event)
        self.assertFalse(self.cfg.unreplied_customers)
        self.history.record_customer_message.assert_not_awaited()

    async def test_old_staff_reply_cannot_close_a_new_session(self):
        session = self.storage.open_session_sync(20002, 100)
        self.storage.record_event_sync(session, "staff_reply", time=100, message_id=124)
        await self.private.handle_private_msg(self.event)
        event = OneBotClient._parse_event(dict(
            private_message(124, target_id=20002), post_type="message_sent", user_id=10001,
        ))
        await self.private.handle_sent_msg(event)
        self.sender.close_session.assert_not_awaited()
        self.assertEqual(self.cfg.unreplied_customers[20002]["msg_ids"], [123])

    async def test_new_message_still_updates_the_existing_session(self):
        await self.private.handle_private_msg(self.event)
        second = OneBotClient._parse_event(private_message(124))
        await self.private.handle_private_msg(second)
        self.assertEqual(self.cfg.unreplied_customers[20002]["msg_ids"], [123, 124])
        self.assertEqual(self.history.record_customer_message.await_count, 2)

    async def test_database_failure_still_accepts_a_new_message(self):
        with patch.object(self.storage, "has_message", AsyncMock(side_effect=sqlite3.OperationalError("unavailable"))):
            await self.private.handle_private_msg(self.event)
        self.assertEqual(self.cfg.unreplied_customers[20002]["msg_ids"], [123])


if __name__ == "__main__":
    unittest.main()
