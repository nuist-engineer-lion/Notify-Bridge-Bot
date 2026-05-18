import time
import asyncio

from napcat import FriendRequestEvent
from napcat.exceptions import NapCatAPIError

from .config import (
    log,
    INTERNAL_GROUP_ID,
    WELCOME_MESSAGE,
    FRIEND_WELCOME_DELAY,
    FRIEND_WELCOME_RETRIES,
    FRIEND_WELCOME_RETRY_INTERVAL,
    PROCESSED_FRIEND_REQUESTS_EXPIRE,
    processed_friend_requests,
    friend_approve_time,
    client,
)


async def handle_friend_request(event: FriendRequestEvent) -> bool:
    """自动通过好友申请并通知内部群"""
    uid = event.user_id
    comment = event.comment
    flag = event.flag

    log.info("收到好友申请：" + str(uid) + " " + comment)
    now = time.time()
    last_time = processed_friend_requests.get(flag)
    if last_time and (now - last_time) < PROCESSED_FRIEND_REQUESTS_EXPIRE:
        log.info("好友申请重复事件已忽略: flag=%s, user_id=%s", flag, uid)
        return True

    processed_friend_requests[flag] = now

    try:
        await event.approve()
        friend_approve_time[uid] = time.time()

        await asyncio.sleep(FRIEND_WELCOME_DELAY)

        for attempt in range(FRIEND_WELCOME_RETRIES):
            try:
                await client.send_private_msg(
                    user_id=str(uid),
                    message=WELCOME_MESSAGE,
                )
                break
            except NapCatAPIError as e:
                if attempt < FRIEND_WELCOME_RETRIES - 1:
                    log.warning(
                        "欢迎消息发送失败，重试 %d/%d: user_id=%s, retcode=%s",
                        attempt + 1, FRIEND_WELCOME_RETRIES, uid, e.retcode,
                    )
                    await asyncio.sleep(FRIEND_WELCOME_RETRY_INTERVAL)
                else:
                    log.error(
                        "欢迎消息全部重试失败: user_id=%s, retcode=%s, err=%s",
                        uid, e.retcode, e, exc_info=True,
                    )
            except Exception as welcome_err:
                log.error("欢迎消息发送失败: user_id=%s, err=%s", uid, welcome_err, exc_info=True)
                break

        notify_text = (
            "✅ 已自动通过好友申请\n"
            f"QQ: {uid}\n"
            f"备注: {comment or '（无）'}"
        )
        try:
            await client.send_group_msg(
                group_id=str(INTERNAL_GROUP_ID),
                message=notify_text,
            )
        except Exception as notify_err:
            log.error("好友申请群通知发送失败: user_id=%s, err=%s", uid, notify_err, exc_info=True)
        log.info("已自动通过好友申请: user_id=%s, comment=%s", uid, comment)
        processed_friend_requests[flag] = time.time()
    except Exception as e:
        log.error("自动通过好友申请失败: user_id=%s, err=%s", uid, e, exc_info=True)
        processed_friend_requests[flag] = time.time()
    return True
