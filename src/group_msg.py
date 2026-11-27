import time
import asyncio
import random
from dataclasses import dataclass
from datetime import date
from sqlite3 import Error as SQLiteError

from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.adapters.onebot.v11.event import PokeNotifyEvent

from .onebot import GroupMsgEmojiLikeEvent, GroupMessageEvent

from . import config as cfg
from .log_setup import get_logger
from .utils import format_duration
from . import mute
from .message_sender import (
    close_session,
    send_status_panel,
    send_forward_from_message_ids,
    send_and_track_feedback,
    track_forward_message,
    add_emoji_to_message,
    remove_emoji_from_message,
    action_emoji_ids,
    build_say_feedback,
    register_recallable_send,
)
from . import history, storage
from .notice_pr import handle_notice_command
from .watch_chart import build_watch_chart

log = get_logger(__name__)

# 运维类命令（无需引用机器人消息）
OPS_COMMAND_PREFIXES = ('.say', '.bye', '.more', '.help', '.close', '.list', '.reload', '.update', '.reboot', '.mute', '.unmute', '.status')

_pending_say_tasks: dict[int, asyncio.Task] = {}


@dataclass
class ByeConfirmation:
    group_id: int
    operator_uid: int
    customer_sessions: dict[int, tuple[int | None, float] | None]
    confirm_emoji_id: int
    emoji_ids: tuple[int, ...]
    expires_at: float


_pending_bye: dict[int, ByeConfirmation] = {}
_pending_bye_tasks: dict[int, asyncio.Task] = {}


