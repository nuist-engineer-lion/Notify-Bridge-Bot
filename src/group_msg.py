import time
import asyncio

from napcat import (
    GroupMsgEmojiLikeEvent,
    GroupMessageEvent,
    GroupPokeEvent,
    Reply,
    Text,
    Message,
)

from .config import (
    log,
    INTERNAL_GROUP_ID,
    CLOSING_MESSAGE,
    DEBOUNCE_SECONDS,
    EMOJI_MAPPING,
    EMOJI_TO_CMD,
    monitored_forwards,
    unreplied_customers,
    last_command_time,
    pending_say,
    client,
)
from .utils import format_duration
from .message_sender import (
    close_session,
    send_status_panel,
    send_forward_from_message_ids,
    send_and_track_feedback,
    track_forward_message,
    add_emoji_to_message,
)


# ======================= 昵称批量获取 =======================

async def get_nicknames_batch(user_ids: list[int], delay: float = 0.2) -> dict[int, str]:
    """
    批量获取用户昵称，返回 {qq: nickname} 映射。
    逐次调用 get_stranger_info 并加入延迟以避免限流。
    """
    nicknames: dict[int, str] = {}
    for uid in user_ids:
        try:
            info = await client.get_stranger_info(user_id=str(uid))
            nickname = info.get("nickname", "未知昵称")
            nicknames[uid] = str(nickname)
        except Exception as e:
            log.error("获取用户 %d 昵称失败: %s", uid, e)
            nicknames[uid] = "获取失败"
        await asyncio.sleep(delay)
    return nicknames


# ======================= 命令处理辅助 =======================

def extract_sendable_segments(message: list[Message], strip_prefix: str | None = None) -> list[Message]:
    """从群消息中提取可私聊发送的消息段（去掉 Reply），可选去掉首个 Text 段的命令前缀。"""
    segments: list[Message] = []
    for seg in message:
        if isinstance(seg, Reply):
            continue
        if isinstance(seg, Text) and strip_prefix:
            text = seg.text
            if text.startswith(strip_prefix):
                text = text[len(strip_prefix):]
                if not text:
                    continue
            segments.append(Text(text=text))
        else:
            segments.append(seg)
    return segments


async def resolve_target_from_reply(reply_id: int) -> tuple[list[int] | None, str | None]:
    """
    根据被引用的消息ID解析对应的客户列表。
    返回 (customer_ids, error_message)，若成功则 error_message 为 None。
    """
    data = monitored_forwards.get(reply_id)
    if data is not None:
        return data["customer_ids"], None

    try:
        msg_detail = await client.get_msg(message_id=str(reply_id))
        sender_id = int(msg_detail.get("user_id", 0))
        self_id = int(client.self_id)
        if sender_id == self_id:
            return None, "操作已过期"
        else:
            return None, "无法识别的消息"
    except Exception as e:
        log.error("查询消息详情失败: reply_id=%s, err=%s", reply_id, e)
        return None, "查询消息失败"


async def handle_bye_command(gid: int, reply_id: int, customer_id: int) -> str:
    """执行 .bye 命令：发送结束语并关闭会话，返回反馈文本"""
    try:
        await client.send_private_msg(
            user_id=str(customer_id),
            message=CLOSING_MESSAGE,
        )
        closed = await close_session(customer_id, send_closing=False)
        return f"✅ 已向客户 {customer_id} 发送结束语。" + ("（客户已在待回复队列）" if closed else "（客户不在待回复队列）")
    except Exception as e:
        log.error("发送结束语失败: customer=%s, err=%s", customer_id, e, exc_info=True)
        return f"❌ 发送失败：{e}"


async def handle_close_command(gid: int, reply_id: int, customer_id: int) -> str:
    """执行 .close 命令：仅关闭会话，不发送结束语，返回反馈文本"""
    try:
        closed = await close_session(customer_id, send_closing=False)
        return f"✅ 已关闭客户 {customer_id} 的会话（未发送结束语）。" + ("（客户已在待回复队列）" if closed else "（客户不在待回复队列）")
    except Exception as e:
        log.error("关闭会话失败: customer=%s, err=%s", customer_id, e, exc_info=True)
        return f"❌ 关闭失败：{e}"


