"""本地终端 Shell 控制台：与 bot 同进程、同生命周期。

- 控制台随 bot 启动，随 bot 关停结束；quit/exit 会请求停止整个 bot
- 命令回执只写本地终端，不向 QQ 群发送命令 ACK
- 业务结果以 log 为准（handler 已展示）；_print 仅用于校验失败等无日志场景
- unmute 与群内 .unmute 共用 mute.unmute_and_flush（含向通知群汇总延后提醒）
- 日志经 ConsoleSafeLogHandler 输出：插入日志后重绘 prompt + 已输入缓冲，避免打断命令
- stdin 读取可被 _stop_stdin_read 中断，避免关停时阻塞默认执行器
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time

from napcat import Text

from . import config as cfg
from .config import log
from . import mute
from .group_msg import (
    handle_bye_command,
    handle_close_command,
    parse_qq_arg,
    send_private_and_close,
)
from .utils import format_duration

_PROMPT = "notifybot> "
# 供日志 Handler 重绘：是否正在等待输入、当前已键入内容
_reading = False
_input_buffer = ""
_log_handler: logging.Handler | None = None
_installed_logger_names: list[str] = []
# 关停时置位，使阻塞在 executor 中的 stdin 读取尽快返回
_stop_stdin_read = threading.Event()
# 终端防抖：独立内存字典，不写入 last_command_time（避免破坏 state 持久化键格式）
_shell_debounce: dict[tuple, float] = {}


def request_console_stop() -> None:
    """优雅关停时调用：唤醒/退出 stdin 读取，避免阻塞默认执行器。"""
    _stop_stdin_read.set()


def _print(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _supports_ansi() -> bool:
    if os.environ.get("TERM_PROGRAM") or os.environ.get("WT_SESSION"):
        return True
    if os.name != "nt":
        return sys.stdout.isatty()
    # Windows 10+ 终端一般支持；失败时 handler 会回退空格清除
    return True


def _clear_current_line(stream) -> None:
    try:
        if _supports_ansi():
            stream.write("\r\033[2K")
        else:
            stream.write("\r" + " " * 120 + "\r")
    except Exception:
        try:
            stream.write("\r")
        except Exception:
            pass


class ConsoleSafeLogHandler(logging.Handler):
    """日志输出时清行打印，并在控制台读入中重绘 prompt+缓冲，避免打断输入。"""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            stream = sys.stderr
            _clear_current_line(stream)
            stream.write(msg + "\n")
            if _reading:
                stream.write(_PROMPT + _input_buffer)
            stream.flush()
        except Exception:
            self.handleError(record)


def _is_terminal_stream_handler(h: logging.Handler) -> bool:
    """是否为写向终端的 StreamHandler（FileHandler 等保留）。"""
    if isinstance(h, logging.FileHandler):
        return False
    if isinstance(h, logging.StreamHandler):
        stream = getattr(h, "stream", None)
        if stream in (sys.stdout, sys.stderr):
            return True
        # basicConfig 默认挂 stderr；未识别 stream 的 StreamHandler 也视为控制台
        if stream is None:
            return True
        # 常见：stream 是 sys.__stderr__
        if stream is getattr(sys, "__stdout__", None) or stream is getattr(sys, "__stderr__", None):
            return True
    return False


def _remove_console_handlers(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        if isinstance(h, ConsoleSafeLogHandler) or _is_terminal_stream_handler(h):
            logger.removeHandler(h)


def install_console_log_handler() -> None:
    """
    安装控制台安全日志 Handler。

    只挂在 root 上一次；子 logger 默认 propagate 到 root，避免同一 Handler
    在 root + 子 logger 各 emit 一次导致输出重复。
    propagate=False 的 logger 单独挂同一 handler。
    """
    global _log_handler, _installed_logger_names
    if _log_handler is not None:
        return

    handler = ConsoleSafeLogHandler()
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    _remove_console_handlers(root)
    root.addHandler(handler)

    # 清理已创建 logger 上的控制台 StreamHandler / 重复的本 Handler
    manager = root.manager
    attached_non_propagating: list[str] = []
    for name, lg in list(manager.loggerDict.items()):
        if not isinstance(lg, logging.Logger):
            continue
        _remove_console_handlers(lg)
        if not lg.propagate:
            lg.addHandler(handler)
            attached_non_propagating.append(name)

    # 确保关键 logger 会冒泡到 root（不各自挂 handler）
    for name in ("Notify-Bridge-Bot", "napcat", "napcat.client", "napcat.connection"):
        lg = logging.getLogger(name)
        _remove_console_handlers(lg)
        lg.propagate = True

    _log_handler = handler
    _installed_logger_names = ["root", *attached_non_propagating]


def uninstall_console_log_handler() -> None:
    global _log_handler, _installed_logger_names
    if _log_handler is None:
        return
    root = logging.getLogger()
    try:
        root.removeHandler(_log_handler)
    except Exception:
        pass
    manager = root.manager
    for lg in manager.loggerDict.values():
        if isinstance(lg, logging.Logger):
            try:
                lg.removeHandler(_log_handler)
            except Exception:
                pass
    _log_handler = None
    _installed_logger_names = []


HELP_TEXT = """\
可用控制台命令（与 bot 同生命周期；命令回执仅本地）：
  help                 显示本帮助
  status               查看运行状态
  list                 列出待回复客户
  mute [分钟]          临时静音；不带参数为不限时，直到 unmute
  unmute               解除静音并汇总延后提醒（与群内 .unmute 一致）
  reload               重载明文 config.yaml
  say <qq|all> <文本>  私聊发送；all=当前待回复队列
  bye <qq|all>         发送结束语并关闭会话
  close <qq|all>       关闭会话（不发结束语）
  more                 终端不支持；请在通知群用 .more
  quit / exit / stop   优雅停止 bot（保存状态并退出；与 Ctrl+C 相同）