def pop_pending_bye(mid: int) -> ByeConfirmation | None:
    pending = _pending_bye.pop(mid, None)
    task = _pending_bye_tasks.pop(mid, None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    return pending


def _customer_session(customer_id: int) -> tuple[int | None, float] | None:
    data = cfg.unreplied_customers.get(customer_id)
    return (data.get("session_id"), data["pending_since"]) if data is not None else None


async def request_bye_confirmation(gid: int, mid: int, user_id: int, targets: list[int]) -> None:
    """首次点击只发确认提示，每位操作者仅保留最新的一次请求。"""
    timeout = cfg.BYE_CONFIRM_TIMEOUT_SECONDS
    emoji_pool = cfg.BYE_CONFIRM_EMOJI_POOL
    now = asyncio.get_running_loop().time()
    for prompt_id, pending in list(_pending_bye.items()):
        if pending.expires_at <= now or pending.operator_uid == user_id:
            pop_pending_bye(prompt_id)
            await remove_emoji_from_message(prompt_id, list(pending.emoji_ids))
    emoji_ids = tuple(random.sample(emoji_pool, 5))
    pending = ByeConfirmation(
        group_id=gid,
        operator_uid=user_id,
        customer_sessions={cust: _customer_session(cust) for cust in targets},
        confirm_emoji_id=random.choice(emoji_ids),
        emoji_ids=emoji_ids,
        expires_at=now + timeout,
    )
    shown = "、".join(str(cust) for cust in list(pending.customer_sessions)[:10])
    if len(pending.customer_sessions) > 10:
        shown += "……"
    resp = await cfg.client.call_api(
        "send_group_msg", group_id=str(gid),
        message=[
            MessageSegment.at(user_id),
            MessageSegment.text(
                f" 完成人机验证后将向 {shown} 发送结束语\n"
                f"请在 {timeout:g} 秒内点击本消息下方的 "
            ),
            MessageSegment.face(pending.confirm_emoji_id),
            MessageSegment.text(" 表情完成验证，超时将取消发送。"),
        ],
    )
    prompt_id = resp.get("message_id") if resp else None
    if prompt_id is None:
        log.warning("结束语确认消息未返回消息 ID，未执行操作: group=%s, user=%s", gid, user_id)
        return
    pending.expires_at = asyncio.get_running_loop().time() + timeout
    prompt_id = int(prompt_id)
    _pending_bye[prompt_id] = pending
    log.info("群指令等待表情确认: command=.bye group_id=%d user_id=%d message_id=%d prompt_id=%d",
             gid, user_id, mid, prompt_id)

    async def expire_confirmation() -> None:
        await asyncio.sleep(timeout)
        if _pending_bye.get(prompt_id) is pending:
            try:
                await handle_bye_confirmation_click(gid, prompt_id, user_id, [pending.confirm_emoji_id])
            except Exception:
                log.exception("清理结束语验证表情失败: message_id=%s", prompt_id)

    _pending_bye_tasks[prompt_id] = asyncio.create_task(expire_confirmation())
    await add_emoji_to_message(prompt_id, list(pending.emoji_ids))


async def handle_bye_confirmation_click(gid: int, mid: int, user_id: int, emoji_ids: list[int]) -> bool:
    pending = _pending_bye.get(mid)
    if pending is None:
        return False
    if gid != pending.group_id or user_id != pending.operator_uid:
        return True
    if not emoji_ids:
        return True

    # 先消费请求再等待网络，重复点击、重复事件均不会再次发送结束语。
    pop_pending_bye(mid)
    expired = asyncio.get_running_loop().time() >= pending.expires_at
    if expired or any(eid != pending.confirm_emoji_id for eid in emoji_ids):
        log.info("群指令取消确认: command=.bye group_id=%d user_id=%d prompt_id=%d reason=%s",
                 gid, user_id, mid, "超时" if expired else "表情不匹配")
        await remove_emoji_from_message(mid, list(pending.emoji_ids))
        return True
    else:
        ok = 0
        failed: list[int] = []
        for cust, session in pending.customer_sessions.items():
            if _customer_session(cust) != session:
                result = f"❌ 客户 {cust} 的会话已变化，请重新发起确认。"
            else:
                result = await handle_bye_command(gid, mid, cust, operator_uid=user_id, via="emoji")
            if result.startswith("❌"):
                failed.append(cust)
            else:
                ok += 1
        log.info("群指令确认处理结束: command=.bye group_id=%d user_id=%d prompt_id=%d 成功=%d 失败=%d",
                 gid, user_id, mid, ok, len(failed))
        if len(pending.customer_sessions) == 1:
            await send_and_track_feedback(gid, mid, result, cust)
            return True
        feedback = format_batch_result("发送结束语并关闭会话", ok, failed)
        if failed:
            feedback += "\n失败或会话已变化的客户未关闭，请核对后重新操作。"

    await cfg.client.call_api(
        "send_group_msg", group_id=str(gid),
        message=[MessageSegment.reply(str(mid)), MessageSegment.text(feedback)],
    )
    return True


async def _send_reload_feedback(gid: int, msg_id: int, text: str) -> None:
    """引用回复失败时退回普通消息，最终失败交给原有事件循环处理。"""
    try:
        await cfg.client.call_api(
            "send_group_msg", group_id=str(gid),
            message=[MessageSegment.reply(str(msg_id)), MessageSegment.text(text)],
        )
    except Exception:
        await cfg.client.call_api("send_group_msg", group_id=str(gid), message=text)


def pop_pending_say(user_id: int) -> dict | None:
    """结束等待，并取消对应的超时任务。"""
    pending = cfg.pending_say.pop(user_id, None)
    task = _pending_say_tasks.pop(user_id, None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    return pending


async def expire_pending_say(user_id: int, pending: dict) -> None:
    """结束本次等待，将原提示上的取消表情换成配置的超时表情。"""
    if cfg.pending_say.get(user_id) is not pending:
        return
    pop_pending_say(user_id)
    prompt_msg_id = pending.get("prompt_msg_id")
    if prompt_msg_id:
        await remove_emoji_from_message(
            prompt_msg_id, [pending.get("cancel_emoji_id", cfg.EMOJI_MAPPING["cancel"])],
        )
        await add_emoji_to_message(prompt_msg_id, [cfg.EMOJI_MAPPING["say_timeout"]])


def register_pending_say(user_id: int, pending: dict) -> None:
    pop_pending_say(user_id)
    timeout = cfg.SAY_INPUT_TIMEOUT_SECONDS
    pending["cancel_emoji_id"] = cfg.EMOJI_MAPPING["cancel"]
    pending["expires_at"] = asyncio.get_running_loop().time() + timeout
    cfg.pending_say[user_id] = pending

    async def wait_for_timeout() -> None:
        await asyncio.sleep(timeout)
        await expire_pending_say(user_id, pending)

    _pending_say_tasks[user_id] = asyncio.create_task(wait_for_timeout())


def parse_qq_arg(token: str) -> int | None:
    """解析客户 QQ 号；非正整数返回 None。"""
    t = (token or "").strip()
    if not t or not t.isdigit():
        return None
    qq = int(t)
    return qq if qq > 0 else None


def extract_say_target_segments(message: list[MessageSegment]) -> tuple[str | None, list[MessageSegment]]:
    """无 Reply 时解析 .say <qq|all> [内容…]：返回 (目标 token, 可发送消息段)。"""
    target: str | None = None
    segments: list[MessageSegment] = []
    first_text_done = False
    for seg in message:
        if seg.type == "reply":
            continue
        if seg.type == "text" and not first_text_done:
            first_text_done = True
            text = seg.data["text"].strip()
            if not text.startswith(".say"):
                continue
            rest = text[len(".say"):].strip()
            parts = rest.split(None, 1)
            if not parts:
                return None, []
            target = parts[0]
            if len(parts) > 1 and parts[1].strip():
                segments.append(MessageSegment.text(parts[1]))
        else:
            segments.append(seg)
    return target, segments


def extract_say_qq_segments(message: list[MessageSegment]) -> tuple[int | None, list[MessageSegment]]:
    """兼容旧语义：仅接受数字 QQ；all/非法返回 (None, [])。"""
    target, segments = extract_say_target_segments(message)
    if target is None:
        return None, []
    qq = parse_qq_arg(target)
    if qq is None:
        return None, []
    return qq, segments


def format_batch_result(op_label: str, ok: int, failed: list[int]) -> str:
    suffix = ""
    if failed:
        shown = ", ".join(str(x) for x in failed[:10])
        more = f" 等共 {len(failed)} 人" if len(failed) > 10 else ""
        suffix = f"；失败 QQ：{shown}{more}"
    return f"{op_label} 完成：成功 {ok}，失败 {len(failed)}{suffix}"


def extract_say_plain_content(message: list[MessageSegment]) -> list[MessageSegment]:
    """无目标参数时：.say 后全部内容作为正文（自动匹配客户用）。"""
    segments: list[MessageSegment] = []
    first_text_done = False
    for seg in message:
        if seg.type == "reply":
            continue
        if seg.type == "text" and not first_text_done:
            first_text_done = True
            text = seg.data["text"].strip()
            if text.startswith(".say"):
                rest = text[len(".say"):].strip()
                if rest:
                    segments.append(MessageSegment.text(rest))
            else:
                segments.append(seg)
        else:
            segments.append(seg)
    return segments


def sole_unreplied_customer() -> int | None:
    """待回复队列恰好 1 人时返回其 QQ，否则 None。"""
    keys = list(cfg.unreplied_customers.keys())
    return keys[0] if len(keys) == 1 else None


async def apply_session_op_to_customers(
    op: str,
    customer_ids: list[int],
    *,
    gid: int | None,
    operator_uid: int | None,
    via: str,
    reply_id: int | None = None,
    segments: list[MessageSegment] | None = None,
) -> tuple[int, list[int]]:
    """对一组客户执行 say/bye/close（不含 more）。返回 (成功数, 失败 QQ 列表)。"""
    ok = 0
    failed: list[int] = []
    for cust in customer_ids:
        if op == "say":
            if not segments:
                failed.append(cust)
                continue
            feedback, _closed, _mid = await send_private_and_close(
                cust, segments,
                operator_uid=operator_uid,
                via=via,
                close_reason="say",
                group_id=gid,
                reply_id=reply_id,
            )
        elif op == "bye":
            feedback = await handle_bye_command(
                gid, reply_id, cust, operator_uid=operator_uid, via=via,
            )
        elif op == "close":
            feedback = await handle_close_command(
                gid, reply_id, cust, operator_uid=operator_uid, via=via,
            )
        else:
            failed.append(cust)
            continue
        if feedback.startswith("❌"):
            failed.append(cust)
        else:
            ok += 1
    return ok, failed


async def send_private_and_close(
    customer_id: int,
    segments: list[MessageSegment],
    *,
    operator_uid: int | None,
    via: str,
    close_reason: str = "say",
    group_id: int | None = None,
    reply_id: int | None = None,
) -> tuple[str, bool, int | None]:
    """私聊发送并关闭待回复会话（终端 say / 群 .say <qq> 共用）。返回 (feedback, closed, msg_id)。"""
    customer_msg_id: int | None = None
    try:
        send_resp = await cfg.client.call_api(
            "send_private_msg",
            user_id=str(customer_id),
            message=segments,
        )
        raw_mid = send_resp.get("message_id") if send_resp else None
        customer_msg_id = int(raw_mid) if raw_mid is not None else None
        await history.record_command(
            customer_id, operator_uid, "say",
            mode="content", reply_id=reply_id, group_id=group_id, via=via,
        )
        if customer_msg_id is not None:
            await history.record_staff_reply(
                customer_id, customer_msg_id, time.time(), segments, actor_uid=operator_uid,
            )
        closed = await close_session(customer_id, send_closing=False, close_reason=close_reason)
        feedback = build_say_feedback(
            customer_id,
            closed,
            customer_msg_id is not None,
            recall_hint=via != "shell",
        )
        return feedback, closed, customer_msg_id
    except Exception:
        log.exception("发送私聊消息失败: customer=%s", customer_id)
        return "❌ 发送失败，请稍后重试。", False, customer_msg_id


async def send_say_feedback(
    gid: int, reply_id: int, customer_id: int, operator_uid: int,
    feedback: str, customer_msg_id: int | None,
) -> None:
    """各类 .say 共用反馈、监听和限时撤回登记。"""
    resp = await cfg.client.call_api(
        "send_group_msg",
        group_id=str(gid),
        message=[MessageSegment.reply(str(reply_id)), MessageSegment.text(feedback)],
    )
    feedback_msg_id = resp.get("message_id")
    if feedback_msg_id:
        track_forward_message(feedback_msg_id, [customer_id], gid)
        if customer_msg_id is not None and not feedback.startswith("❌"):
            register_recallable_send(feedback_msg_id, customer_msg_id, customer_id, gid, operator_uid)
            asyncio.create_task(add_emoji_to_message(feedback_msg_id, [cfg.EMOJI_MAPPING["recall"]]))


# ======================= 昵称批量获取 =======================

async def get_nicknames_batch(user_ids: list[int], delay: float = 0.2) -> dict[int, str]:
    """
    批量获取用户昵称，返回 {qq: nickname} 映射。
    逐次调用 get_stranger_info 并加入延迟以避免限流。
    """
    nicknames: dict[int, str] = {}
    for uid in user_ids:
        try:
            info = await cfg.client.call_api("get_stranger_info", user_id=str(uid))
            nickname = info.get("nickname", "未知昵称")
            nicknames[uid] = str(nickname)
        except Exception as e:
            log.error("获取用户 %d 昵称失败: %s", uid, e)
            nicknames[uid] = "获取失败"
        await asyncio.sleep(delay)
    return nicknames


# ======================= 命令处理辅助 =======================

def extract_sendable_segments(message: list[MessageSegment], strip_prefix: str | None = None) -> list[MessageSegment]:
    """从群消息中提取可私聊发送的消息段（去掉 Reply），可选去掉首个 Text 段的命令前缀。"""
    segments: list[MessageSegment] = []
    for seg in message:
        if seg.type == "reply":
            continue
        if seg.type == "text" and strip_prefix:
            text = seg.data["text"]
            if text.startswith(strip_prefix):
                text = text[len(strip_prefix):]
                if not text:
                    continue
            segments.append(MessageSegment.text(text))
        else:
            segments.append(seg)
    return segments


async def resolve_target_from_reply(reply_id: int) -> tuple[list[int] | None, str | None]:
    """
    根据被引用的消息ID解析对应的客户列表。
    返回 (customer_ids, error_message)，若成功则 error_message 为 None。
    """
    data = cfg.monitored_forwards.get(reply_id)
    if data is not None:
        return data["customer_ids"], None

    try:
        msg_detail = await cfg.client.call_api("get_msg", message_id=str(reply_id))
        sender_id = int(msg_detail.get("user_id", 0))
        self_id = int(cfg.client.self_id)
        if sender_id == self_id:
            return None, "操作已过期"
        else:
            return None, "无法识别的消息"
    except Exception as e:
        log.error("查询消息详情失败: reply_id=%s, err=%s", reply_id, e)
        return None, "查询消息失败"


async def handle_bye_command(
    gid: int | None,
    reply_id: int | None,
    customer_id: int,
    operator_uid: int | None = None,
    via: str = "group_reply",
) -> str:
    """执行 .bye 命令：发送结束语并关闭会话，返回反馈文本"""
    try:
        await history.record_command(
            customer_id, operator_uid, "bye",
            reply_id=reply_id, group_id=gid, via=via,
        )
        resp = await cfg.client.call_api(
            "send_private_msg",
            user_id=str(customer_id),
            message=cfg.CLOSING_MESSAGE,
        )
        mid = resp.get("message_id") if resp else None
        await history.record_bot_send(
            customer_id, int(mid) if mid is not None else None, "closing_message", cfg.CLOSING_MESSAGE,
        )
        closed = await close_session(customer_id, send_closing=False, close_reason="bye")
        return f"✅ 已向客户 {customer_id} 发送结束语。" + ("（客户已在待回复队列）" if closed else "（客户不在待回复队列）")
    except Exception:
        log.exception("发送结束语失败: customer=%s", customer_id)
        return "❌ 发送失败，请稍后重试。"


async def handle_close_command(
    gid: int | None,
    reply_id: int | None,
    customer_id: int,
    operator_uid: int | None = None,
    via: str = "group_reply",
) -> str:
    """执行 .close 命令：仅关闭会话，不发送结束语，返回反馈文本"""
    try:
        await history.record_command(
            customer_id, operator_uid, "close",
            reply_id=reply_id, group_id=gid, via=via,
        )
        closed = await close_session(customer_id, send_closing=False, close_reason="close")
        return f"✅ 已关闭客户 {customer_id} 的会话（未发送结束语）。" + ("（客户已在待回复队列）" if closed else "（客户不在待回复队列）")
    except Exception:
        log.exception("关闭会话失败: customer=%s", customer_id)
        return "❌ 关闭失败，请稍后重试。"


async def handle_more_command(
    gid: int | None,
    reply_id: int | None,
    customer_id: int,
    operator_uid: int | None = None,
    via: str = "group_reply",
) -> tuple[bool, str | None, int | None]:
    """
    执行 .more 命令：获取历史消息并发送合并转发。
    返回 (成功标志, 反馈文本或 None, 新合并转发的消息ID或 None)
    """
    try:
        await history.record_command(
            customer_id, operator_uid, "more",
            reply_id=reply_id, group_id=gid, via=via,
        )
        resp = await cfg.client.call_api(
            "get_friend_msg_history",
            user_id=str(customer_id),
            count=100,
            parse_mult_msg=True,
        )
        messages = resp.get("messages", [])
        if not messages:
            return True, f"客户 {customer_id} 暂无更多历史消息", None

        messages.sort(key=lambda m: m.get("time", 0))
        msg_ids = [m["message_id"] for m in messages if "message_id" in m]
        title = f"客户 {customer_id} 的最近 {len(msg_ids)} 条消息"
        new_fwd_id = await send_forward_from_message_ids(
            group_id=gid,
            title=title,
            user_id=customer_id,
            message_ids=msg_ids,
            messages=messages,
        )
        if new_fwd_id:
            return True, None, new_fwd_id
        return False, "❌ 历史消息发送失败，请稍后重试。", None
    except Exception:
        log.exception("获取历史消息失败: customer=%s", customer_id)
        return False, "❌ 获取历史消息失败，请稍后重试。", None


async def handle_recall_click(gid: int, mid: int, user_id: int) -> bool:
    """
    处理撤回表情点击：撤回最近一次 .say 发送给客户的私聊消息。
    仅在 recall_window_seconds 内生效；返回 True 表示命中了可撤回记录。
    """
    data = cfg.recallable_sends.pop(mid, None)
    if data is None:
        return False

    customer_id = data["customer_id"]
    elapsed = time.time() - data["sent_at"]
    if elapsed > cfg.RECALL_WINDOW_SECONDS:
        log.info("撤回请求已超时: feedback_msg=%d, 耗时 %.1f 秒, 点击者=%d", mid, elapsed, user_id)
        await cfg.client.call_api(
            "send_group_msg",
            group_id=str(gid),
            message=[MessageSegment.reply(str(mid)), MessageSegment.text(f"⏰ 已超过 {cfg.RECALL_WINDOW_SECONDS} 秒，无法撤回该消息。")],
        )
        return True

    try:
        await cfg.client.call_api("delete_msg", message_id=str(data["customer_msg_id"]))
    except Exception:
        log.exception("撤回私聊消息失败: customer=%s, msg_id=%s", customer_id, data["customer_msg_id"])
        # 仍在时限内则恢复记录，允许重试
        if time.time() - data["sent_at"] <= cfg.RECALL_WINDOW_SECONDS:
            cfg.recallable_sends[mid] = data
        await cfg.client.call_api(
            "send_group_msg",
            group_id=str(gid),
            message=[MessageSegment.reply(str(mid)), MessageSegment.text("❌ 撤回失败，请稍后重试。")],
        )
        return True

    log.info("已撤回 .say 发送的私聊消息: customer=%d, msg_id=%d, 点击者=%d",
             customer_id, data["customer_msg_id"], user_id)
    try:
        await history.record_command(
            customer_id, user_id, "recall",
            feedback_msg_id=mid,
            customer_msg_id=data["customer_msg_id"],
            group_id=gid,
            operator_id=data.get("operator_id"),
            elapsed=round(elapsed, 1),
        )
    except Exception as e:
        log.error("记录撤回事件失败: customer=%s, err=%s", customer_id, e)
    await cfg.client.call_api(
        "send_group_msg",
        group_id=str(gid),
        message=[MessageSegment.reply(str(mid)), MessageSegment.text(f"🧹 已撤回发送给客户 {customer_id} 的消息。")],
    )
    return True


# ======================= 群事件处理 =======================

def _log_group_command(event: GroupMessageEvent | GroupMsgEmojiLikeEvent, command: str, status: str) -> None:
    log.info(
        "群指令%s: command=%s group_id=%d user_id=%d message_id=%d",
        status, command, event.group_id, event.user_id, event.message_id,
    )


async def handle_group_emoji(event: GroupMsgEmojiLikeEvent) -> bool:
    """处理内部群表情贴纸操作：匹配表情到对应命令"""
    gid = event.group_id
    mid = event.message_id

    if gid != cfg.INTERNAL_GROUP_ID:
        return False

    log.debug(
        "内部群表情上报: group_id=%d user_id=%d message_id=%d action=%s count=%d",
        gid, event.user_id, mid, "add" if event.is_add else "remove", len(event.likes),
    )

    if event.user_id == cfg.client.self_id:
        return False

    if not event.is_add or not event.likes:
        return True
    if mid in _pending_bye:
        try:
            emoji_ids = [int(like.get("emoji_id")) for like in event.likes]
        except (TypeError, ValueError):
            return True
        await handle_bye_confirmation_click(gid, mid, event.user_id, emoji_ids)
        return True
    first = event.likes[0]
    try:
        # SnowLuma convertGroupMsgEmojiLike 与 NapCat OB11MsgEmojiLikeEvent 均用 emoji_id；
        # 源码版本及路径见 onebot.py 的核对基线，SnowLuma 添加/移除事件均已实测。
        eid = int(first.get("emoji_id"))
    except (TypeError, ValueError):
        return True

    # 检查是否点击了 .say 通报消息上的撤回表情
    # 过期记录在宽限期内仍保留，handle_recall_click 会回复超时提示
    if eid == cfg.EMOJI_MAPPING.get("recall") and mid in cfg.recallable_sends:
        _log_group_command(event, ".say", "收到撤回表情")
        await handle_recall_click(gid, mid, event.user_id)
        return True

    # 检查是否是 pending say 提示消息上的确认/取消
    pending = cfg.pending_say.get(event.user_id)
    if pending and mid == pending["prompt_msg_id"]:
        cmd = cfg.EMOJI_TO_CMD.get(eid)
        if cmd == "cancel":
            _log_group_command(event, ".say", "收到取消表情")
            pop_pending_say(event.user_id)
            await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[MessageSegment.reply(str(mid)), MessageSegment.text("❌ 已取消发送。")],
            )
        return True

    tracked_data = cfg.monitored_forwards.get(mid)
    if tracked_data is None:
        return True

    customer_ids = list(tracked_data["customer_ids"])
    cmd = cfg.EMOJI_TO_CMD.get(eid)
    if cmd not in ("say", "bye", "close", "more"):
        return True

    # more 仅支持单客户；say/bye/close 在多客户合并转发上作用域扩展为该消息内全部客户
    if not customer_ids:
        return True
    _log_group_command(event, f".{cmd}", "收到表情操作")
    if cmd == "more" and len(customer_ids) != 1:
        _log_group_command(event, ".more", "引用包含多个客户")
        await cfg.client.call_api(
            "send_group_msg",
            group_id=str(gid),
            message=[MessageSegment.reply(str(mid)), MessageSegment.text("暂不支持：该合并转发包含多个客户，more 请手动处理单客户。")],
        )
        return True
    if cmd == "more":
        customer_id = customer_ids[0]
        try:
            success, feedback, new_fwd_id = await handle_more_command(
                gid, mid, customer_id, operator_uid=event.user_id, via="emoji",
            )
            if success and new_fwd_id:
                track_forward_message(new_fwd_id, [customer_id], gid)
                await add_emoji_to_message(new_fwd_id, action_emoji_ids())
            if feedback:
                await send_and_track_feedback(gid, mid, feedback, customer_id)
            _log_group_command(event, ".more", "处理完成" if success else "处理失败")
        except Exception:
            log.exception("表情操作失败: command=more group_id=%d message_id=%d", gid, mid)
        return True

    # say / bye / close：作用域 = 转发内全部客户
    targets = customer_ids
    try:
        if cmd == "say":
            resp = await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[MessageSegment.reply(str(mid)), MessageSegment.text("📝 请发送要回复给客户的消息内容：")],
            )
            prompt_msg_id = resp.get("message_id")
            register_pending_say(event.user_id, {
                "prompt_msg_id": prompt_msg_id,
                "customer_id": targets[0],
                "customer_ids": targets,
                "reply_id": mid,
                "group_id": gid,
                "via": "emoji",
                "target_source": "emoji_multi" if len(targets) > 1 else "emoji",
                "debounce_key": (mid, "say"),
            })
            _log_group_command(event, ".say", "等待回复内容")
            for cust in targets:
                await history.record_command(
                    cust, event.user_id, "say",
                    mode="pending", via="emoji", reply_id=mid, group_id=gid,
                )
            if prompt_msg_id:
                hint = ""
                if len(targets) > 1:
                    hint = f"（将发送给合并转发中的 {len(targets)} 名客户）"
                await add_emoji_to_message(prompt_msg_id, [cfg.EMOJI_MAPPING["cancel"]])
                if hint:
                    await cfg.client.call_api(
                        "send_group_msg",
                        group_id=str(gid),
                        message=[MessageSegment.reply(str(prompt_msg_id)), MessageSegment.text(hint)],
                    )
        elif cmd == "bye":
            await request_bye_confirmation(gid, mid, event.user_id, targets)
        elif cmd == "close":
            if len(targets) == 1:
                feedback = await handle_close_command(
                    gid, mid, targets[0], operator_uid=event.user_id, via="emoji",
                )
                await send_and_track_feedback(gid, mid, feedback, targets[0])
                _log_group_command(event, ".close", "处理失败" if feedback.startswith("❌") else "处理完成")
            else:
                ok, failed = await apply_session_op_to_customers(
                    "close", targets,
                    gid=gid,
                    operator_uid=event.user_id,
                    via="emoji",
                    reply_id=mid,
                )
                summary = format_batch_result("emoji close all", ok, failed)
                _log_group_command(event, ".close", f"批量处理结束（成功 {ok}，失败 {len(failed)}）")
                await cfg.client.call_api(
                    "send_group_msg",
                    group_id=str(gid),
                    message=[MessageSegment.reply(str(mid)), MessageSegment.text(summary)],
                )
    except Exception:
        log.exception("表情操作失败: command=%s group_id=%d message_id=%d", cmd, gid, mid)
    return True


