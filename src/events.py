import time
import asyncio

from napcat import (
    FriendRequestEvent,
    GroupMsgEmojiLikeEvent,
    PrivateMessageEvent,
    FriendPokeEvent,
    GroupMessageEvent,
    GroupPokeEvent,
    Reply,
    Text,
    Message,
)

from .config import (
    log,
    INTERNAL_GROUP_ID,
    WHITELIST,
    PROCESSED_FRIEND_REQUESTS_EXPIRE,
    DEBOUNCE_SECONDS,
    EMOJI_MAPPING,
    EMOJI_TO_CMD,
    processed_friend_requests,
    friend_approve_time,
    unreplied_customers,
    monitored_forwards,
    last_command_time,
    client,
)
from .utils import extract_message_text, get_nicknames_batch, format_duration
from .state import save_state
from .forward import track_forward_message, add_emoji_to_message
from .commands import (
    resolve_target_from_reply,
    handle_bye_command,
    handle_close_command,
    handle_more_command,
    send_and_track_feedback,
)
from .monitor import close_session, send_status_panel


async def handle_event(event) -> bool:
    """
    处理单个事件。返回 True 表示事件已被处理，False 表示未匹配。
    """
    match event:
        # 0. 自动通过好友申请
        case FriendRequestEvent(user_id=uid, comment=comment, flag=flag):
            log.info("收到好友申请：" + str(uid) + " " + comment)
            now = time.time()
            last_time = processed_friend_requests.get(flag)
            if last_time and (now - last_time) < PROCESSED_FRIEND_REQUESTS_EXPIRE:
                log.info("好友申请重复事件已忽略: flag=%s, user_id=%s", flag, uid)
                return True
            # 记录当前处理
            processed_friend_requests[flag] = now

            try:
                await event.approve()
                # 记录通过申请的时间戳，用于忽略之后短时间内收到的第一条消息
                friend_approve_time[uid] = time.time()
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
            except Exception as e:
                log.error("自动通过好友申请失败: user_id=%s, err=%s", uid, e, exc_info=True)
            return True

        # 1. 侦听内部群表情操作（点击预设表情执行对应命令）
        case GroupMsgEmojiLikeEvent(group_id=gid, message_id=mid, user_id=uid) if gid == INTERNAL_GROUP_ID and uid != client.self_id:
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
                if cmd == "close":
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

        # 2. 侦听客服的回复（清理字典并记录耗时）
        case PrivateMessageEvent(post_type="message_sent", target_id=tid) if tid in unreplied_customers:
            await close_session(tid, send_closing=False)
            log.info("客服已回复 %s，移除提醒并记录耗时。剩余未回复: %d", tid, len(unreplied_customers))
            return True

        # 3. 接收客户消息 (加入/更新字典)
        case PrivateMessageEvent(post_type="message", user_id=uid, message_id=msg_id, message=msg_segments) if int(uid) not in WHITELIST:
            uid = int(uid)
            now = time.time()

            # 检查用户是否刚通过好友申请（窗口期内忽略其消息）
            if uid in friend_approve_time and (now - friend_approve_time[uid]) < PROCESSED_FRIEND_REQUESTS_EXPIRE:
                log.info("忽略刚通过好友申请的用户 %d 的消息（窗口期 %d 秒），不加入队列",
                         uid, PROCESSED_FRIEND_REQUESTS_EXPIRE)
                return True

            if uid not in unreplied_customers:
                msg_text = extract_message_text(msg_segments)
                unreplied_customers[uid] = {
                    "last_active": now,
                    "msg_ids": [msg_id],
                    "is_newly_reported": False,
                    "reported_milestones": set(),
                    "pending_since": now,
                }
                log.info("新增客户 %d 进入待回复队列 (msg_id=%s, 内容: %s)。当前队列长度: %d",
                         uid, msg_id, msg_text, len(unreplied_customers))
            else:
                # 更新已有客户的信息
                unreplied_customers[uid]["last_active"] = now
                unreplied_customers[uid]["msg_ids"].append(msg_id)
                unreplied_customers[uid]["is_newly_reported"] = False
                unreplied_customers[uid]["reported_milestones"].clear()
                log.info("客户 %d 追加消息 (msg_id=%s)，累计 %d 条，重置通报倒计时。",
                         uid, msg_id, len(unreplied_customers[uid]["msg_ids"]))

            save_state()  # 客户状态变更后持久化
            return True

        # 4. 侦听私聊戳一戳（自动发送结束语，并从队列移除）
        case FriendPokeEvent(sender_id=sid, target_id=tid) if tid == client.self_id and sid not in WHITELIST:
            closed = await close_session(sid, send_closing=True)
            if closed:
                log.info("私聊戳一戳结束会话: user_id=%s", sid)
            else:
                log.info("私聊戳一戳自动发送结束语（客户不在队列）: user_id=%s", sid)
            return True

        # 5. 内部群戳一戳 -> 发送状态面板
        case GroupPokeEvent(group_id=gid, target_id=tid) if gid == INTERNAL_GROUP_ID and tid == client.self_id:
            log.info("内部群戳一戳触发状态面板: group=%d", gid)
            await send_status_panel(gid)
            return True

        # 6. 内部群命令处理
        case GroupMessageEvent(group_id=gid, message=segments, message_id=msg_id, user_id=uid) if gid == INTERNAL_GROUP_ID and uid != client.self_id:
            # 解析引用和命令
            reply_id = None
            cmd_parts: list[str] = []
            for seg in segments:
                if isinstance(seg, Reply) and seg.id is not None:
                    reply_id = int(seg.id)
                elif isinstance(seg, Text):
                    cmd_parts.append(seg.text)

            cmd_text = ''.join(cmd_parts).strip()
            log.debug("群命令: reply_id=%s, cmd=%s", reply_id, cmd_text)

            # 先检查是否是支持的命令，如果不是则直接忽略
            if not any(cmd_text.startswith(prefix) for prefix in ('.say', '.bye', '.more', '.help', '.close')):
                return True

            if cmd_text.startswith(".help"):
                help_text = (
                    "📖 可用命令列表：\n"
                    "• .say <内容> – 向客户发送私聊消息\n"
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
                # 防抖检查
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

                # 检查是否有未回复客户
                if not unreplied_customers:
                    await client.send_group_msg(
                        group_id=str(gid),
                        message=[Reply(id=str(msg_id)), Text(text="📭 当前没有待回复的客户。")],
                    )
                    return True

                # 获取待回复客户列表（按最新活跃时间倒序）
                sorted_customers = sorted(
                    unreplied_customers.items(),
                    key=lambda item: item[1]["last_active"],
                    reverse=True
                )
                customer_ids: list[int] = [qq for qq, _ in sorted_customers]

                # 批量获取昵称
                nicknames: dict[int, str] = await get_nicknames_batch(customer_ids)

                # 构造列表文本
                lines: list[str] = ["📋 待回复客户列表："]
                now_ts = time.time()
                for idx, (qq, data) in enumerate(sorted_customers, 1):
                    nickname = nicknames.get(qq, "未知昵称")
                    wait_seconds = now_ts - data["pending_since"]
                    wait_str = format_duration(wait_seconds)
                    lines.append(f"{idx}. {nickname}（{qq}）已等待 {wait_str}")

                # 防止消息过长（最多显示20条客户记录）
                full_text: str
                if len(lines) > 22:  # 头部1行 + 最多20条客户记录
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
            # 解析目标客户
            customer_ids, err_msg = await resolve_target_from_reply(reply_id)
            if err_msg:
                await client.send_group_msg(
                    group_id=str(gid),
                    message=[Reply(id=str(msg_id)), Text(text=err_msg)],
                )
                return True

            # 防御性检查：customer_ids 必须非空
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

            # 根据命令分发

            if cmd_text.startswith(".say"):
                content = cmd_text[4:].strip()
                if not content:
                    await client.send_group_msg(
                        group_id=str(gid),
                        message=[Reply(id=str(msg_id)), Text(text=".say 命令后需要附带要发送的消息内容")],
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
                        message=content,
                    )
                    closed = await close_session(customer_id, send_closing=False)
                    # 不再移除监听，只更新防抖记录
                    last_command_time[key] = now
                    feedback = f"✅ 已向客户 {customer_id} 发送消息。" + ("（客户已在待回复队列）" if closed else "（客户不在待回复队列）")
                except Exception as e:
                    log.error("发送私聊消息失败: customer=%s, err=%s", customer_id, e, exc_info=True)
                    feedback = f"❌ 发送失败：{e}"

                # 发送反馈消息并加入监听
                resp = await client.send_group_msg(
                    group_id=str(gid),
                    message=[Reply(id=str(msg_id)), Text(text=feedback)],
                )
                feedback_msg_id = resp.get("message_id")
                if feedback_msg_id:
                    track_forward_message(feedback_msg_id, [customer_id], gid)
                return True

            elif cmd_text.startswith(".bye"):
                # 防抖检查
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
                # 不是我们关心的命令，忽略
                return True

        case _:
            return False

    return False