"""


def format_status_text() -> str:
    uptime = format_duration(time.time() - cfg.STARTED_AT)
    pending = len(cfg.unreplied_customers)
    total_replies = len(cfg.reply_durations)
    monitored = len(cfg.monitored_forwards)
    delayed = len(cfg.delayed_notifications)
    lines = [
        "机器人状态",
        f"  运行时长：{uptime}",
        f"  待回复客户：{pending}",
        f"  已完结会话：{total_replies}",
        f"  监听合并转发：{monitored}",
        f"  延后通知：{delayed}",
        f"  静音状态：{mute.describe_mute_status()}",
        f"  通知群：{cfg.INTERNAL_GROUP_ID}",
        f"  客户端运行：{'是' if cfg.client.is_running else '否'}",
    ]
    return "\n".join(lines)


def format_customer_list() -> str:
    if not cfg.unreplied_customers:
        return "当前没有待回复客户。"
    sorted_customers = sorted(
        cfg.unreplied_customers.items(),
        key=lambda item: item[1]["last_active"],
        reverse=True,
    )
    now = time.time()
    lines = [f"待回复客户（{len(sorted_customers)}）："]
    for idx, (qq, data) in enumerate(sorted_customers[:20], 1):
        wait = format_duration(now - data["pending_since"])
        lines.append(f"  {idx}. {qq} 已等待 {wait}")
    if len(sorted_customers) > 20:
        lines.append(f"  ... 共 {len(sorted_customers)} 人，仅显示前 20 条")
    return "\n".join(lines)


def _parse_shell_target(token: str) -> tuple[int | None, bool, str | None]:
    """解析终端目标：返回 (qq, is_all, error)。"""
    t = (token or "").strip()
    if t.lower() == "all":
        return None, True, None
    qq = parse_qq_arg(t)
    if qq is None:
        return None, False, f"无效目标：{token}，请使用客户 QQ 号或 all"
    return qq, False, None


def _shell_key(target: int | str, op: str) -> tuple:
    """终端防抖键（仅存在 _shell_debounce，不进入持久化 last_command_time）。"""
    return (target, op)


def _shell_debounce_allowed(key: tuple, op: str) -> bool:
    debounce_sec = cfg.DEBOUNCE_SECONDS.get(op, 5)
    now = time.time()
    if now - _shell_debounce.get(key, 0) < debounce_sec:
        _print(f"操作过于频繁，请稍后再试（防抖 {debounce_sec} 秒）")
        return False
    return True


async def _shell_say(args: list[str]) -> None:
    if not args:
        _print("用法：say <qq|all> <文本…>")
        return
    qq, is_all, err = _parse_shell_target(args[0])
    if err:
        _print(err)
        return
    if len(args) < 2:
        _print("用法：say <qq|all> <文本…>（缺少要发送的内容）")
        return
    if not cfg.client.is_running:
        _print("客户端未运行，无法发送私聊消息。")
        return

    text = " ".join(args[1:]).strip()
    if not text:
        _print("用法：say <qq|all> <文本…>（缺少要发送的内容）")
        return
    segments = [Text(text=text)]

    if is_all:
        targets = list(cfg.unreplied_customers.keys())
        if not targets:
            _print("当前没有待回复客户。")
            return
        key = _shell_key("__all__", "say")
        if not _shell_debounce_allowed(key, "say"):
            return
        ok = 0
        failed: list[int] = []
        for cust in targets:
            feedback, _closed, _mid = await send_private_and_close(
                cust, segments,
                operator_uid=None,
                via="shell",
                close_reason="say",
            )
            if feedback.startswith("❌"):
                failed.append(cust)
            else:
                ok += 1
        if ok:
            _shell_debounce[key] = time.time()
        suffix = ""
        if failed:
            shown = ", ".join(str(x) for x in failed[:10])
            more = f" 等共 {len(failed)} 人" if len(failed) > 10 else ""
            suffix = f"；失败 QQ：{shown}{more}"
        _print(f"say all 完成：成功 {ok}，失败 {len(failed)}{suffix}")
        return

    key = _shell_key(qq, "say")
    if not _shell_debounce_allowed(key, "say"):
        return
    feedback, _closed, _mid = await send_private_and_close(
        qq, segments,
        operator_uid=None,
        via="shell",
        close_reason="say",
    )
    if not feedback.startswith("❌"):
        _shell_debounce[key] = time.time()
    _print(feedback)


async def _shell_bye(args: list[str]) -> None:
    if not args:
        _print("用法：bye <qq|all>")
        return
    qq, is_all, err = _parse_shell_target(args[0])
    if err:
        _print(err)
        return
    if not cfg.client.is_running:
        _print("客户端未运行，无法发送结束语。")
        return

    if is_all:
        targets = list(cfg.unreplied_customers.keys())
        if not targets:
            _print("当前没有待回复客户。")
            return
        key = _shell_key("__all__", "bye")
        if not _shell_debounce_allowed(key, "bye"):
            return
        ok = 0
        failed: list[int] = []
        for cust in targets:
            feedback = await handle_bye_command(
                None, None, cust, operator_uid=None, via="shell",
            )
            if feedback.startswith("❌"):
                failed.append(cust)
            else:
                ok += 1
        if ok:
            _shell_debounce[key] = time.time()
        suffix = ""
        if failed:
            shown = ", ".join(str(x) for x in failed[:10])
            more = f" 等共 {len(failed)} 人" if len(failed) > 10 else ""
            suffix = f"；失败 QQ：{shown}{more}"
        _print(f"bye all 完成：成功 {ok}，失败 {len(failed)}{suffix}")
        return

    key = _shell_key(qq, "bye")
    if not _shell_debounce_allowed(key, "bye"):
        return
    feedback = await handle_bye_command(
        None, None, qq, operator_uid=None, via="shell",
    )
    if not feedback.startswith("❌"):
        _shell_debounce[key] = time.time()
    _print(feedback)


async def _shell_close(args: list[str]) -> None:
    if not args:
        _print("用法：close <qq|all>")
        return
    qq, is_all, err = _parse_shell_target(args[0])
    if err:
        _print(err)
        return

    if is_all:
        targets = list(cfg.unreplied_customers.keys())
        if not targets:
            _print("当前没有待回复客户。")
            return
        key = _shell_key("__all__", "close")
        if not _shell_debounce_allowed(key, "close"):
            return
        ok = 0
        failed: list[int] = []
        for cust in targets:
            feedback = await handle_close_command(
                None, None, cust, operator_uid=None, via="shell",
            )
            if feedback.startswith("❌"):
                failed.append(cust)
            else:
                ok += 1
        if ok:
            _shell_debounce[key] = time.time()
        suffix = ""
        if failed:
            shown = ", ".join(str(x) for x in failed[:10])
            more = f" 等共 {len(failed)} 人" if len(failed) > 10 else ""
            suffix = f"；失败 QQ：{shown}{more}"
        _print(f"close all 完成：成功 {ok}，失败 {len(failed)}{suffix}")
        return

    key = _shell_key(qq, "close")
    if not _shell_debounce_allowed(key, "close"):
        return
    feedback = await handle_close_command(
        None, None, qq, operator_uid=None, via="shell",
    )
    if not feedback.startswith("❌"):
        _shell_debounce[key] = time.time()
    _print(feedback)


async def handle_shell_command(raw: str) -> bool:
    """处理一条控制台命令。返回 False 表示请求停止 bot/退出控制台循环。"""
    cmd = raw.strip()
    if not cmd:
        return True

    parts = cmd.split()
    op = parts[0].lower()
    args = parts[1:]

    if op in ("help", "?"):
        _print(HELP_TEXT)
        return True

    if op == "status":
        _print(format_status_text())
        return True

    if op == "list":
        _print(format_customer_list())
        return True

    if op in ("more", ".more"):
        _print("终端不支持 more：请在通知群使用 .more（可引用机器人消息，或 .more <客户QQ>）")
        return True

    if op == "say":
        await _shell_say(args)
        return True

    if op == "bye":
        await _shell_bye(args)
        return True

    if op == "close":
        await _shell_close(args)
        return True

    if op == "mute":
        # set_mute 已写 log（控制台日志 handler 会展示），不再 _print 重复回执
        if not args:
            mute.set_mute(None)
            return True
        try:
            minutes = float(args[0])
        except ValueError:
            _print(f"无效时长：{args[0]}，示例：mute 30；直接输入 mute 表示不限时")
            return True
        if minutes <= 0:
            _print("静音时长必须大于 0 分钟；不限时请直接输入 mute")
            return True
        try:
            mute.set_mute(minutes)
        except ValueError as e:
            _print(str(e))
        return True

    if op == "unmute":
        # 与群内 .unmute 一致：解除 + 汇总发出延后提醒（业务消息会进通知群）
        # 成功路径业务层已 log；仅在无日志可依时才本地回执
        was, flushed, text = await mute.unmute_and_flush()
        if not was and flushed == 0:
            _print(text)
        return True

    if op in ("reload", ".reload"):
        # force_reload_config 已 log 成功/警告/失败，不再 _print 重复结果
        await cfg.run_reload_cfg()
        return True

    if op in ("quit", "exit", "stop"):
        # 控制台与 bot 运行绑定：退出控制台 = 优雅停止整个 bot
        # request_shutdown 会 log「收到停止请求」，不再 _print 重复描述
        try:
            from . import main as app_main
            app_main.request_shutdown("console-quit")
        except Exception as e:
            _print(f"触发关停失败：{e}")
        return False

    _print(f"未知命令：{op}，输入 help 查看可用命令。")
    return True


def _read_line_sync() -> str:
    """
    同步读一行命令。Windows 用 msvcrt 跟踪缓冲以便日志后重绘；
    其他平台回退 readline（日志插入时至少重绘 prompt）。
    EOF 返回 ''。
    """
    global _reading, _input_buffer
    _reading = True
    _input_buffer = ""
    try:
        if os.name == "nt":
            return _read_line_windows()
        return _read_line_unix_fallback()
    finally:
        _reading = False
        _input_buffer = ""


def _read_line_windows() -> str:
    import msvcrt

    buf: list[str] = []
    global _input_buffer
    sys.stdout.write(_PROMPT)
    sys.stdout.flush()
    try:
        while True:
            if _stop_stdin_read.is_set():
                return ""
            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            ch = msvcrt.getwch()
            # Ctrl+C
            if ch == "\x03":
                raise KeyboardInterrupt
            # Enter
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(buf) + "\n"
            # Backspace
            if ch in ("\x08", "\x7f"):
                if buf:
                    buf.pop()
                    _input_buffer = "".join(buf)
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            # 方向键/功能键前缀 \x00 或 \xe0，吞掉后续
            if ch in ("\x00", "\xe0"):
                for _ in range(20):
                    if _stop_stdin_read.is_set():
                        return ""
                    if msvcrt.kbhit():
                        try:
                            msvcrt.getwch()
                        except Exception:
                            pass
                        break
                    time.sleep(0.01)
                continue
            buf.append(ch)
            _input_buffer = "".join(buf)
            sys.stdout.write(ch)
            sys.stdout.flush()
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise


def _read_line_unix_fallback() -> str:
    sys.stdout.write(_PROMPT)
    sys.stdout.flush()
    # 用可超时的 select 轮询，关停时能及时退出，避免阻塞默认执行器
    try:
        import select
    except ImportError:
        return sys.stdin.readline()

    try:
        while True:
            if _stop_stdin_read.is_set():
                return ""
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
            except (OSError, ValueError, TypeError):
                # select 不可用时退回阻塞读；EOF/关停由上层处理
                return sys.stdin.readline()
            if ready:
                return sys.stdin.readline()
    except Exception:
        return sys.stdin.readline()


async def shell_console_loop() -> None:
    """与 bot 同进程运行；bot 关停时本任务会被取消。"""
    global _reading, _input_buffer

    _stop_stdin_read.clear()
    install_console_log_handler()

    try:
        if sys.stdin is None or sys.stdin.closed:
            log.info("Shell 控制台未启用：无可用 stdin")
            return
    except Exception:
        log.info("Shell 控制台未启用：stdin 不可用")
        return

    loop = asyncio.get_running_loop()
    log.info("Shell 控制台已启动，Ctrl+C 或 quit 以停止")
    _print("输入 help 查看命令。")

    try:
        while True:
            try:
                line = await loop.run_in_executor(None, _read_line_sync)
            except (RuntimeError, asyncio.CancelledError):
                return
            except KeyboardInterrupt:
                try:
                    from . import main as app_main
                    app_main.request_shutdown("console-ctrl-c")
                except Exception:
                    pass
                return
            except Exception as e:
                log.warning("Shell 控制台读取失败，退出: %s", e)
                return

            if line == "":
                # 关停触发的空返回不记作「stdin 关闭」业务事件
                if _stop_stdin_read.is_set():
                    return
                log.info("Shell 控制台 stdin 关闭，控制台结束运行")
                return

            try:
                alive = await handle_shell_command(line)
            except Exception as e:
                # 已由日志 handler 输出（含 traceback），不再 _print 重复错误
                log.error("控制台命令执行失败: %s", e, exc_info=True)
                alive = True

            if not alive:
                return
    finally:
        _reading = False
        _input_buffer = ""
        _stop_stdin_read.set()