async def handle_group_poke(event: PokeNotifyEvent) -> bool:
    """处理内部群戳一戳：发送状态面板"""
    if event.group_id == cfg.INTERNAL_GROUP_ID and event.target_id == cfg.client.self_id:
        log.info("内部群戳一戳触发状态面板: group=%d", event.group_id)
        await send_status_panel(event.group_id)
        return True
    return False


async def handle_session_command_by_qq(
    event: GroupMessageEvent,
    gid: int,
    msg_id: int,
    op: str,
    rest: str,
) -> bool:
    """无 Reply 时按客户 QQ / all / 队列唯一客户自动匹配执行会话命令。"""
    usage = {
        ".say": "用法：.say <客户QQ|all> <内容>（无参且队列仅 1 人时自动匹配；或引用后 .say <内容>）",
        ".bye": "用法：.bye <客户QQ|all>（无参且队列仅 1 人时自动匹配）",
        ".close": "用法：.close <客户QQ|all>（无参且队列仅 1 人时自动匹配）",
        ".more": "用法：.more <客户QQ>（无参且队列仅 1 人时自动匹配；不支持 all）",
    }
    op_name = op.lstrip(".")

    async def _reply(text: str) -> None:
        await cfg.client.call_api(
            "send_group_msg",
            group_id=str(gid),
            message=[MessageSegment.reply(str(msg_id)), MessageSegment.text(text)],
        )

    rest_parts = rest.split()
    now = time.time()
    debounce_sec = cfg.DEBOUNCE_SECONDS.get(op_name, 5)

    # ---- all ----
    if rest_parts and rest_parts[0].lower() == "all":
        if op == ".more":
            await _reply("❌ 群内不允许 .more all；请指定具体客户 QQ，或引用机器人消息。")
            _log_group_command(event, op, "参数无效")
            return True
        if op == ".say":
            _target, segments = extract_say_target_segments(event.message)
            if not segments:
                await _reply("❌ " + usage[".say"] + "（all 必须附带发送内容，不支持等待输入）")
                _log_group_command(event, op, "缺少发送内容")
                return True
        key = ("__all__", op_name)
        if now - cfg.last_command_time.get(key, 0) < debounce_sec:
            await _reply(f"⏳ 操作过于频繁，请 {debounce_sec} 秒后重试。")
            _log_group_command(event, op, "触发频率限制")
            return True

        targets = list(cfg.unreplied_customers.keys())
        if not targets:
            await _reply("📭 当前没有待回复客户。")
            _log_group_command(event, op, "队列为空")
            return True

        if op in (".say", ".bye") and not cfg.client.is_running:
            await _reply("❌ 客户端未运行，无法执行批量 " + op)
            _log_group_command(event, op, "客户端未运行")
            return True

        ok, failed = await apply_session_op_to_customers(
            op_name, targets,
            gid=gid,
            operator_uid=event.user_id,
            via="group_qq",
            segments=segments if op == ".say" else None,
        )
        if ok:
            cfg.last_command_time[key] = now
        await _reply(format_batch_result(f"{op_name} all", ok, failed))
        _log_group_command(event, op, f"批量处理结束（成功 {ok}，失败 {len(failed)}）")
        return True

    # ---- 目标解析：显式 QQ / all 已处理；否则无参自动匹配 ----
    auto_qq = sole_unreplied_customer()
    queue_len = len(cfg.unreplied_customers)
    say_segments: list[MessageSegment] | None = None
    qq: int | None = None

    if op == ".say":
        qq, say_segments = extract_say_qq_segments(event.message)
        if qq is None:
            # 无合法 QQ 参数：队列唯一则自动匹配，正文为 .say 后全部内容
            if auto_qq is not None:
                qq = auto_qq
                say_segments = extract_say_plain_content(event.message)
            else:
                msg = usage[".say"]
                if queue_len == 0:
                    msg = "📭 当前没有待回复客户，且未指定客户 QQ。"
                elif queue_len > 1:
                    msg = f"待回复客户有 {queue_len} 人，请指定 QQ 或 all。{usage['.say']}"
                await _reply("❌ " + msg)
                _log_group_command(event, op, "缺少客户目标")
                return True
    else:
        if rest_parts:
            qq = parse_qq_arg(rest_parts[0])
            if qq is None:
                await _reply("❌ " + usage[op])
                _log_group_command(event, op, "参数无效")
                return True
        else:
            if auto_qq is not None:
                qq = auto_qq
            else:
                msg = usage[op]
                if queue_len == 0:
                    msg = "📭 当前没有待回复客户，且未指定客户 QQ。"
                elif queue_len > 1:
                    msg = f"待回复客户有 {queue_len} 人，请指定 QQ。{usage[op]}"
                await _reply("❌ " + msg)
                _log_group_command(event, op, "缺少客户目标")
                return True

    key = (qq, op_name)
    if now - cfg.last_command_time.get(key, 0) < debounce_sec:
        await _reply(f"⏳ 操作过于频繁，请 {debounce_sec} 秒后重试。")
        _log_group_command(event, op, "触发频率限制")
        return True

    if op == ".say":
        if not say_segments:
            resp = await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[MessageSegment.reply(str(msg_id)), MessageSegment.text("📝 请发送要回复给客户的消息内容：")],
            )
            prompt_msg_id = resp.get("message_id")
            register_pending_say(event.user_id, {
                "prompt_msg_id": prompt_msg_id,
                "customer_id": qq,
                "customer_ids": [qq],
                "reply_id": None,
                "group_id": gid,
                "via": "group_qq",
                "target_source": "qq" if rest_parts and parse_qq_arg(rest_parts[0]) else "auto",
                "debounce_key": key,
            })
            await history.record_command(
                qq, event.user_id, "say",
                mode="pending", reply_id=None, group_id=gid, via="group_qq",
            )
            if prompt_msg_id:
                await add_emoji_to_message(prompt_msg_id, [cfg.EMOJI_MAPPING["cancel"]])
            _log_group_command(event, op, "等待回复内容")
            return True
        feedback, _closed, customer_msg_id = await send_private_and_close(
            qq, say_segments,
            operator_uid=event.user_id,
            via="group_qq",
            close_reason="say",
            group_id=gid,
            reply_id=None,
        )
        if not feedback.startswith("❌"):
            cfg.last_command_time[key] = now
        await send_say_feedback(
            gid, msg_id, qq, event.user_id, feedback, customer_msg_id,
        )
        _log_group_command(event, op, "处理失败" if feedback.startswith("❌") else "处理完成")
        return True

    if op in (".bye", ".close"):
        handler = handle_bye_command if op == ".bye" else handle_close_command
        feedback = await handler(gid, None, qq, operator_uid=event.user_id, via="group_qq")
        if not feedback.startswith("❌"):
            cfg.last_command_time[key] = now
        await send_and_track_feedback(gid, msg_id, feedback, qq)
        _log_group_command(event, op, "处理失败" if feedback.startswith("❌") else "处理完成")
        return True

    if op == ".more":
        success, feedback, new_fwd_id = await handle_more_command(
            gid, None, qq, operator_uid=event.user_id, via="group_qq",
        )
        if success:
            cfg.last_command_time[key] = now
        if new_fwd_id:
            track_forward_message(new_fwd_id, [qq], gid)
            asyncio.create_task(add_emoji_to_message(new_fwd_id, action_emoji_ids()))
        if feedback:
            asyncio.create_task(send_and_track_feedback(gid, msg_id, feedback, qq))
        _log_group_command(event, op, "处理完成" if success else "处理失败")
        return True

    return True


