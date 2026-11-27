import asyncio
import os
import signal
import sys

from src import main as app
from src.log_setup import get_logger
from src.state import save_state

log = get_logger("entrypoint")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """注册 SIGINT/SIGTERM：请求优雅关停并唤醒主等待。"""

    def _on_stop(name: str) -> None:
        app.request_shutdown(name)
        stop_event.set()

    def _threadsafe(name: str) -> None:
        try:
            loop.call_soon_threadsafe(_on_stop, name)
        except RuntimeError:
            # loop 已关闭时直接置位
            app.request_shutdown(name)
            stop_event.set()

    for sig, name in ((signal.SIGINT, "SIGINT/Ctrl+C"), (signal.SIGTERM, "SIGTERM")):
        installed = False
        try:
            loop.add_signal_handler(sig, lambda n=name: _on_stop(n))
            installed = True
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        if not installed:
            try:
                signal.signal(sig, lambda _s, _f, n=name: _threadsafe(n))
            except Exception as e:
                log.debug("注册信号 %s 失败: %s", name, e)


async def run_app() -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    # 控制台 quit / 信号共用同一 stop event，与 bot 运行生命周期绑定
    app.bind_shutdown_event(stop_event)
    _install_signal_handlers(loop, stop_event)

    main_task = asyncio.create_task(app.main(), name="bot-main")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-wait")

    try:
        await asyncio.wait({main_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        app.request_shutdown("cancelled")
        stop_event.set()

    if not main_task.done():
        # 信号已到但主循环仍挂在 WS 迭代上：取消主任务以打破 async for
        log.info("正在停止主任务...")
        main_task.cancel()
        try:
            await main_task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("主任务退出异常")

    stop_task.cancel()
    try:
        await stop_task
    except asyncio.CancelledError:
        pass

    # 收尾（幂等；main 的 finally 也可能已调用过）
    await app.graceful_shutdown(reason="entry-signal")

    if main_task.done() and not main_task.cancelled():
        try:
            exc = main_task.exception()
        except asyncio.CancelledError:
            exc = None
        if exc is not None:
            raise exc


def main() -> None:
    try:
        asyncio.run(run_app())
    except KeyboardInterrupt:
        # 信号处理器未生效时的兜底：至少把状态落盘
        log.warning("强制中断，执行兜底状态保存...")
        try:
            save_state()
        except Exception:
            log.exception("强制中断时保存状态失败")
        log.info("程序已停止。")
        return

    if app.is_reboot_requested():
        log.info("正在重新启动机器人进程...")
        if app.is_reboot_silent():
            os.environ[app.REBOOT_SILENT_ENV] = "1"
        else:
            os.environ.pop(app.REBOOT_SILENT_ENV, None)
        # 保留解释器参数（含 -m / -X 等）、入口和启动参数。
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


if __name__ == "__main__":
    main()
