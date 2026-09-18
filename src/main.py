import asyncio
import signal

from napcat import (
    FriendAddNoticeEvent,
    FriendRequestEvent,
    GroupMsgEmojiLikeEvent,
    PrivateMessageEvent,
    FriendPokeEvent,
    GroupMessageEvent,
    GroupPokeEvent,
)

from . import config as cfg
from .config import log
from .state import load_state, save_state
from .monitor import monitor_loop
from .private_msg import handle_private_msg, handle_sent_msg, handle_friend_poke
from .new_user import handle_friend_request
from .group_msg import handle_group_emoji, handle_group_poke, handle_group_command
from .notice_pr import handle_notice_group_msg, flush_pending_acks
from .shell_console import shell_console_loop
from . import storage
from . import history

# 后台任务句柄，优雅关停时统一取消
_background_tasks: list[asyncio.Task] = []
_shutdown_requested = False
_shutdown_finished = False
# 入口 run_app 注入的 stop event：request_shutdown 时一并 set，用于打断 WS 等待
_external_stop: asyncio.Event | None = None


def bind_shutdown_event(event: asyncio.Event | None) -> None:
    """由入口把 stop event 绑到 bot，控制台/信号共用同一条关停路径。"""
    global _external_stop
    _external_stop = event


def request_shutdown(reason: str = "manual") -> None:
    """请求停止主事件循环（幂等）。"""
    global _shutdown_requested
    if _shutdown_requested:
        return
    _shutdown_requested = True
    log.info("收到停止请求: %s", reason)
    if _external_stop is not None:
        try:
            _external_stop.set()
        except RuntimeError:
            pass


def is_shutdown_requested() -> bool:
    return _shutdown_requested


async def _close_napcat_client() -> None:
    """尽量优雅关闭 NapCat WebSocket；失败不抛出。"""
    client = cfg.client
    conn = getattr(client, "_conn", None)
    if conn is not None:
        close = getattr(conn, "close", None)
        if close is not None:
            try:
                await close()
                log.info("NapCat WebSocket 连接已关闭")
                return
            except Exception as e:
                log.warning("connection.close() 失败: %s", e)

    # 回退：按引用计数退出客户端上下文
    try:
        refs = int(getattr(client, "_context_refs", 0) or 0)
        for _ in range(max(refs, 0)):
            await client.__aexit__(None, None, None)
        if refs > 0:
            log.info("NapCat 客户端上下文已退出")
    except Exception as e:
        log.warning("client.__aexit__ 失败: %s", e)


async def graceful_shutdown(reason: str = "signal") -> None:
    """优雅关停：取消后台任务 → 关闭连接 → 最后保存状态。可重入。"""
    global _shutdown_finished, _shutdown_requested
    if _shutdown_finished:
        return
    _shutdown_finished = True
    _shutdown_requested = True
    log.info("===== 开始关停（reason=%s） =====", reason)

    # 先打断控制台 stdin 读取，避免默认执行器在关停时被阻塞
    try:
        from .shell_console import request_console_stop
        request_console_stop()
    except Exception:
        pass

    tasks = [t for t in _background_tasks if t is not None and not t.done()]
    _background_tasks.clear()
    if tasks:
        log.info("正在取消 %d 个后台任务...", len(tasks))
        for t in tasks:
            t.cancel()
        done, pending = await asyncio.wait(tasks, timeout=5.0)
        for t in pending:
            name = t.get_name() if hasattr(t, "get_name") else repr(t)
            log.warning("后台任务未在 5 秒内结束: %s", name)
        for t in done:
            if t.cancelled():
                continue
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                continue
            if exc is not None:
                log.debug("后台任务结束时异常: %s", exc)

    try:
        await _close_napcat_client()
    except Exception as e:
        log.warning("关闭 NapCat 客户端时出错: %s", e)

    try:
        save_state()
        log.info("运行状态已保存: %s", cfg.STATE_FILE)
    except Exception as e:
        log.error("关停时保存状态失败: %s", e, exc_info=True)

    log.info("===== 关停完成 =====")