async def format_friend_stats(count: str) -> str:
    if not count:
        limit = 7
    elif count.isascii() and count.isdecimal() and len(count) <= 2 and 1 <= int(count) <= 30:
        limit = int(count)
    else:
        return "用法：.watch friends [条数]，可查询 1 到 30 条，默认 7 条。"

    try:
        records = await storage.recent_friend_stats(limit)
    except SQLiteError:
        log.exception("读取好友变化记录失败")
        return "好友变化记录暂时无法读取，请稍后重试。"
    if not records:
        return "暂无好友变化记录，首次晨报统计完成后可查询。"

    lines = ["好友变化表（最近在前）", "日期 | 新增 | 减少 | 净增减 | 总数"]
    for day, added, removed, total in records:
        change = added - removed
        lines.append(f"{date.fromisoformat(day)} | {added} | {removed} | {change:+d} | {total}")
    return "\n".join(lines)


async def format_group_stats(args: list[str]) -> str:
    usage = "用法：.watch groups <群号或级联名称> [条数]，默认 7 条，最多 30 条。"
    if not args:
        groups = "、".join(f"{name}（{group_id}）" for name, group_id in cfg.MONITORED_GROUPS.items()) or "暂无"
        cascades = "、".join(cfg.MONITORED_GROUP_CASCADES) or "暂无"
        return f"正在监控的群：{groups}\n级联统计：{cascades}\n{usage}"
    target = " ".join(args)
    count = "7"
    if target not in cfg.MONITORED_GROUP_CASCADES and len(args) > 1:
        target, count = " ".join(args[:-1]), args[-1]
    if not count.isascii() or not count.isdecimal() or len(count) > 2 or not 1 <= int(count) <= 30:
        return usage
    key: int | str
    if target in cfg.MONITORED_GROUP_CASCADES:
        key = storage.cascade_key(cfg.MONITORED_GROUP_CASCADES[target])
        label = target
    elif target.isascii() and target.isdecimal() and len(target) <= 19 and 0 < int(target) < 2**63:
        key = int(target)
        name = next((name for name, group_id in cfg.MONITORED_GROUPS.items() if group_id == key), "群聊")
        label = f"{name}（{key}）"
    else:
        return usage

    try:
        records = await storage.recent_group_stats(key, int(count))
    except SQLiteError:
        log.exception("读取群人数记录失败: %s", label)
        return "群人数记录暂时无法读取，请稍后重试。"
    if not records:
        return f"{label} 暂无人数记录。"

    lines = [f"{label}人数变化表", "日期 | 新增 | 减少 | 当前数量 | 净改变"]
    for day, total, change, added, removed in records:
        counts = [str(value) if value is not None else "–" for value in (added, removed, total)]
        delta = f"{change:+d}" if change is not None else "–"
        lines.append(" | ".join((str(date.fromisoformat(day)), *counts, delta)))
    return "\n".join(lines)


