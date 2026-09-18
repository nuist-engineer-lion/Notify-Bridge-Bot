"""本地终端 Shell 控制台：与 bot 同进程读 stdin，提供运维指令。

约束：终端命令的所有输出只写本地 stdout，不向任何 QQ 群发送命令回执。
延后提醒的汇总发送只由群内 .unmute 或巡检到期触发。
"""

from __future__ import annotations

import asyncio
import sys
import time

from . import config as cfg
from .config import log
from . import mute
from .utils import format_duration

HELP_TEXT = """\
可用控制台命令（输出仅在本地终端，不发送到群）：
  help                 显示本帮助
  status               查看运行状态
  list                 列出待回复客户
  mute [分钟]          临时静音；不带参数为不限时，直到 unmute
  unmute               解除静音（不向群发送；延后提醒需群内 .unmute 或等待汇总）
  reload               重载明文 config.yaml
  quit / exit          退出控制台（bot 继续运行；停止 bot 请用 Ctrl+C）
"""


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


def _describe_mute_opened() -> str:
    if mute.is_unlimited():
        return "已开启不限时静音（直到 unmute）。期间提醒将暂存；终端操作不会向群发送回执。"
    until_str = time.strftime("%H:%M:%S", time.localtime(cfg.mute_until))
    return (
        f"已开启临时静音（至 {until_str}）。"
        "期间提醒将暂存；终端操作不会向群发送回执。"
    )


async def handle_shell_command(raw: str) -> bool:
    """处理一条控制台命令。返回 False 表示请求退出控制台。

    所有反馈只写本地 stdout，绝不调用群消息接口。
    """
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
        # 无参数 = 不限时；有参数 = 限时分钟数
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
        # 仅本地解除静音状态，不向群汇总发送，避免终端命令输出进群
        was = mute.clear_mute()
        delayed = len(cfg.delayed_notifications)
        if not was:
            _print("当前未处于静音状态。")
        elif delayed > 0:
            _print(
                f"已在本地解除静音。延后通知仍有 {delayed} 条，"
                "未向群发送；请在群内执行 .unmute 汇总发出，或等待夜间汇总。"
            )
        else:
            _print("已在本地解除静音；暂无延后通知。")
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
    log.info("Shell 控制台已启动（输出仅本地），输入 help 查看命令；Ctrl+C 停止 bot")

    _print("Notify-Bridge-Bot Shell 控制台已就绪（输出仅本地，不发送到群）。输入 help 查看命令。")

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