def _register_background_task(coro, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _background_tasks.append(task)
    return task


async def _bot_event_loop() -> None:
    """WebSocket 事件循环：连接、分发、断开重连，直到请求关停。"""
    startup_notified = False
    while not _shutdown_requested:
        log.info("正在连接 WebSocket...")
        try:
            async for event in cfg.client:
                if _shutdown_requested:
                    log.info("事件循环收到关停请求，停止处理新事件")
                    break

                if not startup_notified:
                    try:
                        await cfg.client.send_group_msg(
                            group_id=str(cfg.INTERNAL_GROUP_ID),
                            message="🤖 客服机器人已启动，开始监听消息。",
                        )
                        startup_notified = True
                        log.info("启动通知已发送至群 %d", cfg.INTERNAL_GROUP_ID)
                    except Exception as e:
                        log.error("发送启动通知失败: %s", e, exc_info=True)

                    for attempt in range(3):
                        if _shutdown_requested:
                            break
                        try:
                            friend_list = await cfg.client.send(
                                {"action": "get_friend_list", "params": {}},
                                timeout=30.0,
                            )
                            if friend_list.get("status") == "ok" and friend_list.get("retcode") == 0:
                                cfg.friend_count = len(friend_list.get("data", []))
                                log.info("好友数量已初始化: %d", cfg.friend_count)
                                break
                        except Exception as e:
                            log.warning("初始化好友数量失败 (尝试 %d/3): %s", attempt + 1, e)
                            if attempt < 2:
                                await asyncio.sleep(10)

                log.debug("收到事件: type=%s, post_type=%s", type(event).__name__, getattr(event, 'post_type', '?'))

                match event:
                    case FriendAddNoticeEvent():
                        if event.user_id not in cfg.friend_approve_time:
                            cfg.friend_count += 1
                            log.info("好友增加: user_id=%s, 好友数=%d", event.user_id, cfg.friend_count)

                    case FriendRequestEvent():
                        await handle_friend_request(event)

                    case GroupMsgEmojiLikeEvent():
                        await handle_group_emoji(event)

                    case PrivateMessageEvent(post_type="message_sent"):
                        await handle_sent_msg(event)

                    case PrivateMessageEvent(post_type="message"):
                        await handle_private_msg(event)

                    case FriendPokeEvent():
                        await handle_friend_poke(event)

                    case GroupPokeEvent():
                        await handle_group_poke(event)

                    case GroupMessageEvent():
                        # 通知群消息先走通知 PR 流程（仅 notice_pr.groups 命中时生效），
                        # 再进入客服群命令处理，两者互不影响
                        await handle_notice_group_msg(event)
                        await handle_group_command(event)

                    case _:
                        pass

            if _shutdown_requested:
                break
            log.warning("连接断开或出错，5秒后重连...")
            await asyncio.sleep(5)

        except asyncio.CancelledError:
            if _shutdown_requested:
                log.info("事件循环已取消（关停）")
                break
            raise
        except Exception as e:
            if _shutdown_requested:
                break
            log.error("事件循环异常: %s", e, exc_info=True)
            await asyncio.sleep(5)


async def main():
    log.info("程序启动, WS_URL=%s, 通知群=%d, 白名单=%s", cfg.WS_URL, cfg.INTERNAL_GROUP_ID, cfg.WHITELIST)
    log.info("里程碑阈值(分钟): %s", cfg.MILESTONES)

    load_state()

    # 会话库初始化与启动恢复：队列客户补齐会话周期、清理孤儿会话、恢复耗时统计
    try:
        await storage.init_db()
        await history.recover_sessions()
        await history.restore_reply_durations()
    except Exception as e:
        log.error("会话库初始化/恢复失败: %s", e, exc_info=True)

    _register_background_task(monitor_loop(), "monitor-loop")
    # 冲刷上次运行中发送失败的通知 PR ack（已落盘队列）
    _register_background_task(flush_pending_acks(), "notice-ack-flush")
    # 本地终端 Shell 控制台（stdin 不可用时自动禁用）
    _register_background_task(shell_console_loop(), "shell-console")

    try:
        await _bot_event_loop()
    finally:
        # 主循环退出后保证只执行一次收尾（重复调用会被幂等忽略）
        await graceful_shutdown(reason="main-exit")
