import asyncio

from napcat import (
    FriendAddNoticeEvent,
    FriendRequestEvent,
    GroupMsgEmojiLikeEvent,
    PrivateMessageEvent,
    FriendPokeEvent,
    GroupMessageEvent,
    GroupPokeEvent,
)

from .config import (
    log,
    WS_URL,
    INTERNAL_GROUP_ID,
    WHITELIST,
    MILESTONES,
    client,
)
from . import config as cfg
from .state import load_state
from .monitor import monitor_loop
from .private_msg import handle_private_msg, handle_sent_msg, handle_friend_poke
from .new_user import handle_friend_request
from .group_msg import handle_group_emoji, handle_group_poke, handle_group_command


async def main():
    log.info("程序启动, WS_URL=%s, 通知群=%d, 白名单=%s", WS_URL, INTERNAL_GROUP_ID, WHITELIST)
    log.info("里程碑阈值(分钟): %s", MILESTONES)

    load_state()
    startup_notified = False
    asyncio.create_task(monitor_loop())

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

                try:
                    friend_list = await client.get_friend_list()
                    cfg.friend_count = len(friend_list)
                    log.info("好友数量已初始化: %d", cfg.friend_count)
                except Exception as e:
                    log.warning("初始化好友数量失败: %s", e, exc_info=True)

            log.debug("收到事件: type=%s, post_type=%s", type(event).__name__, getattr(event, 'post_type', '?'))

            match event:
                case FriendAddNoticeEvent():
                    if event.user_id not in cfg.friend_approve_time:
                        cfg.friend_count += 1
                    log.info("好友增加通知: user_id=%s, 当前好友数=%d", event.user_id, cfg.friend_count)

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
                    await handle_group_command(event)

                case _:
                    pass

        log.warning("连接断开或出错，5秒后重连...")
        await asyncio.sleep(5)
