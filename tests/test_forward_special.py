import copy
import json
import unittest

from napcat import Reply, Text

import test_forward_quotes as quotes


class SpecialMessageTests(unittest.IsolatedAsyncioTestCase):
    setUp = quotes.ForwardQuoteTests.setUp

    async def build(self, quoted, body=None):
        history = {
            "11": quotes.message(11, body or [quotes.reply(5), quotes.text("回复正文")]),
            "5": quotes.message(5, quoted, uid=999),
        }
        before = copy.deepcopy(history)
        nodes = await self.sender.build_forward_history_nodes([11], history)
        self.assertEqual(history, before)
        self.client.get_msg.assert_not_awaited()
        # 与发送 API 相同的原始 JSON 边界。
        return json.loads(json.dumps([dict(node) for node in nodes]))

    async def test_special_types_keep_original_native_node_in_quotes_and_body(self):
        fixtures = {
            "record": {"file": "voice.amr"},
            "video": {"file": "video.mp4"},
            "file": {"file_id": "abc", "name": "维修清单.pdf"},
            "forward": {"id": "resid", "content": []},
            "node": {"id": "1234"},
            "json": {"data": '{"app":"com.tencent.multimsg"}'},
            "xml": {"data": "<msg/>"},
            "music": {"type": "qq", "id": "123"},
            "markdown": {"content": "**卡片**"},
            "miniapp": {"data": "{}"},
            "onlinefile": {"name": "文档"},
            "flashtransfer": {"fileSetId": "123"},
            "mface": {"emoji_id": "123", "key": "abc"},
            "dice": {"result": 6},
            "rps": {"result": 2},
            "poke": {"type": "1", "id": "2"},
            "future_extension": {"payload": "不可丢失"},
        }
        for kind, data in fixtures.items():
            special = {"type": kind, "data": data}
            for quoted in (True, False):
                with self.subTest(kind=kind, quoted=quoted):
                    nodes = await self.build(
                        [special] if quoted else [quotes.text("原文")],
                        None if quoted else [quotes.reply(5), special, quotes.text("正文")],
                    )
                    expected_id = "5" if quoted else "11"
                    self.assertEqual(nodes[1], {"type": "node", "data": {"id": expected_id}})
                    content = nodes[0]["data"]["content"]
                    self.assertTrue(all(seg["type"] == "text" for seg in content))
                    self.assertIn("正文", str(content))
                    self.assertIn(f"见下方原消息 {expected_id}", str(content))

    async def test_special_content_without_reply_keeps_native_reference(self):
        for kind in ("record", "video", "file", "forward", "json", "node", "unknown"):
            with self.subTest(kind=kind):
                nodes = await self.sender.build_forward_history_nodes(
                    [11], {"11": quotes.message(11, [{"type": kind, "data": {}}])},
                )
                self.assertEqual([dict(node) for node in nodes], [
                    {"type": "node", "data": {"id": "11"}},
                ])

    async def test_multiple_special_segments_share_one_original_node(self):
        nodes = await self.build([
            {"type": "record", "data": {"file": "voice"}},
            {"type": "file", "data": {"name": "说明.pdf"}},
            {"type": "forward", "data": {"id": "forward"}},
        ])
        self.assertEqual(len(nodes), 2)
        self.assertIn("说明.pdf", str(nodes[0]))

    async def test_nested_nodes_never_mix_with_snapshot_text(self):
        nodes = await self.build([{
            "type": "node", "data": {
                "nickname": "原发送人", "user_id": "999",
                "content": [quotes.text("嵌套内容")],
            },
        }])
        self.assertTrue(all(seg["type"] != "node" for seg in nodes[0]["data"]["content"]))
        self.assertEqual(nodes[1]["data"], {"id": "5"})

    async def test_media_identifier_uses_url_without_losing_image_metadata(self):
        image = {"type": "image", "data": {
            "file": "ABC123.image", "url": "https://example.test/photo.jpg",
            "sub_type": 1, "summary": "[动画表情]",
        }}
        nodes = await self.build([image])
        self.assertEqual(len(nodes), 1)
        actual = next(seg for seg in nodes[0]["data"]["content"] if seg["type"] == "image")
        self.assertEqual(actual["data"], {
            **image["data"], "file": "https://example.test/photo.jpg",
        })

    async def test_images_with_sendable_sources_are_preserved(self):
        for source in (
            "https://example.test/1.jpg", "http://example.test/1.jpg",
            "base64://YWJj", "file:///C:/media/1.jpg",
        ):
            with self.subTest(source=source):
                image = {"type": "image", "data": {"file": source}}
                nodes = await self.build([image])
                self.assertIn(image, nodes[0]["data"]["content"])
                self.assertEqual(len(nodes), 1)

    async def test_unresolvable_image_keeps_native_original(self):
        nodes = await self.build([{"type": "image", "data": {"file": "ABC.image"}}])
        self.assertIn("图片", str(nodes[0]))
        self.assertFalse(any(seg["type"] == "image" for seg in nodes[0]["data"]["content"]))
        self.assertEqual(nodes[1]["data"], {"id": "5"})

    async def test_face_and_mentions_keep_original_fields(self):
        segments = [
            {"type": "face", "data": {"id": "14", "resultId": "1"}},
            {"type": "at", "data": {"qq": "123", "name": "客户"}},
        ]
        nodes = await self.build(segments)
        self.assertEqual(len(nodes), 1)
        for segment in segments:
            self.assertIn(segment, nodes[0]["data"]["content"])

    async def test_special_nodes_reach_both_forward_send_paths(self):
        history = [
            quotes.message(11, [quotes.reply(5), quotes.text("回复正文")]),
            quotes.message(5, [{"type": "record", "data": {"file": "voice.amr"}}]),
        ]
        await self.sender.send_forward_from_message_ids(
            10, "历史", 123, [11], messages=history,
        )
        async def recent(uid, **kwargs):
            kwargs["messages_by_id"].update({str(m["message_id"]): m for m in history})
            return [11]
        self.sender.get_recent_message_ids.side_effect = recent
        await self.sender.send_nested_forward(10, [(123, {})], "提醒")
        for call in self.client.send_group_forward_msg.call_args_list:
            customer_node = dict(call.kwargs["messages"][-1])
            nodes = customer_node["data"]["content"]
            self.assertEqual(nodes[-1], {"type": "node", "data": {"id": "5"}})
            self.assertIn("回复正文", str(nodes[0]))
            self.assertIn("语音", str(nodes[0]))
            self.assertTrue(all(node["type"] == "node" for node in nodes))

    async def test_cq_strings_parse_reply_media_and_escape_sequences(self):
        nodes = await self.build(
            "A&amp;B&#91;x&#93;&#44;&#65;[CQ:image,file=https://example.test/a?x=1&#44;2&amp;y=3]",
            "[CQ:reply,id=5]回复&#91;原文&#93;",
        )
        content = nodes[0]["data"]["content"]
        self.assertIn(quotes.text("A&B[x],&#65;"), content)
        self.assertIn(quotes.text("回复[原文]"), content)
        self.assertIn({
            "type": "image", "data": {"file": "https://example.test/a?x=1,2&y=3"},
        }, content)

    async def test_escaped_cq_is_literal_and_never_interpreted_twice(self):
        nodes = await self.build("&#91;CQ:reply,id=88&#93; &amp;#91;")
        self.assertIn(quotes.text("[CQ:reply,id=88] &#91;"), nodes[0]["data"]["content"])

    async def test_single_segment_dict_and_sdk_objects_are_supported(self):
        nodes = await self.build(quotes.text("单段消息"), quotes.reply(5))
        self.assertIn(quotes.text("单段消息"), nodes[0]["data"]["content"])
        nodes = await self.build(
            [Text(text="SDK原文")], [Reply(id="5"), Text(text="SDK回复")],
        )
        self.assertIn("SDK原文", str(nodes))
        self.assertIn("SDK回复", str(nodes))

    async def test_malformed_segments_and_sender_do_not_break_other_messages(self):
        history = {
            "11": quotes.message(11, [
                quotes.reply(5), None, "bad", {"type": "file", "data": None},
                quotes.text("有效正文"),
            ]),
            "5": quotes.message(5, {"type": "video", "data": {}}),
        }
        history["11"]["sender"] = "invalid"
        history["5"]["sender"] = ["invalid"]
        nodes = await self.sender.build_forward_history_nodes([11], history)
        self.assertIn("有效正文", str(dict(nodes[0])))
        self.assertIn("无法解析的消息段", str(dict(nodes[0])))
        self.assertEqual(dict(nodes[1])["data"]["id"], "5")

    async def test_raw_message_fallback_keeps_cq_reply(self):
        history = {
            "11": {"raw_message": "[CQ:reply,id=5]回复"},
            "5": {"raw_message": "原文"},
        }
        nodes = await self.sender.build_forward_history_nodes([11], history)
        self.assertIn("原文", str(dict(nodes[0])))
        self.assertIn("回复", str(dict(nodes[0])))


if __name__ == "__main__":
    unittest.main()
