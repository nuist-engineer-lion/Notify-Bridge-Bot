import time
import asyncio

from nonebot.adapters.onebot.v11.event import FriendRequestEvent
from nonebot.adapters.onebot.v11.exception import ActionFailed

from . import config as cfg
from .config import log


async def handle_friend_request(event: FriendRequestEvent) -> bool:
    """自动通过好友申请并私聊发送欢迎消息。"""
    uid = event.user_id
    comment = event.comment or ""
    flag = event.flag

    log.info("收到好友申请：" + str(uid) + " " + comment)
    now = time.time()
    last_time = cfg.processed_friend_requests.get(flag)
    if last_time and (now - last_time) < cfg.PROCESSED_FRIEND_REQUESTS_EXPIRE:
        log.info("好友申请重复事件已忽略: flag=%s, user_id=%s", flag, uid)
        return True

    try:
        await cfg.client.call_api("set_friend_add_request", flag=flag, approve=True)
        # 审批成功即标记，失败可重试；后续欢迎语或通知失败不重复审批。
        cfg.processed_friend_requests[flag] = time.time()
        cfg.friend_approve_time[uid] = time.time()

        await asyncio.sleep(cfg.FRIEND_WELCOME_DELAY)

        for attempt in range(cfg.FRIEND_WELCOME_RETRIES):
            try:
                await cfg.client.call_api(
                    "send_private_msg",
                    user_id=str(uid),
                    message=cfg.WELCOME_MESSAGE,
                )
                break
            except ActionFailed as e:
                if attempt < cfg.FRIEND_WELCOME_RETRIES - 1:
                    log.warning(
                        "欢迎消息发送失败，重试 %d/%d: user_id=%s, retcode=%s",
                        attempt + 1, cfg.FRIEND_WELCOME_RETRIES, uid, e.info.get("retcode"),
                    )
                    await asyncio.sleep(cfg.FRIEND_WELCOME_RETRY_INTERVAL)
                else:
                    log.error(
                        "欢迎消息全部重试失败: user_id=%s, retcode=%s, err=%s",
                        uid, e.info.get("retcode"), e, exc_info=True,
                    )
            except Exception as welcome_err:
                log.error("欢迎消息发送失败: user_id=%s, err=%s", uid, welcome_err, exc_info=True)
                break

        log.info("已自动通过好友申请: user_id=%s, comment=%s", uid, comment)
    except Exception as e:
        log.error("自动通过好友申请失败: user_id=%s, err=%s", uid, e, exc_info=True)
    return True
