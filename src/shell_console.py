"""本地终端 Shell 控制台：与 bot 同进程读 stdin，提供运维指令。"""

from __future__ import annotations

import asyncio
import sys
import time

from . import config as cfg
from .config import log
from . import mute
from .utils import format_duration, is_night_time

HELP_TEXT = """\
可用控制台命令：
  help                 显示本帮助
  status               查看运行状态（本地输出）
  list                 列出待回复客户
  mute [分钟]          临时静音（默认 {default} 分钟）
  unmute               解除静音并汇总发出延后提醒
  reload               重载明文 config.yaml
  quit / exit          退出控制台（bot 继续运行；停止 bot 请用 Ctrl+C）
""".format(default=60)


def _print(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


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


async def handle_shell_command(raw: str) -> bool:
    """处理一条控制台命令。返回 False 表示请求退出控制台。"""
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
        minutes = cfg.MUTE_DEFAULT_MINUTES
        if args:
            try:
                minutes = float(args[0])
            except ValueError:
                _print(f"无效时长：{args[0]}，示例：mute 30")
                return True
            if minutes <= 0:
                _print("静音时长必须大于 0 分钟")
                return True
        until = mute.set_mute(minutes)
        until_str = time.strftime("%H:%M:%S", time.localtime(until))
        _print(f"已开启临时静音 {minutes:g} 分钟（至 {until_str}）。期间提醒将暂存，解除时汇总发出。")
        return True

    if op == "unmute":
        was = mute.clear_mute()
        flushed = await mute.flush_delayed_notifications(reason="unmute")
        if not was and flushed == 0:
            _print("当前未处于静音状态。")
        elif flushed > 0:
            _print(f"已解除静音，并汇总发出 {flushed} 名客户的延后提醒。")
        elif was:
            if is_night_time():
                _print("已解除静音；当前仍在夜间模式，延后通知将在次日汇总发送。")
            else:
                _print("已解除静音；暂无延后通知。")
        return True

    if op in ("reload", ".reload"):
        ok, message = await cfg.run_reload_cfg()
        _print(("✔ " if ok else "✘ ") + message.replace("\n", "\n  "))
        return True

    if op in ("quit", "exit"):
        _print("控制台已退出（bot 仍在后台运行）。停止 bot 请在进程终端按 Ctrl+C。")
        return False

    _print(f"未知命令：{op}，输入 help 查看可用命令。")
    return True


async def shell_console_loop() -> None:
    """在终端读取命令行；stdin 关闭（如 systemd）时自动禁用控制台。"""
    try:
        if sys.stdin is None or sys.stdin.closed:
            log.info("Shell 控制台未启用：无可用 stdin")
            return
    except Exception:
        log.info("Shell 控制台未启用：stdin 不可用")
        return

    loop = asyncio.get_running_loop()
    log.info("Shell 控制台已启动，输入 help 查看命令；Ctrl+C 停止 bot")

    _print("Notify-Bridge-Bot Shell 控制台已就绪，输入 help 查看命令。")

    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except (RuntimeError, asyncio.CancelledError):
            log.info("Shell 控制台已停止")
            return
        except Exception as e:
            log.warning("Shell 控制台读取失败，控制台退出: %s", e)
            return

        if line == "":
            # EOF：非交互环境或 stdin 被关闭
            log.info("Shell 控制台收到 EOF，控制台已禁用（bot 继续运行）")
            _print("Shell 控制台已禁用（stdin 关闭），bot 继续运行。")
            return

        try:
            alive = await handle_shell_command(line)
        except Exception as e:
            log.error("控制台命令执行失败: %s", e, exc_info=True)
            _print(f"命令执行失败：{e}")
            alive = True

        if not alive:
            return