async def handle_more_command(gid: int, reply_id: int, customer_id: int) -> tuple[bool, str | None, int | None]:
    """
    执行 .more 命令：获取历史消息并发送合并转发。
    返回 (成功标志, 反馈文本或 None, 新合并转发的消息ID或 None)
    """
    try:
        resp = await client.get_friend_msg_history(
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
        )
        if new_fwd_id:
            return True, None, new_fwd_id
        else:
            return False, f"❌ 构造合并转发失败", None
    except Exception as e:
        log.error("获取历史消息失败: customer=%s, err=%s", customer_id, e, exc_info=True)
        return False, f"❌ 获取历史消息失败：{e}", None


# ======================= 群事件处理 =======================

async def handle_group_emoji(event: GroupMsgEmojiLikeEvent) -> bool:
    """处理内部群表情贴纸操作：匹配表情到对应命令"""
    gid = event.group_id
    mid = event.message_id

    if gid != INTERNAL_GROUP_ID or event.user_id == client.self_id:
        return False

    # 动态提取表情 ID
    eid = None
    try:
        likes = getattr(event, 'likes', [])
        if likes and len(likes) > 0:
            first = likes[0]
            for field in ('emoji_id', 'face_id', 'id'):
                if hasattr(first, field):
                    eid = getattr(first, field, None)
                    break
            if eid is None and isinstance(first, dict):
                eid = first.get('emoji_id') or first.get('face_id') or first.get('id')
            if eid is not None:
                eid = int(eid)
    except Exception:
        pass

    is_add = bool(getattr(event, 'is_add', True))
    if eid is None or not is_add:
        return True

    tracked_data = monitored_forwards.get(mid)
    if tracked_data is None:
        return True

    customer_ids = tracked_data["customer_ids"]
    if len(customer_ids) != 1:
        return True

    customer_id = customer_ids[0]
    cmd = EMOJI_TO_CMD.get(eid)
    if cmd is None:
        return True

    try:
        if cmd == "say":
            pending_say[event.user_id] = (customer_id, mid, gid)
            await client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(mid)), Text(text="📝 请发送要回复给客户的消息内容：")],
            )
        elif cmd == "close":
            feedback = await handle_close_command(gid, mid, customer_id)
            await send_and_track_feedback(gid, mid, feedback, customer_id)
        elif cmd == "bye":
            feedback = await handle_bye_command(gid, mid, customer_id)
            await send_and_track_feedback(gid, mid, feedback, customer_id)
        elif cmd == "more":
            success, feedback, new_fwd_id = await handle_more_command(gid, mid, customer_id)
            if success:
                if new_fwd_id:
                    track_forward_message(new_fwd_id, [customer_id], gid)
                    await add_emoji_to_message(new_fwd_id, list(EMOJI_MAPPING.values()))
                if feedback:
                    await send_and_track_feedback(gid, mid, feedback, customer_id)
            else:
                if feedback:
                    await send_and_track_feedback(gid, mid, feedback, customer_id)
    except Exception:
        pass
    return True


async def handle_group_poke(event: GroupPokeEvent) -> bool:
    """处理内部群戳一戳：发送状态面板"""
    if event.group_id == INTERNAL_GROUP_ID and event.target_id == client.self_id:
        log.info("内部群戳一戳触发状态面板: group=%d", event.group_id)
        await send_status_panel(event.group_id)
        return True
    return False