async def watch_chart_message(args: list[str]) -> MessageSegment:
    count = args[0] if args else "7"
    if len(args) > 1 or not count.isascii() or not count.isdecimal() or len(count) > 2 or not 1 <= int(count) <= 30:
        return MessageSegment.text("用法：.watch chart [天数]，默认 7 天，最多 30 天。")
    try:
        image = await build_watch_chart(int(count))
    except Exception:
        log.exception("生成监控图片报表失败")
        return MessageSegment.text("暂时无法生成监控图片报表，请稍后重试。")
    if image is None:
        return MessageSegment.text("暂无监控记录可生成图片报表。")
    return image


async def handle_group_command(event: GroupMessageEvent) -> bool:
    """处理内部群命令消息"""
    gid = event.group_id
    msg_id = event.message_id

    if gid != cfg.INTERNAL_GROUP_ID or event.user_id == cfg.client.self_id:
        return False

    # 通知指令独立于客户会话，必须先处理，避免被 .say 等待输入当成回复内容。
    if await handle_notice_command(event):
        return True

    # 解析引用和命令
    reply_id = None
    cmd_parts: list[str] = []
    for seg in event.message:
        if seg.type == "reply" and seg.data.get("id") is not None:
            reply_id = int(seg.data.get("id"))
        elif seg.type == "text":
            cmd_parts.append(seg.data["text"])

    cmd_text = ''.join(cmd_parts).strip()
    log.debug("处理群消息: group_id=%d message_id=%d reply_id=%s", gid, msg_id, reply_id)

    command, *watch_args = cmd_text.split() or [""]
    if command == ".watch":
        subcommand, *args = watch_args or [""]
        command = f".watch {subcommand}" if subcommand in ("friends", "groups", "chart") else ".watch"
        _log_group_command(event, command, "开始处理")
        if subcommand == "friends":
            result = MessageSegment.text(await format_friend_stats(" ".join(args)))
        elif subcommand == "groups":
            result = MessageSegment.text(await format_group_stats(args))
        elif subcommand == "chart":
            result = await watch_chart_message(args)
        else:
            result = MessageSegment.text(
                "监控查询\n"
                ".watch friends [条数] 查看好友变化表\n"
                ".watch groups 列出监控群\n"
                ".watch groups <群号或级联名称> [条数] 查看人数变化表\n"
                ".watch chart [天数] 发送综合图片报表"
            )
        await cfg.client.call_api(
            "send_group_msg",
            group_id=str(gid),
            message=[MessageSegment.reply(str(msg_id)), result],
        )
        _log_group_command(event, command, "已回复")
        return True

    op = next((prefix for prefix in OPS_COMMAND_PREFIXES if cmd_text.startswith(prefix)), None)

    # 检查是否处于等待 .say 内容的状态：收到消息立即发送
    pending = cfg.pending_say.get(event.user_id)
    if pending and asyncio.get_running_loop().time() >= pending["expires_at"]:
        await expire_pending_say(event.user_id, pending)
    if event.user_id in cfg.pending_say and op is None:
        _log_group_command(event, ".say", "收到后续内容")
        segments = extract_sendable_segments(event.message)
        if not segments:
            await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[MessageSegment.reply(str(msg_id)), MessageSegment.text("⚠️ 消息内容为空，已忽略。")],
            )
            _log_group_command(event, ".say", "忽略空内容")
            return True

        pending = pop_pending_say(event.user_id)
        targets: list[int] = list(pending.get("customer_ids") or [])
        if not targets and pending.get("customer_id") is not None:
            targets = [int(pending["customer_id"])]
        orig_reply_id = pending.get("reply_id")
        via = pending.get("via", "group_reply")
        debounce_key = pending.get("debounce_key") or (orig_reply_id, "say")

        if len(targets) == 1:
            customer_id = targets[0]
            feedback, _closed, customer_msg_id = await send_private_and_close(
                customer_id, segments,
                operator_uid=event.user_id,
                via=via,
                group_id=gid,
                reply_id=orig_reply_id,
            )
            if not feedback.startswith("❌"):
                cfg.last_command_time[debounce_key] = time.time()
            await send_say_feedback(
                gid, msg_id, customer_id, event.user_id, feedback, customer_msg_id,
            )
            _log_group_command(event, ".say", "处理失败" if feedback.startswith("❌") else "处理完成")
            return True

        # 多客户 pending（emoji 多客户合并转发 / 后续扩展）
        ok, failed = await apply_session_op_to_customers(
            "say", targets,
            gid=gid,
            operator_uid=event.user_id,
            via=via,
            reply_id=orig_reply_id,
            segments=segments,
        )
        if ok:
            cfg.last_command_time[debounce_key] = time.time()
        summary = format_batch_result("say", ok, failed)
        await cfg.client.call_api(
            "send_group_msg",
            group_id=str(gid),
            message=[MessageSegment.reply(str(msg_id)), MessageSegment.text(summary)],
        )
        _log_group_command(event, ".say", f"批量处理结束（成功 {ok}，失败 {len(failed)}）")
        return True

    if op is None:
        return True

    remote_reload = op in (".reload", ".update") and cmd_text[len(op):].strip() == "remote"
    _log_group_command(event, op, "开始远程更新" if remote_reload else "开始处理")

    if op == ".help":
        help_text = (
            "📖 可用命令列表：\n"
            "• .say <内容> – 向客户发送私聊消息（不带内容则等待下一条消息）\n"
            "• .say <QQ|all> <内容> – 不引用时按 QQ/队列发送；无参且队列仅 1 人时自动匹配\n"
            "• .bye / .bye <QQ|all> – 发送结束语并关闭会话（无参且队列仅 1 人时自动匹配）\n"
            "• .close / .close <QQ|all> – 关闭会话但不发送结束语（无参自动匹配同上）\n"
            "• .more / .more <QQ> – 获取历史合并转发；**不支持 .more all**；无参且队列仅 1 人时自动匹配\n"
            "• .list – 列出所有未回复客户及其等待时间\n"
            "• .status – 查看运行状态面板\n"
            "• .notice n – 将通知群最近共 n 条消息合并创建一个通知 PR（n 为 1–100）\n"
            "• .mute [分钟] – 临时静音（不带参数不限时；如 .mute 30 为 30 分钟）\n"
            "• .unmute – 解除静音，并汇总发出延后提醒\n"
            "• .reload – 重新加载本地配置\n"
            "• .reload remote – 获取并应用远程更新\n"
            "• .reboot [slient] – 保存状态并重启；slient 跳过重启后的启动群通知\n"
            "• .help – 显示此帮助信息\n"
            "• .watch friends [条数] – 查看好友变化表，默认最近 7 条，最多 30 条\n"
            "• .watch groups [群号或级联名称] [条数] – 查看监控列表或人数变化表\n"
            "• .watch chart [天数] – 发送好友和群人数综合图片报表，默认 7 天\n"
            "\n"
            "使用方法：优先回复机器人消息；或 .命令 <客户QQ|all>；无参且待回复仅 1 人时自动匹配该客户。\n"
            "说明：all = 当前待回复队列；.say all 必须带内容；群内不允许 .more all。\n"
            "表情：多客户合并转发上 say/bye/close 作用域为该消息内全部客户；more 仅单客户。"
        )
        await cfg.client.call_api(
            "send_group_msg",
            group_id=str(gid),
            message=[MessageSegment.text(help_text)],
        )
        _log_group_command(event, op, "已回复")
        return True

    elif op == ".status":
        sent = await send_status_panel(gid)
        _log_group_command(event, op, "已回复" if sent else "回复失败")
        return True

    elif op == ".unmute":
        key = (msg_id, "unmute")
        now_ts = time.time()
        debounce_sec = cfg.DEBOUNCE_SECONDS.get("unmute", cfg.DEBOUNCE_SECONDS.get("close", 5))
        last_time = cfg.last_command_time.get(key, 0.0)
        if now_ts - last_time < debounce_sec:
            await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[MessageSegment.reply(str(msg_id)), MessageSegment.text(f"⏳ 操作过于频繁，请 {debounce_sec:.0f} 秒后重试。")],
            )
            _log_group_command(event, op, "触发频率限制")
            return True
        cfg.last_command_time[key] = now_ts

        # 与终端 unmute 共用同一套业务逻辑
        _was, _flushed, text = await mute.unmute_and_flush()
        await cfg.client.call_api(
            "send_group_msg",
            group_id=str(gid),
            message=[MessageSegment.reply(str(msg_id)), MessageSegment.text("✅ " + text)],
        )
        _log_group_command(event, op, "已解除静音")
        return True

    elif op == ".mute":
        key = (msg_id, "mute")
        now_ts = time.time()
        debounce_sec = cfg.DEBOUNCE_SECONDS.get("mute", cfg.DEBOUNCE_SECONDS.get("close", 5))
        last_time = cfg.last_command_time.get(key, 0.0)
        if now_ts - last_time < debounce_sec:
            await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[MessageSegment.reply(str(msg_id)), MessageSegment.text(f"⏳ 操作过于频繁，请 {debounce_sec:.0f} 秒后重试。")],
            )
            _log_group_command(event, op, "触发频率限制")
            return True
        cfg.last_command_time[key] = now_ts

        arg = cmd_text[len(".mute"):].strip()
        # 无参数 = 不限时静音；有参数 = 限时分钟数
        minutes: float | None = None
        if arg:
            try:
                minutes = float(arg)
            except ValueError:
                await cfg.client.call_api(
                    "send_group_msg",
                    group_id=str(gid),
                    message=[MessageSegment.reply(str(msg_id)), MessageSegment.text(f"❌ 无效时长：{arg}，示例：.mute 30；直接发送 .mute 表示不限时")],
                )
                _log_group_command(event, op, "参数无效")
                return True
            if minutes <= 0:
                await cfg.client.call_api(
                    "send_group_msg",
                    group_id=str(gid),
                    message=[MessageSegment.reply(str(msg_id)), MessageSegment.text("❌ 静音时长必须大于 0 分钟；不限时请直接发送 .mute")],
                )
                _log_group_command(event, op, "参数无效")
                return True

        until = mute.set_mute(minutes)
        if minutes is None:
            text = (
                "🔕 已开启不限时静音（直到 .unmute）。\n"
                "期间新客户提醒与里程碑催办将暂存；解除静音后汇总发出。"
            )
        else:
            until_str = time.strftime("%H:%M:%S", time.localtime(until))
            text = (
                f"🔕 已开启临时静音 {minutes:g} 分钟（至 {until_str}）。\n"
                "期间新客户提醒与里程碑催办将暂存；解除静音或到期后汇总发出。"
            )
        await cfg.client.call_api(
            "send_group_msg",
            group_id=str(gid),
            message=[MessageSegment.reply(str(msg_id)), MessageSegment.text(text)],
        )
        _log_group_command(event, op, "已开启静音")
        return True

    elif op == ".list":
        now = time.time()
        key = (msg_id, "list")
        debounce_list = cfg.DEBOUNCE_SECONDS.get("list", 5)
        last_time = cfg.last_command_time.get(key, 0.0)
        if now - last_time < debounce_list:
            await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[
                    MessageSegment.reply(str(msg_id)),
                    MessageSegment.text(f"⏳ 操作过于频繁，请 {debounce_list:.0f} 秒后重试。")
                ],
            )
            _log_group_command(event, op, "触发频率限制")
            return True

        if not cfg.unreplied_customers:
            await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[MessageSegment.reply(str(msg_id)), MessageSegment.text("📭 当前没有待回复的客户。")],
            )
            _log_group_command(event, op, "已回复空队列")
            return True

        sorted_customers = sorted(
            cfg.unreplied_customers.items(),
            key=lambda item: item[1]["last_active"],
            reverse=True
        )
        customer_ids: list[int] = [qq for qq, _ in sorted_customers]
        nicknames: dict[int, str] = await get_nicknames_batch(customer_ids)

        lines: list[str] = ["📋 待回复客户列表："]
        now_ts = time.time()
        for idx, (qq, data) in enumerate(sorted_customers, 1):
            nickname = nicknames.get(qq, "未知昵称")
            wait_seconds = now_ts - data["pending_since"]
            wait_str = format_duration(wait_seconds)
            lines.append(f"{idx}. {nickname}（{qq}）已等待 {wait_str}")

        if len(lines) > 22:
            full_text = "\n".join(lines[:21]) + f"\n... 共 {len(customer_ids)} 人，仅显示前20条"
        else:
            full_text = "\n".join(lines)

        try:
            resp = await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[MessageSegment.reply(str(msg_id)), MessageSegment.text(full_text)],
            )
            feedback_msg_id = resp.get("message_id")
            if feedback_msg_id:
                track_forward_message(feedback_msg_id, [], gid)
            cfg.last_command_time[key] = now
            _log_group_command(event, op, f"已回复（{len(customer_ids)} 名客户）")
        except Exception:
            log.exception("发送 .list 结果失败")
        return True

    elif op == ".reboot":
        from . import main as app_main
        reboot_parts = cmd_text.split()
        valid_reboot = reboot_parts in ([".reboot"], [".reboot", "slient"])
        if not valid_reboot:
            message = "❌ 用法：.reboot [slient]"
        else:
            message = app_main.reboot_block_reason() or "🔄 正在重启机器人…"
        await cfg.client.call_api(
            "send_group_msg",
            group_id=str(gid),
            message=[MessageSegment.reply(str(msg_id)), MessageSegment.text(message)],
        )
        accepted = False
        if valid_reboot and not message.startswith("❌"):
            accepted = app_main.request_reboot(
                f"group:{gid}:user:{event.user_id}", silent=reboot_parts[1:] == ["slient"],
            )
        _log_group_command(event, op, "已请求重启" if accepted else "重启请求被拒绝")
        return True

    elif op in (".reload", ".update"):
        arg = cmd_text[len(op):].strip()
        # .reload / .update 可不带参数；cfg 为兼容旧写法
        if arg not in ("", "cfg", "remote"):
            await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[MessageSegment.reply(str(msg_id)), MessageSegment.text("❌ 用法：.reload [cfg|remote]")],
            )
            _log_group_command(event, op, "参数无效")
            return True

        async def on_started() -> None:
            try:
                await _send_reload_feedback(
                    gid, msg_id,
                    "⏳ 已开始远程更新，请稍候。",
                )
            except Exception:
                # 开始回执发送失败不取消已受理的更新，结束后仍尝试发送结果。
                pass

        if arg == "remote":
            success, update_message = await cfg.run_reload_cfg(remote=True, on_started=on_started)
        else:
            success, update_message = await cfg.run_reload_cfg()
        await _send_reload_feedback(gid, msg_id, update_message)
        _log_group_command(event, op, "配置更新完成" if success else "配置更新失败")
        return True

    if reply_id is None:
        return await handle_session_command_by_qq(event, gid, msg_id, op, cmd_text[len(op):].strip())

    customer_ids, err_msg = await resolve_target_from_reply(reply_id)
    if err_msg:
        await cfg.client.call_api(
            "send_group_msg",
            group_id=str(gid),
            message=[MessageSegment.reply(str(msg_id)), MessageSegment.text(err_msg)],
        )
        _log_group_command(event, op, "无法解析引用目标")
        return True

    if not customer_ids:
        await cfg.client.call_api(
            "send_group_msg",
            group_id=str(gid),
            message=[MessageSegment.reply(str(msg_id)), MessageSegment.text("无法确定要处理的客户，请使用命令指定客户 QQ。")],
        )
        _log_group_command(event, op, "缺少客户目标")
        return True

    if len(customer_ids) != 1:
        await cfg.client.call_api(
            "send_group_msg",
            group_id=str(gid),
            message=[MessageSegment.reply(str(msg_id)), MessageSegment.text("暂不支持：该合并转发包含多个客户，请手动处理。")],
        )
        _log_group_command(event, op, "引用包含多个客户")
        return True

    customer_id = customer_ids[0]
    now = time.time()

    # ---- .say ----
    if op == ".say":
        segments = extract_sendable_segments(event.message, strip_prefix=".say")
        if not segments:
            # 无内容：进入等待模式，收集用户后续消息
            resp = await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[MessageSegment.reply(str(msg_id)), MessageSegment.text("📝 请发送要回复给客户的消息内容：")],
            )
            prompt_msg_id = resp.get("message_id")
            register_pending_say(event.user_id, {
                "prompt_msg_id": prompt_msg_id,
                "customer_id": customer_id,
                "reply_id": reply_id,
                "group_id": gid,
                "via": "group_reply",
                "target_source": "reply",
                "debounce_key": (reply_id, "say"),
            })
            await history.record_command(
                customer_id, event.user_id, "say",
                mode="pending", reply_id=reply_id, group_id=gid, via="group_reply",
            )
            if prompt_msg_id:
                await add_emoji_to_message(prompt_msg_id, [
                    cfg.EMOJI_MAPPING["cancel"],
                ])
            _log_group_command(event, op, "等待回复内容")
            return True

        key = (reply_id, "say")
        last_time = cfg.last_command_time.get(key, 0)
        if now - last_time < cfg.DEBOUNCE_SECONDS["say"]:
            await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[
                    MessageSegment.reply(str(msg_id)),
                    MessageSegment.text(f"⏳ 操作过于频繁，请 {cfg.DEBOUNCE_SECONDS['say']} 秒后重试。")
                ],
            )
            _log_group_command(event, op, "触发频率限制")
            return True

        feedback, _closed, customer_msg_id = await send_private_and_close(
            customer_id, segments,
            operator_uid=event.user_id,
            via="group_reply",
            group_id=gid,
            reply_id=reply_id,
        )
        if not feedback.startswith("❌"):
            cfg.last_command_time[key] = now

        await send_say_feedback(
            gid, msg_id, customer_id, event.user_id, feedback, customer_msg_id,
        )
        _log_group_command(event, op, "处理失败" if feedback.startswith("❌") else "处理完成")
        return True

    # ---- .bye ----
    elif op == ".bye":
        key = (reply_id, "bye")
        last_time = cfg.last_command_time.get(key, 0)
        if now - last_time < cfg.DEBOUNCE_SECONDS["bye"]:
            await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[
                    MessageSegment.reply(str(msg_id)),
                    MessageSegment.text(f"⏳ 操作过于频繁，请 {cfg.DEBOUNCE_SECONDS['bye']} 秒后重试。")
                ],
            )
            _log_group_command(event, op, "触发频率限制")
            return True

        feedback = await handle_bye_command(gid, msg_id, customer_id, operator_uid=event.user_id, via="group_reply")
        if not feedback.startswith("❌"):
            cfg.last_command_time[key] = now
        asyncio.create_task(send_and_track_feedback(gid, msg_id, feedback, customer_id))
        _log_group_command(event, op, "处理失败" if feedback.startswith("❌") else "处理完成")
        return True

    # ---- .close ----
    elif op == ".close":
        key = (reply_id, "close")
        last_time = cfg.last_command_time.get(key, 0)
        debounce_sec = cfg.DEBOUNCE_SECONDS.get("close", 5)
        if now - last_time < debounce_sec:
            await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[
                    MessageSegment.reply(str(msg_id)),
                    MessageSegment.text(f"⏳ 操作过于频繁，请 {debounce_sec} 秒后重试。")
                ],
            )
            _log_group_command(event, op, "触发频率限制")
            return True

        feedback = await handle_close_command(gid, msg_id, customer_id, operator_uid=event.user_id, via="group_reply")
        if not feedback.startswith("❌"):
            cfg.last_command_time[key] = now
        asyncio.create_task(send_and_track_feedback(gid, msg_id, feedback, customer_id))
        _log_group_command(event, op, "处理失败" if feedback.startswith("❌") else "处理完成")
        return True

    # ---- .more ----
    elif op == ".more":
        key = (reply_id, "more")
        last_time = cfg.last_command_time.get(key, 0)
        if now - last_time < cfg.DEBOUNCE_SECONDS["more"]:
            await cfg.client.call_api(
                "send_group_msg",
                group_id=str(gid),
                message=[
                    MessageSegment.reply(str(msg_id)),
                    MessageSegment.text(f"⏳ 操作过于频繁，请 {cfg.DEBOUNCE_SECONDS['more']} 秒后重试。")
                ],
            )
            _log_group_command(event, op, "触发频率限制")
            return True

        success, feedback, new_fwd_id = await handle_more_command(
            gid, msg_id, customer_id, operator_uid=event.user_id, via="group_reply",
        )
        if success:
            cfg.last_command_time[key] = now
            if new_fwd_id:
                track_forward_message(new_fwd_id, [customer_id], gid)
                asyncio.create_task(add_emoji_to_message(new_fwd_id, action_emoji_ids()))
        if feedback:
            asyncio.create_task(send_and_track_feedback(gid, msg_id, feedback, customer_id))
        _log_group_command(event, op, "处理完成" if success else "处理失败")
        return True

    else:
        return True

