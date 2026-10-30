import copy
import unittest
from unittest.mock import AsyncMock, patch

import test_message_sender as baseline


def message(mid, content, uid=123):
    return {
        "message_id": mid, "time": 1000 + mid,
        "sender": {"user_id": uid, "nickname": f"用户{uid}"},
        "message": content,
    }


def text(value):
    return {"type": "text", "data": {"text": value}}


def reply(mid):
    return {"type": "reply", "data": {"id": str(mid)}}


class ForwardQuoteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        baseline.ForwardMessageTests.setUp(self)
        self.client.get_msg = AsyncMock()

    def sent_nodes(self):
        return baseline.ForwardMessageTests.sent_nodes(self)

    async def test_quote_preserves_sender_time_text_and_images_without_mutating_history(self):
        image = {"type": "image", "data": {"file": "https://example.test/photo.jpg"}}
        messages = {
            "11": message(11, [reply(5), text("这里有问题")]),
            "5": message(5, [text("请查看照片"), image], uid=999),
            "12": message(12, [text("普通消息")]),
        }
        before = copy.deepcopy(messages)
        nodes = await self.sender.build_forward_history_nodes([11, 12], messages)
        first = dict(nodes[0])["data"]
        self.assertEqual(first["user_id"], "123")
        self.assertEqual(first["nickname"], "用户123")
        self.assertEqual(first["time"], 1011)
        self.assertEqual(first["content"], [
            text("【引用 用户999（999）】\n"), text("请查看照片"), image,
            text("\n【回复】\n"), text("这里有问题"),
        ])
        self.assertEqual(dict(nodes[1]), {"type": "node", "data": {"id": "12"}})
        self.assertEqual(messages, before)
        self.client.get_msg.assert_not_awaited()

    async def test_quote_outside_history_is_fetched_only_once(self):
        messages = {
            "11": message(11, [reply(5), text("第一条")]),
            "12": message(12, [reply(5), text("第二条")]),
        }
        self.client.get_msg.return_value = message(5, "原文", uid=999)
        nodes = await self.sender.build_forward_history_nodes([11, 12], messages)
        self.client.get_msg.assert_awaited_once_with(message_id="5")
        self.assertTrue(all("原文" in str(dict(node)) for node in nodes))

    async def test_unavailable_quote_keeps_reply_and_caches_failure(self):
        messages = {
            "11": message(11, [reply(5), text("第一条")]),
            "12": message(12, [reply(5), text("第二条")]),
        }
        self.client.get_msg.side_effect = RuntimeError("消息已撤回")
        nodes = await self.sender.build_forward_history_nodes([11, 12], messages)
        self.client.get_msg.assert_awaited_once_with(message_id="5")
        self.assertIn("原消息无法获取", str(dict(nodes[0])))
        self.assertIn("第一条", str(dict(nodes[0])))
        self.assertIn("第二条", str(dict(nodes[1])))

    async def test_nested_quotes_and_cycle_are_bounded(self):
        messages = {
            "11": message(11, [reply(5), text("最新回复")]),
            "5": message(5, [reply(3), text("中间回复")]),
            "3": message(3, [reply(11), text("最早回复")]),
        }
        nodes = await self.sender.build_forward_history_nodes([11], messages)
        content = dict(nodes[0])["data"]["content"]
        self.assertIn("已省略更深层引用", str(content))
        for value in ("最新回复", "中间回复", "最早回复"):
            self.assertIn(value, str(content))
        self.assertFalse(any(seg["type"] == "reply" for seg in content))
        self.client.get_msg.assert_not_awaited()

    async def test_long_quote_chain_stops_at_depth_limit(self):
        messages = {
            str(mid): message(mid, [reply(mid + 1), text(str(mid))])
            for mid in range(1, 10)
        }
        nodes = await self.sender.build_forward_history_nodes([1], messages)
        content = dict(nodes[0])["data"]["content"]
        self.assertIn("引用消息 5：已省略更深层引用", str(content))
        self.client.get_msg.assert_not_awaited()

    async def test_missing_id_and_empty_quote_have_visible_placeholders(self):
        messages = {
            "11": message(11, [
                {"type": "reply", "data": {}}, reply(5), text("保留回复"),
            ]),
            "5": message(5, []),
        }
        nodes = await self.sender.build_forward_history_nodes([11], messages)
        content = str(dict(nodes[0]))
        for value in ("缺少消息 ID", "原消息内容不可用", "保留回复"):
            self.assertIn(value, content)
        self.client.get_msg.assert_not_awaited()

    async def test_missing_source_message_falls_back_to_original_reference(self):
        self.client.get_msg.side_effect = RuntimeError("无法获取")
        nodes = await self.sender.build_forward_history_nodes([11])
        self.assertEqual(dict(nodes[0]), {"type": "node", "data": {"id": "11"}})

    async def test_more_path_expands_quotes_using_supplied_history(self):
        messages = [
            message(11, [reply(5), text("客户回复")]),
            message(5, [text("客服原文")], uid=999),
        ]
        result = await self.sender.send_forward_from_message_ids(
            10, "历史消息", 123, [11], messages=messages,
        )
        self.assertEqual(result, 789)
        self.assertIn("客服原文", str(self.sent_nodes()))
        self.assertIn("客户回复", str(self.sent_nodes()))
        self.client.get_msg.assert_not_awaited()

    async def test_reminder_path_expands_quotes_from_history_cache(self):
        async def recent(uid, **kwargs):
            kwargs["messages_by_id"].update({
                "11": message(11, [reply(5), text("客户回复")]),
                "5": message(5, [text("客服原文")], uid=999),
            })
            return [11]
        self.sender.get_recent_message_ids.side_effect = recent
        result = await self.sender.send_nested_forward(10, [(123, {})], "提醒")
        self.assertEqual(result, 789)
        self.assertIn("客服原文", str(self.sent_nodes()))
        self.client.get_msg.assert_not_awaited()

    async def test_history_cache_includes_quote_outside_age_window(self):
        self.client.get_friend_msg_history = AsyncMock(return_value={"messages": [
            message(5, [text("旧原文")]),
            message(11, [reply(5), text("新回复")]),
        ]})
        # 重新加载模块，恢复 setUp 替换前的真实历史获取函数。
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            self.sender.__name__, self.sender.__file__,
        )
        spec.loader.exec_module(self.sender)
        cache = {}
        with patch.object(self.sender.time, "time", return_value=1012):
            ids = await self.sender.get_recent_message_ids(
                123, max_age_seconds=2, messages_by_id=cache,
            )
        self.assertEqual(ids, [11])
        self.assertIn("5", cache)
        self.assertIn("11", cache)


if __name__ == "__main__":
    unittest.main()