async def handle_group_command(event: GroupMessageEvent) -> bool:
    """处理内部群命令消息"""
    gid = event.group_id
    msg_id = event.message_id

    if gid != INTERNAL_GROUP_ID or event.user_id == client.self_id:
        return False

    # 解析引用和命令
    reply_id = None
    cmd_parts: list[str] = []
    for seg in event.message:
        if isinstance(seg, Reply) and seg.id is not None:
            reply_id = int(seg.id)
        elif isinstance(seg, Text):
            cmd_parts.append(seg.text)

    cmd_text = ''.join(cmd_parts).strip()
    log.debug("群命令: reply_id=%s, cmd=%s", reply_id, cmd_text)

    # 检查是否处于等待 .say 内容的状态
    if event.user_id in pending_say and not any(cmd_text.startswith(prefix) for prefix in ('.say', '.bye', '.more', '.help', '.close', '.list')):
        customer_id, orig_reply_id, _ = pending_say.pop(event.user_id)
        segments = extract_sendable_segments(event.message)
        if not segments:
            await client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(msg_id)), Text(text="⚠️ 消息内容为空，已取消发送。")],
            )
            return True

        now = time.time()
        key = (orig_reply_id, "say")
        try:
            await client.send_private_msg(
                user_id=str(customer_id),
                message=segments,
            )
            closed = await close_session(customer_id, send_closing=False)
            last_command_time[key] = now
            feedback = f"✅ 已向客户 {customer_id} 发送消息。" + ("（客户已在待回复队列）" if closed else "（客户不在待回复队列）")
        except Exception as e:
            log.error("发送私聊消息失败: customer=%s, err=%s", customer_id, e, exc_info=True)
            feedback = f"❌ 发送失败：{e}"

        resp = await client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(msg_id)), Text(text=feedback)],
        )
        feedback_msg_id = resp.get("message_id")
        if feedback_msg_id:
            track_forward_message(feedback_msg_id, [customer_id], gid)
        return True

    if not any(cmd_text.startswith(prefix) for prefix in ('.say', '.bye', '.more', '.help', '.close', '.list')):
        return True

    if cmd_text.startswith(".help"):
        help_text = (
            "📖 可用命令列表：\n"
            "• .say <内容> – 向客户发送私聊消息（不带内容则等待下一条消息）\n"
            "• .bye – 向客户发送结束语并关闭会话\n"
            "• .close – 关闭会话但不发送结束语\n"
            "• .more – 获取客户的最近100条历史消息\n"
            "• .list – 列出所有未回复客户及其等待时间\n"
            "• .help – 显示此帮助信息\n"
            "\n"
            "使用方法：回复一条合并转发消息，然后输入对应命令。"
        )
        await client.send_group_msg(
            group_id=str(gid),
            message=[Text(text=help_text)],
        )
        return True

    elif cmd_text.startswith(".list"):
        now = time.time()
        key = (msg_id, "list")
        debounce_list = DEBOUNCE_SECONDS.get("list", 5)
        last_time = last_command_time.get(key, 0.0)
        if now - last_time < debounce_list:
            await client.send_group_msg(
                group_id=str(gid),
                message=[
                    Reply(id=str(msg_id)),
                    Text(text=f"⏳ 操作过于频繁，请稍后再试（防抖 {debounce_list:.0f} 秒）")
                ],
            )
            return True

        if not unreplied_customers:
            await client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(msg_id)), Text(text="📭 当前没有待回复的客户。")],
            )
            return True

        sorted_customers = sorted(
            unreplied_customers.items(),
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
            resp = await client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(msg_id)), Text(text=full_text)],
            )
            feedback_msg_id = resp.get("message_id")
            if feedback_msg_id:
                track_forward_message(feedback_msg_id, [], gid)
            last_command_time[key] = now
            log.info(".list 命令执行成功，返回 %d 名客户", len(customer_ids))
        except Exception as e:
            log.error("发送 .list 结果失败: %s", e, exc_info=True)
        return True

    if reply_id is None or not cmd_parts:
        return True

    customer_ids, err_msg = await resolve_target_from_reply(reply_id)
    if err_msg:
        await client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(msg_id)), Text(text=err_msg)],
        )
        return True

    if not customer_ids:
        await client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(msg_id)), Text(text="内部错误：无法解析客户列表")],
        )
        return True

    if len(customer_ids) != 1:
        await client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(msg_id)), Text(text="暂不支持：该合并转发包含多个客户，请手动处理。")],
        )
        return True

    customer_id = customer_ids[0]
    now = time.time()

    # ---- .say ----
    if cmd_text.startswith(".say"):
        segments = extract_sendable_segments(event.message, strip_prefix=".say")
        if not segments:
            # 无内容：进入等待模式，侦听用户下一条消息
            pending_say[event.user_id] = (customer_id, reply_id, gid)
            await client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(msg_id)), Text(text="📝 请发送要回复给客户的消息内容：")],
            )
            return True

        key = (reply_id, "say")
        last_time = last_command_time.get(key, 0)
        if now - last_time < DEBOUNCE_SECONDS["say"]:
            await client.send_group_msg(
                group_id=str(gid),
                message=[
                    Reply(id=str(msg_id)),
                    Text(text=f"⏳ 操作过于频繁，请稍后再试（防抖 {DEBOUNCE_SECONDS['say']} 秒）")
                ],
            )
            return True

        try:
            await client.send_private_msg(
                user_id=str(customer_id),
                message=segments,
            )
            closed = await close_session(customer_id, send_closing=False)
            last_command_time[key] = now
            feedback = f"✅ 已向客户 {customer_id} 发送消息。" + ("（客户已在待回复队列）" if closed else "（客户不在待回复队列）")
        except Exception as e:
            log.error("发送私聊消息失败: customer=%s, err=%s", customer_id, e, exc_info=True)
            feedback = f"❌ 发送失败：{e}"

        resp = await client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(msg_id)), Text(text=feedback)],
        )
        feedback_msg_id = resp.get("message_id")
        if feedback_msg_id:
            track_forward_message(feedback_msg_id, [customer_id], gid)
        return True

    # ---- .bye ----
    elif cmd_text.startswith(".bye"):
        key = (reply_id, "bye")
        last_time = last_command_time.get(key, 0)
        if now - last_time < DEBOUNCE_SECONDS["bye"]:
            await client.send_group_msg(
                group_id=str(gid),
                message=[
                    Reply(id=str(msg_id)),
                    Text(text=f"⏳ 操作过于频繁，请稍后再试（防抖 {DEBOUNCE_SECONDS['bye']} 秒）")
                ],
            )
            return True

        feedback = await handle_bye_command(gid, msg_id, customer_id)
        last_command_time[key] = now
        asyncio.create_task(send_and_track_feedback(gid, msg_id, feedback, customer_id))
        return True

    # ---- .close ----
    elif cmd_text.startswith(".close"):
        key = (reply_id, "close")
        last_time = last_command_time.get(key, 0)
        debounce_sec = DEBOUNCE_SECONDS.get("close", 5)
        if now - last_time < debounce_sec:
            await client.send_group_msg(
                group_id=str(gid),
                message=[
                    Reply(id=str(msg_id)),
                    Text(text=f"⏳ 操作过于频繁，请稍后再试（防抖 {debounce_sec} 秒）")
                ],
            )
            return True

        feedback = await handle_close_command(gid, msg_id, customer_id)
        last_command_time[key] = now
        asyncio.create_task(send_and_track_feedback(gid, msg_id, feedback, customer_id))
        return True

    # ---- .more ----
    elif cmd_text.startswith(".more"):
        key = (reply_id, "more")
        last_time = last_command_time.get(key, 0)
        if now - last_time < DEBOUNCE_SECONDS["more"]:
            await client.send_group_msg(
                group_id=str(gid),
                message=[
                    Reply(id=str(msg_id)),
                    Text(text=f"⏳ 操作过于频繁，请稍后再试（防抖 {DEBOUNCE_SECONDS['more']} 秒）")
                ],
            )
            return True

        success, feedback, new_fwd_id = await handle_more_command(gid, msg_id, customer_id)
        if success:
            last_command_time[key] = now
            if new_fwd_id:
                track_forward_message(new_fwd_id, [customer_id], gid)
                asyncio.create_task(add_emoji_to_message(new_fwd_id, list(EMOJI_MAPPING.values())))
            if feedback:
                asyncio.create_task(send_and_track_feedback(gid, msg_id, feedback, customer_id))
        else:
            if feedback:
                asyncio.create_task(send_and_track_feedback(gid, msg_id, feedback, customer_id))
        return True

    else:
        return True

