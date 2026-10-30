import importlib.util
import logging
import sys
import types
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


class ForwardMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 隔离生产配置与持久化；消息段仍使用真实的 NapCat SDK。
        package_name = "_forward_tests"
        source_dir = Path(__file__).resolve().parents[1] / "src"
        package = types.ModuleType(package_name)
        package.__path__ = [str(source_dir)]
        modules = {package_name: package}
        for name in ("config", "ai", "utils", "state", "history", "mute"):
            module = types.ModuleType(f"{package_name}.{name}")
            modules[module.__name__] = module
            setattr(package, name, module)

        cfg = modules[f"{package_name}.config"]
        cfg.log = logging.getLogger(__name__)
        cfg.STARTED_AT = 0
        cfg.monitored_forward_order = deque()
        cfg.monitored_forwards = {}
        cfg.last_command_time = {}
        cfg.RECENT_MESSAGE_MAX_AGE = 3600
        cfg.MONITORED_FORWARD_LIMIT = 100
        cfg.EMOJI_MAPPING = {}
        self.client = cfg.client = Mock(
            self_id=999,
            get_friend_list=AsyncMock(return_value=[
                {"user_id": 123, "nickname": "小明", "remark": "维修客户"},
                {"user_id": 456, "nickname": "小红", "remark": ""},
            ]),
            get_stranger_info=AsyncMock(return_value={"nickname": "陌生客户"}),
            send_group_forward_msg=AsyncMock(return_value={"message_id": 789}),
            send_group_msg=AsyncMock(),
        )
        self.ai = modules[f"{package_name}.ai"]
        self.ai.suggest_for_customers = AsyncMock(return_value={123: "请提供故障照片"})
        modules[f"{package_name}.utils"].format_duration = Mock()
        modules[f"{package_name}.utils"].get_current_available_members = Mock(return_value=[])
        modules[f"{package_name}.state"].save_state = Mock()
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        spec = importlib.util.spec_from_file_location(
            f"{package_name}.message_sender", source_dir / "message_sender.py",
        )
        self.sender = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sender)
        self.sender.get_recent_message_ids = AsyncMock(return_value=[11, 12])
        self.sender.add_emoji_to_message = AsyncMock()

    def sent_nodes(self):
        return [
            dict(node)["data"]
            for node in self.client.send_group_forward_msg.call_args.kwargs["messages"]
        ]

    async def test_customer_info_and_ai_are_separate_nodes_inside_forward(self):
        result = await self.sender.send_nested_forward(10, [(123, {})], "客户提醒")
        self.assertEqual(result, 789)
        nodes = self.sent_nodes()
        self.assertEqual(
            [node["nickname"] for node in nodes],
            ["客服系统提示", "客户信息", "AI建议", "小明"],
        )
        self.assertEqual(nodes[0]["content"][0]["data"]["text"], "客户提醒")
        self.assertEqual(
            nodes[1]["content"][0]["data"]["text"],
            "QQ号：123\n昵称：小明\n备注：维修客户",
        )
        self.assertEqual(nodes[3]["content"], [
            {"type": "node", "data": {"id": "11"}},
            {"type": "node", "data": {"id": "12"}},
        ])
        self.assertIn("客户 123", nodes[2]["content"][0]["data"]["text"])
        self.assertIn("请提供故障照片", nodes[2]["content"][0]["data"]["text"])
        self.assertNotIn("快捷传送门", str(nodes))
        self.assertNotIn("lion-qq", str(nodes))
        self.client.send_group_msg.assert_not_awaited()

    async def test_multiple_customers_have_distinct_profiles_and_suggestions(self):
        self.ai.suggest_for_customers.return_value[456] = "请提供机型"
        await self.sender.send_nested_forward(10, [(123, {}), (456, {})], "两位客户")
        nodes = self.sent_nodes()
        self.assertEqual(
            [node["nickname"] for node in nodes],
            ["客服系统提示", "客户信息", "AI建议", "小明", "客户信息", "AI建议", "小红"],
        )
        profiles = [
            node["content"][0]["data"]["text"]
            for node in nodes if node["nickname"] == "客户信息"
        ]
        self.assertEqual(profiles, [
            "QQ号：123\n昵称：小明\n备注：维修客户",
            "QQ号：456\n昵称：小红\n备注：（无）",
        ])
        suggestions = [
            node["content"][0]["data"]["text"]
            for node in nodes if node["nickname"] == "AI建议"
        ]
        self.assertEqual(suggestions, [
            "🤖 AI建议回复（客户 123）：\n请提供故障照片",
            "🤖 AI建议回复（客户 456）：\n请提供机型",
        ])
        self.client.get_friend_list.assert_awaited_once()
        self.client.get_stranger_info.assert_not_awaited()

    async def test_missing_suggestion_preserves_customer_order(self):
        await self.sender.send_nested_forward(10, [(456, {}), (123, {})], "两位客户")
        self.assertEqual(
            [node["nickname"] for node in self.sent_nodes()],
            ["客服系统提示", "客户信息", "小红", "客户信息", "AI建议", "小明"],
        )

    async def test_profile_api_failure_still_sends_history(self):
        self.client.get_friend_list.side_effect = RuntimeError("好友列表不可用")
        self.client.get_stranger_info.side_effect = RuntimeError("昵称不可用")
        self.assertEqual(
            await self.sender.send_nested_forward(10, [(123, {})], "提醒"), 789,
        )
        self.assertEqual(
            self.sent_nodes()[1]["content"][0]["data"]["text"],
            "QQ号：123\n昵称：未知昵称\n备注：（未获取到）",
        )

    async def test_stranger_nickname_fallback(self):
        profiles = await self.sender.get_customer_profiles([777])
        self.assertEqual(profiles, {777: ("陌生客户", "（未获取到）")})
        self.client.get_stranger_info.assert_awaited_once_with(user_id="777")

    async def test_ai_failure_or_disabled_still_sends_forward(self):
        for failure in (None, RuntimeError("AI超时")):
            with self.subTest(failure=failure):
                self.ai.suggest_for_customers.return_value = {}
                self.ai.suggest_for_customers.side_effect = failure
                result = await self.sender.send_nested_forward(10, [(123, {})], "提醒")
                self.assertEqual(result, 789)
                self.assertEqual(len(self.sent_nodes()), 3)
                self.client.send_group_msg.assert_not_awaited()

    async def test_empty_history_skips_forward_and_ai(self):
        self.sender.get_recent_message_ids.return_value = []
        self.assertIsNone(await self.sender.send_nested_forward(10, [(123, {})], "提醒"))
        self.client.send_group_forward_msg.assert_not_awaited()
        self.ai.suggest_for_customers.assert_not_awaited()

    async def test_skipped_customer_has_no_profile_or_ai_node(self):
        self.sender.get_recent_message_ids.side_effect = [[], [12]]
        self.ai.suggest_for_customers.return_value = {123: "不应显示", 456: "请提供机型"}
        await self.sender.send_nested_forward(10, [(123, {}), (456, {})], "提醒")
        self.ai.suggest_for_customers.assert_awaited_once_with([(456, {})])
        self.assertNotIn("不应显示", str(self.sent_nodes()))
        self.assertNotIn("QQ号：123", str(self.sent_nodes()))

    async def test_more_forward_includes_customer_profile(self):
        result = await self.sender.send_forward_from_message_ids(
            10, "最近两条消息", 123, [11, 12],
        )
        self.assertEqual(result, 789)
        nodes = self.sent_nodes()
        self.assertEqual(
            nodes[0]["content"][0]["data"]["text"],
            "最近两条消息\n\nQQ号：123\n昵称：小明\n备注：维修客户",
        )
        self.assertEqual(nodes[1]["nickname"], "小明")
        self.ai.suggest_for_customers.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
