import asyncio

from .config import (
    log,
    WS_URL,
    INTERNAL_GROUP_ID,
    WHITELIST,
    MILESTONES,
    client,
)
from .state import load_state
from .monitor import monitor_loop
from .events import handle_event


async def main():
    log.info("程序启动, WS_URL=%s, 通知群=%d, 白名单=%s", WS_URL, INTERNAL_GROUP_ID, WHITELIST)
    log.info("里程碑阈值(分钟): %s", MILESTONES)

    # 恢复上次运行时保存的状态
    load_state()

    startup_notified = False

    # 启动定时巡检后台任务
    asyncio.create_task(monitor_loop())

    # 自动重连
    while True:
        log.info("正在连接 WebSocket...")
        async for event in client:
            if not startup_notified:
                try:
                    await client.send_group_msg(
                        group_id=str(INTERNAL_GROUP_ID),
                        message="🤖 客服机器人已启动，开始监听消息。",
                    )
                    startup_notified = True
                    log.info("启动通知已发送至群 %d", INTERNAL_GROUP_ID)
                except Exception as e:
                    log.error("发送启动通知失败: %s", e, exc_info=True)

            log.debug("收到事件: type=%s, post_type=%s", type(event).__name__, getattr(event, 'post_type', '?'))
            await handle_event(event)

        log.warning("连接断开或出错，5秒后重连...")
        await asyncio.sleep(5)
