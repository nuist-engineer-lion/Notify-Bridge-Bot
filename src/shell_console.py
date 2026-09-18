"""本地终端 Shell 控制台：与 bot 同进程、同生命周期。

- 控制台随 bot 启动，随 bot 优雅关停结束；quit/exit 会请求停止整个 bot
- 命令回执只写本地终端，不向 QQ 群发送命令 ACK
- unmute 与群内 .unmute 共用 mute.unmute_and_flush（含向通知群汇总延后提醒）
- 日志经 ConsoleSafeLogHandler 输出：插入日志后重绘 prompt + 已输入缓冲，避免打断命令
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from . import config as cfg
from .config import log
from . import mute
from .utils import format_duration

_PROMPT = "notifybot> "
# 供日志 Handler 重绘：是否正在等待输入、当前已键入内容
_reading = False
_input_buffer = ""
_log_handler: logging.Handler | None = None
_installed_logger_names: list[str] = []


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


def _describe_mute_opened() -> str:
    if mute.is_unlimited():
        return "已开启不限时静音（直到 unmute）。期间提醒将暂存；终端操作不会向群发送命令回执。"
    until_str = time.strftime("%H:%M:%S", time.localtime(cfg.mute_until))
    return (
        f"已开启临时静音（至 {until_str}）。"
        "期间提醒将暂存；终端操作不会向群发送命令回执。"
    )


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

    if op == "mute":
        if not args:
            mute.set_mute(None)
            _print(_describe_mute_opened())
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
        _print(_describe_mute_opened())
        return True

    if op == "unmute":
        # 与群内 .unmute 一致：解除 + 汇总发出延后提醒（业务消息会进通知群）
        # 命令回执仍只打在本地终端
        _was, _flushed, text = await mute.unmute_and_flush()
        _print(text)
        return True

    if op in ("reload", ".reload"):
        ok, message = await cfg.run_reload_cfg()
        _print(("✔ " if ok else "✘ ") + message.replace("\n", "\n  "))
        return True

    if op in ("quit", "exit", "stop"):
        # 控制台与 bot 运行绑定：退出控制台 = 优雅停止整个 bot
        _print("控制台与 bot 运行绑定：正在请求优雅停止 bot（保存状态并退出）...")
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
                try:
                    msvcrt.getwch()
                except Exception:
                    pass
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
    line = sys.stdin.readline()
    return line


async def shell_console_loop() -> None:
    """与 bot 同进程运行；bot 关停时本任务会被取消。"""
    global _reading, _input_buffer

    install_console_log_handler()

    try:
        if sys.stdin is None or sys.stdin.closed:
            log.info("Shell 控制台未启用：无可用 stdin")
            return
    except Exception:
        log.info("Shell 控制台未启用：stdin 不可用")
        return

    loop = asyncio.get_running_loop()
    log.info("Shell 控制台已启动（与 bot 运行绑定；输出仅本地），Ctrl+C 或 quit 优雅停止")
    _print("Notify-Bridge-Bot Shell 控制台已就绪（与 bot 运行绑定，命令回执仅本地）。")
    _print("输入 help 查看命令。")

    try:
        while True:
            try:
                line = await loop.run_in_executor(None, _read_line_sync)
            except (RuntimeError, asyncio.CancelledError):
                log.info("Shell 控制台已停止")
                return
            except KeyboardInterrupt:
                _print("收到 Ctrl+C，正在请求优雅停止 bot...")
                try:
                    from . import main as app_main
                    app_main.request_shutdown("console-ctrl-c")
                except Exception:
                    pass
                return
            except Exception as e:
                log.warning("Shell 控制台读取失败，控制台退出: %s", e)
                return

            if line == "":
                log.info("Shell 控制台收到 EOF（stdin 关闭）；bot 继续运行，控制台结束")
                _print("Shell 控制台已结束（stdin 关闭）。bot 仍在运行；停止请在服务管理器发送 SIGTERM。")
                return

            try:
                alive = await handle_shell_command(line)
            except Exception as e:
                log.error("控制台命令执行失败: %s", e, exc_info=True)
                _print(f"命令执行失败：{e}")
                alive = True

            if not alive:
                return
    finally:
        _reading = False
        _input_buffer = ""
