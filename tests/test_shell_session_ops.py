"""终端/群会话命令目标解析与拒绝路径的轻量回归测试（无真实 WS）。"""

from __future__ import annotations

import asyncio
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from napcat import Reply, Text  # noqa: E402
from src.group_msg import (  # noqa: E402
    extract_say_qq_segments,
    extract_say_target_segments,
    format_batch_result,
    parse_qq_arg,
    split_session_op,
)
from src.shell_console import handle_shell_command  # noqa: E402


class TestParseQqArg(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_qq_arg("123456"), 123456)

    def test_zero_and_negative_rejected(self):
        self.assertIsNone(parse_qq_arg("0"))
        self.assertIsNone(parse_qq_arg("-1"))

    def test_non_digit_rejected(self):
        self.assertIsNone(parse_qq_arg("abc"))
        self.assertIsNone(parse_qq_arg("12ab"))
        self.assertIsNone(parse_qq_arg(""))
        self.assertIsNone(parse_qq_arg("all"))

    def test_whitespace_ok(self):
        self.assertEqual(parse_qq_arg("  42 "), 42)


class TestSplitSessionOp(unittest.TestCase):
    def test_ops(self):
        self.assertEqual(split_session_op(".bye 99"), (".bye", "99"))
        self.assertEqual(split_session_op(".say 99 hi"), (".say", "99 hi"))
        self.assertEqual(split_session_op(".more"), (".more", ""))
        self.assertEqual(split_session_op(".close 1"), (".close", "1"))

    def test_non_session(self):
        self.assertIsNone(split_session_op(".list"))
        self.assertIsNone(split_session_op("hello"))
        self.assertIsNone(split_session_op(""))


class TestExtractSayQqSegments(unittest.TestCase):
    def test_with_content(self):
        msg = [Text(text=".say 123456 hello world")]
        qq, segs = extract_say_qq_segments(msg)
        self.assertEqual(qq, 123456)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].text, "hello world")

    def test_pending_only_qq(self):
        msg = [Text(text=".say 123456")]
        qq, segs = extract_say_qq_segments(msg)
        self.assertEqual(qq, 123456)
        self.assertEqual(segs, [])

    def test_invalid_qq(self):
        qq, segs = extract_say_qq_segments([Text(text=".say abc hi")])
        self.assertIsNone(qq)
        self.assertEqual(segs, [])

    def test_reply_ignored_in_segments(self):
        msg = [Reply(id="1"), Text(text=".say 99 ok"), Text(text="tail")]
        qq, segs = extract_say_qq_segments(msg)
        self.assertEqual(qq, 99)
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0].text, "ok")
        self.assertEqual(segs[1].text, "tail")

    def test_target_all_with_content(self):
        target, segs = extract_say_target_segments([Text(text=".say all 请稍候")])
        self.assertEqual(target.lower(), "all")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].text, "请稍候")

    def test_format_batch_result(self):
        text = format_batch_result("close all", 2, [9])
        self.assertIn("成功 2", text)
        self.assertIn("失败 1", text)
        self.assertIn("9", text)


class TestShellCommandDispatch(unittest.IsolatedAsyncioTestCase):
    async def _run(self, line: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            alive = await handle_shell_command(line)
        self.assertTrue(alive)
        return buf.getvalue()

    async def test_more_rejected(self):
        out = await self._run("more")
        self.assertIn("终端不支持", out)
        out2 = await self._run("more 123")
        self.assertIn("终端不支持", out2)
        out3 = await self._run("more all")
        self.assertIn("终端不支持", out3)

    async def test_say_missing_args(self):
        out = await self._run("say")
        self.assertIn("用法", out)
        out2 = await self._run("say all")
        self.assertIn("用法", out2)

    async def test_say_invalid_target(self):
        out = await self._run("say foo hi")
        self.assertIn("无效目标", out)

    async def test_bye_close_usage(self):
        out = await self._run("bye")
        self.assertIn("用法", out)
        out2 = await self._run("close")
        self.assertIn("用法", out2)

    async def test_close_all_empty_queue(self):
        from src import config as cfg

        saved = cfg.unreplied_customers.copy()
        cfg.unreplied_customers.clear()
        try:
            out = await self._run("close all")
            self.assertIn("没有待回复客户", out)
        finally:
            cfg.unreplied_customers.clear()
            cfg.unreplied_customers.update(saved)

    async def test_unknown_command(self):
        out = await self._run("nope")
        self.assertIn("未知命令", out)

    async def test_offline_say_bye_rejected(self):
        from src import config as cfg
        from unittest import mock

        fake = mock.Mock()
        fake.is_running = False
        with mock.patch.object(cfg, "client", fake):
            out = await self._run("say 123 hello")
            self.assertIn("客户端未运行", out)
            out2 = await self._run("bye 123")
            self.assertIn("客户端未运行", out2)

    async def test_shell_say_feedback_has_no_recall_hint(self):
        from src.message_sender import build_say_feedback

        text = build_say_feedback(1, True, True, recall_hint=False)
        self.assertNotIn("撤回", text)
        self.assertIn("已向客户 1", text)
        text_group = build_say_feedback(1, True, True, recall_hint=True)
        self.assertIn("撤回", text_group)

    async def test_shell_debounce_key_isolated(self):
        from src.shell_console import _shell_key, _shell_debounce
        from src import config as cfg
        from src import state as state_mod

        self.assertEqual(_shell_key(99, "say"), (99, "say"))
        # 终端防抖不写入持久化 last_command_time
        cfg.last_command_time.clear()
        _shell_debounce.clear()
        _shell_debounce[_shell_key(99, "say")] = 10**12
        out = await self._run("say 99 hi")
        self.assertNotIn("操作过于频繁", out)  # offline/error path, not group debounce
        self.assertNotIn(("shell", 99, "say"), cfg.last_command_time)
        self.assertTrue(all(not (isinstance(k, tuple) and len(k) == 3) for k in cfg.last_command_time))
        # 含合法 2 元组 + 异常 3 元组时 save_state 不抛错
        cfg.last_command_time[(123, "say")] = 1.0
        cfg.last_command_time[("shell", 99, "say")] = 2.0  # 防御：即便误写入也不崩
        state_mod.save_state()
        _shell_debounce.clear()

    async def test_help_lists_new_commands(self):
        out = await self._run("help")
        self.assertIn("say <qq|all>", out)
        self.assertIn("bye <qq|all>", out)
        self.assertIn("close <qq|all>", out)
        self.assertIn("终端不支持", out)


if __name__ == "__main__":
    unittest.main()
