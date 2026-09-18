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

from . import config as cfg
from .config import log
from .utils import format_duration
from . import mute
from .message_sender import (
    close_session,
    send_status_panel,
    send_forward_from_message_ids,
    send_and_track_feedback,
    track_forward_message,
    add_emoji_to_message,
    action_emoji_ids,
    build_say_feedback,
    register_recallable_send,
)
from . import history

# 运维类命令（无需引用机器人消息）
OPS_COMMAND_PREFIXES = ('.say', '.bye', '.more', '.help', '.close', '.list', '.reload', '.update', '.mute', '.unmute', '.status')
# 需要客户目标的会话命令：优先 Reply，否则可用 <客户QQ>
SESSION_OP_PREFIXES = ('.say', '.bye', '.close', '.more')


def parse_qq_arg(token: str) -> int | None:
    """解析客户 QQ 号；非正整数返回 None。"""
    t = (token or "").strip()
    if not t or not t.isdigit():
        return None
    qq = int(t)
    return qq if qq > 0 else None


def split_session_op(cmd_text: str) -> tuple[str, str] | None:
    """识别 .say/.bye/.close/.more，返回 (op, 参数串)；不匹配返回 None。"""
    text = (cmd_text or "").strip()
    for op in SESSION_OP_PREFIXES:
        if text.startswith(op):
            return op, text[len(op):].strip()
    return None


def extract_say_target_segments(message: list[Message]) -> tuple[str | None, list[Message]]:
    """无 Reply 时解析 .say <qq|all> [内容…]：返回 (目标 token, 可发送消息段)。"""
    target: str | None = None
    segments: list[Message] = []
    first_text_done = False
    for seg in message:
        if isinstance(seg, Reply):
            continue
        if isinstance(seg, Text) and not first_text_done:
            first_text_done = True
            text = seg.text.strip()
            if not text.startswith(".say"):
                continue
            rest = text[len(".say"):].strip()
            parts = rest.split(None, 1)
            if not parts:
                return None, []
            target = parts[0]
            if len(parts) > 1 and parts[1].strip():
                segments.append(Text(text=parts[1]))
        else:
            segments.append(seg)
    return target, segments


def extract_say_qq_segments(message: list[Message]) -> tuple[int | None, list[Message]]:
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


def extract_say_plain_content(message: list[Message]) -> list[Message]:
    """无目标参数时：.say 后全部内容作为正文（自动匹配客户用）。"""
    segments: list[Message] = []
    first_text_done = False
    for seg in message:
        if isinstance(seg, Reply):
            continue
        if isinstance(seg, Text) and not first_text_done:
            first_text_done = True
            text = seg.text.strip()
            if text.startswith(".say"):
                rest = text[len(".say"):].strip()
                if rest:
                    segments.append(Text(text=rest))
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
    segments: list[Message] | None = None,
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
    segments: list[Message],
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
        send_resp = await cfg.client.send_private_msg(
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
    except Exception as e:
        log.error("发送私聊消息失败: customer=%s, err=%s", customer_id, e, exc_info=True)
        return f"❌ 发送失败：{e}", False, customer_msg_id


# ======================= 昵称批量获取 =======================

async def get_nicknames_batch(user_ids: list[int], delay: float = 0.2) -> dict[int, str]:
    """
    批量获取用户昵称，返回 {qq: nickname} 映射。
    逐次调用 get_stranger_info 并加入延迟以避免限流。
    """
    nicknames: dict[int, str] = {}
    for uid in user_ids:
        try:
            info = await cfg.client.get_stranger_info(user_id=str(uid))
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
    data = cfg.monitored_forwards.get(reply_id)
    if data is not None:
        return data["customer_ids"], None

    try:
        msg_detail = await cfg.client.get_msg(message_id=str(reply_id))
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
        resp = await cfg.client.send_private_msg(
            user_id=str(customer_id),
            message=cfg.CLOSING_MESSAGE,
        )
        mid = resp.get("message_id") if resp else None
        await history.record_bot_send(
            customer_id, int(mid) if mid is not None else None, "closing_message", cfg.CLOSING_MESSAGE,
        )
        closed = await close_session(customer_id, send_closing=False, close_reason="bye")
        return f"✅ 已向客户 {customer_id} 发送结束语。" + ("（客户已在待回复队列）" if closed else "（客户不在待回复队列）")
    except Exception as e:
        log.error("发送结束语失败: customer=%s, err=%s", customer_id, e, exc_info=True)
        return f"❌ 发送失败：{e}"


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
    except Exception as e:
        log.error("关闭会话失败: customer=%s, err=%s", customer_id, e, exc_info=True)
        return f"❌ 关闭失败：{e}"


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
        resp = await cfg.client.get_friend_msg_history(
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
        await cfg.client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(mid)), Text(text=f"⏰ 已超过 {cfg.RECALL_WINDOW_SECONDS} 秒，无法撤回该消息。")],
        )
        return True

    try:
        await cfg.client.delete_msg(message_id=str(data["customer_msg_id"]))
    except Exception as e:
        log.error("撤回私聊消息失败: customer=%s, msg_id=%s, err=%s",
                  customer_id, data["customer_msg_id"], e, exc_info=True)
        # 仍在时限内则恢复记录，允许重试
        if time.time() - data["sent_at"] <= cfg.RECALL_WINDOW_SECONDS:
            cfg.recallable_sends[mid] = data
        await cfg.client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(mid)), Text(text=f"❌ 撤回失败：{e}")],
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
    await cfg.client.send_group_msg(
        group_id=str(gid),
        message=[Reply(id=str(mid)), Text(text=f"🧹 已撤回发送给客户 {customer_id} 的消息。")],
    )
    return True


# ======================= 群事件处理 =======================

async def handle_group_emoji(event: GroupMsgEmojiLikeEvent) -> bool:
    """处理内部群表情贴纸操作：匹配表情到对应命令"""
    gid = event.group_id
    mid = event.message_id

    if gid != cfg.INTERNAL_GROUP_ID or event.user_id == cfg.client.self_id:
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

    # 检查是否点击了 .say 通报消息上的撤回表情
    # 过期记录在宽限期内仍保留，handle_recall_click 会回复超时提示
    if eid == cfg.EMOJI_MAPPING.get("recall") and mid in cfg.recallable_sends:
        await handle_recall_click(gid, mid, event.user_id)
        return True

    # 检查是否是 pending say 提示消息上的确认/取消
    pending = cfg.pending_say.get(event.user_id)
    if pending and mid == pending["prompt_msg_id"]:
        cmd = cfg.EMOJI_TO_CMD.get(eid)
        if cmd == "cancel":
            cfg.pending_say.pop(event.user_id, None)
            await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(mid)), Text(text="❌ 已取消发送。")],
            )
        return True

    tracked_data = cfg.monitored_forwards.get(mid)
    if tracked_data is None:
        return True

    customer_ids = list(tracked_data["customer_ids"])
    cmd = cfg.EMOJI_TO_CMD.get(eid)
    if cmd is None:
        return True

    # more 仅支持单客户；say/bye/close 在多客户合并转发上作用域扩展为该消息内全部客户
    if not customer_ids:
        return True
    if cmd == "more" and len(customer_ids) != 1:
        await cfg.client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(mid)), Text(text="暂不支持：该合并转发包含多个客户，more 请手动处理单客户。")],
        )
        return True
    if cmd == "more":
        customer_id = customer_ids[0]
        try:
            success, feedback, new_fwd_id = await handle_more_command(
                gid, mid, customer_id, operator_uid=event.user_id, via="emoji",
            )
            if success:
                if new_fwd_id:
                    track_forward_message(new_fwd_id, [customer_id], gid)
                    await add_emoji_to_message(new_fwd_id, action_emoji_ids())
                if feedback:
                    await send_and_track_feedback(gid, mid, feedback, customer_id)
            else:
                if feedback:
                    await send_and_track_feedback(gid, mid, feedback, customer_id)
        except Exception:
            pass
        return True

    # say / bye / close：作用域 = 转发内全部客户
    targets = customer_ids
    try:
        if cmd == "say":
            resp = await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(mid)), Text(text="📝 请发送要回复给客户的消息内容：")],
            )
            prompt_msg_id = resp.get("message_id")
            cfg.pending_say[event.user_id] = {
                "prompt_msg_id": prompt_msg_id,
                "customer_id": targets[0],
                "customer_ids": targets,
                "reply_id": mid,
                "group_id": gid,
                "via": "emoji",
                "target_source": "emoji_multi" if len(targets) > 1 else "emoji",
                "debounce_key": (mid, "say"),
            }
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
                    await cfg.client.send_group_msg(
                        group_id=str(gid),
                        message=[Reply(id=str(prompt_msg_id)), Text(text=hint)],
                    )
        elif cmd in ("close", "bye"):
            op_name = cmd
            label = "close" if cmd == "close" else "bye"
            if len(targets) == 1:
                if cmd == "close":
                    feedback = await handle_close_command(
                        gid, mid, targets[0], operator_uid=event.user_id, via="emoji",
                    )
                else:
                    feedback = await handle_bye_command(
                        gid, mid, targets[0], operator_uid=event.user_id, via="emoji",
                    )
                await send_and_track_feedback(gid, mid, feedback, targets[0])
            else:
                ok, failed = await apply_session_op_to_customers(
                    op_name, targets,
                    gid=gid,
                    operator_uid=event.user_id,
                    via="emoji",
                    reply_id=mid,
                )
                summary = format_batch_result(f"emoji {label} all", ok, failed)
                await cfg.client.send_group_msg(
                    group_id=str(gid),
                    message=[Reply(id=str(mid)), Text(text=summary)],
                )
    except Exception:
        pass
    return True


async def handle_group_poke(event: GroupPokeEvent) -> bool:
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
        await cfg.client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(msg_id)), Text(text=text)],
        )

    rest_parts = rest.split()
    now = time.time()
    debounce_sec = cfg.DEBOUNCE_SECONDS.get(op_name, 5)

    # ---- all ----
    if rest_parts and rest_parts[0].lower() == "all":
        if op == ".more":
            await _reply("❌ 群内不允许 .more all；请指定具体客户 QQ，或引用机器人消息。")
            return True
        if op == ".say":
            _target, segments = extract_say_target_segments(event.message)
            if not segments:
                await _reply("❌ " + usage[".say"] + "（all 必须附带发送内容，不支持等待输入）")
                return True
        key = ("__all__", op_name)
        if now - cfg.last_command_time.get(key, 0) < debounce_sec:
            await _reply(f"⏳ 操作过于频繁，请稍后再试（防抖 {debounce_sec} 秒）")
            return True

        targets = list(cfg.unreplied_customers.keys())
        if not targets:
            await _reply("📭 当前没有待回复客户。")
            return True

        if op in (".say", ".bye") and not cfg.client.is_running:
            await _reply("❌ 客户端未运行，无法执行批量 " + op)
            return True

        if op == ".say":
            ok, failed = await apply_session_op_to_customers(
                "say", targets,
                gid=gid,
                operator_uid=event.user_id,
                via="group_qq",
                segments=segments,
            )
            if ok:
                cfg.last_command_time[key] = now
            await _reply(format_batch_result("say all", ok, failed))
            return True

        if op == ".bye":
            ok, failed = await apply_session_op_to_customers(
                "bye", targets,
                gid=gid,
                operator_uid=event.user_id,
                via="group_qq",
            )
            if ok:
                cfg.last_command_time[key] = now
            await _reply(format_batch_result("bye all", ok, failed))
            return True

        if op == ".close":
            ok, failed = await apply_session_op_to_customers(
                "close", targets,
                gid=gid,
                operator_uid=event.user_id,
                via="group_qq",
            )
            if ok:
                cfg.last_command_time[key] = now
            await _reply(format_batch_result("close all", ok, failed))
            return True

    # ---- 目标解析：显式 QQ / all 已处理；否则无参自动匹配 ----
    auto_qq = sole_unreplied_customer()
    queue_len = len(cfg.unreplied_customers)
    say_segments: list[Message] | None = None
    qq: int | None = None

    if op == ".say":
        qq, say_segments = extract_say_qq_segments(event.message)
        if qq is None and rest_parts and rest_parts[0].lower() == "all":
            return True  # 已处理
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
                return True
    else:
        if rest_parts:
            qq = parse_qq_arg(rest_parts[0])
            if qq is None:
                await _reply("❌ " + usage[op])
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
                return True

    key = (qq, op_name if op != ".say" else "say")
    if now - cfg.last_command_time.get(key, 0) < debounce_sec:
        await _reply(f"⏳ 操作过于频繁，请稍后再试（防抖 {debounce_sec} 秒）")
        return True

    if op == ".say":
        if not say_segments:
            resp = await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(msg_id)), Text(text="📝 请发送要回复给客户的消息内容：")],
            )
            prompt_msg_id = resp.get("message_id")
            cfg.pending_say[event.user_id] = {
                "prompt_msg_id": prompt_msg_id,
                "customer_id": qq,
                "customer_ids": [qq],
                "reply_id": None,
                "group_id": gid,
                "via": "group_qq",
                "target_source": "auto" if not rest_parts or parse_qq_arg(rest_parts[0] if rest_parts else "") is None else "qq",
                "debounce_key": key,
            }
            await history.record_command(
                qq, event.user_id, "say",
                mode="pending", reply_id=None, group_id=gid, via="group_qq",
            )
            if prompt_msg_id:
                await add_emoji_to_message(prompt_msg_id, [cfg.EMOJI_MAPPING["cancel"]])
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
        resp = await cfg.client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(msg_id)), Text(text=feedback)],
        )
        feedback_msg_id = resp.get("message_id")
        if feedback_msg_id:
            track_forward_message(feedback_msg_id, [qq], gid)
            if customer_msg_id is not None:
                register_recallable_send(feedback_msg_id, customer_msg_id, qq, gid, event.user_id)
                asyncio.create_task(add_emoji_to_message(feedback_msg_id, [cfg.EMOJI_MAPPING["recall"]]))
        return True

    if op == ".bye":
        feedback = await handle_bye_command(gid, None, qq, operator_uid=event.user_id, via="group_qq")
        if not feedback.startswith("❌"):
            cfg.last_command_time[key] = now
        await send_and_track_feedback(gid, msg_id, feedback, qq)
        return True

    if op == ".close":
        feedback = await handle_close_command(gid, None, qq, operator_uid=event.user_id, via="group_qq")
        if not feedback.startswith("❌"):
            cfg.last_command_time[key] = now
        await send_and_track_feedback(gid, msg_id, feedback, qq)
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
        return True

    return True


async def handle_group_command(event: GroupMessageEvent) -> bool:
    """处理内部群命令消息"""
    gid = event.group_id
    msg_id = event.message_id

    if gid != cfg.INTERNAL_GROUP_ID or event.user_id == cfg.client.self_id:
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

    # 检查是否处于等待 .say 内容的状态：收到消息立即发送
    if event.user_id in cfg.pending_say and not any(cmd_text.startswith(prefix) for prefix in OPS_COMMAND_PREFIXES):
        segments = extract_sendable_segments(event.message)
        if not segments:
            await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(msg_id)), Text(text="⚠️ 消息内容为空，已忽略。")],
            )
            return True

        pending = cfg.pending_say.pop(event.user_id)
        targets: list[int] = list(pending.get("customer_ids") or [])
        if not targets and pending.get("customer_id") is not None:
            targets = [int(pending["customer_id"])]
        orig_reply_id = pending.get("reply_id")
        via = pending.get("via", "group_reply")
        debounce_key = pending.get("debounce_key") or (orig_reply_id, "say")

        if len(targets) == 1:
            customer_id = targets[0]
            customer_msg_id: int | None = None
            try:
                feedback, _closed, customer_msg_id = await send_private_and_close(
                    customer_id, segments,
                    operator_uid=event.user_id,
                    via=via,
                    close_reason="say",
                    group_id=gid,
                    reply_id=orig_reply_id,
                )
                cfg.last_command_time[debounce_key] = time.time()
            except Exception as e:
                log.error("发送私聊消息失败: customer=%s, err=%s", customer_id, e, exc_info=True)
                feedback = f"❌ 发送失败：{e}"

            resp = await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(msg_id)), Text(text=feedback)],
            )
            feedback_msg_id = resp.get("message_id")
            if feedback_msg_id:
                track_forward_message(feedback_msg_id, [customer_id], gid)
                if customer_msg_id is not None and not feedback.startswith("❌"):
                    register_recallable_send(feedback_msg_id, customer_msg_id, customer_id, gid, event.user_id)
                    asyncio.create_task(add_emoji_to_message(feedback_msg_id, [cfg.EMOJI_MAPPING["recall"]]))
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
        await cfg.client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(msg_id)), Text(text=summary)],
        )
        return True

    if not any(cmd_text.startswith(prefix) for prefix in OPS_COMMAND_PREFIXES):
        return True

    if cmd_text.startswith(".help"):
        help_text = (
            "📖 可用命令列表：\n"
            "• .say <内容> – 向客户发送私聊消息（不带内容则等待下一条消息）\n"
            "• .say <QQ|all> <内容> – 不引用时按 QQ/队列发送；无参且队列仅 1 人时自动匹配\n"
            "• .bye / .bye <QQ|all> – 发送结束语并关闭会话（无参且队列仅 1 人时自动匹配）\n"
            "• .close / .close <QQ|all> – 关闭会话但不发送结束语（无参自动匹配同上）\n"
            "• .more / .more <QQ> – 获取历史合并转发；**不支持 .more all**；无参且队列仅 1 人时自动匹配\n"
            "• .list – 列出所有未回复客户及其等待时间\n"
            "• .status – 查看运行状态面板\n"
            "• .mute [分钟] – 临时静音（不带参数不限时；如 .mute 30 为 30 分钟）\n"
            "• .unmute – 解除静音，并汇总发出延后提醒\n"
            "• .reload – 重载当前明文配置（不拉代码、不展示内容；兼容 .reload cfg）\n"
            "• .help – 显示此帮助信息\n"
            "\n"
            "使用方法：优先回复机器人消息；或 .命令 <客户QQ|all>；无参且待回复仅 1 人时自动匹配该客户。\n"
            "说明：all = 当前待回复队列；.say all 必须带内容；群内不允许 .more all。\n"
            "表情：多客户合并转发上 say/bye/close 作用域为该消息内全部客户；more 仅单客户。"
        )
        await cfg.client.send_group_msg(
            group_id=str(gid),
            message=[Text(text=help_text)],
        )
        return True

    elif cmd_text.startswith(".status"):
        await send_status_panel(gid)
        return True

    elif cmd_text.startswith(".unmute"):
        key = (msg_id, "unmute")
        now_ts = time.time()
        debounce_sec = cfg.DEBOUNCE_SECONDS.get("unmute", cfg.DEBOUNCE_SECONDS.get("close", 5))
        last_time = cfg.last_command_time.get(key, 0.0)
        if now_ts - last_time < debounce_sec:
            await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(msg_id)), Text(text=f"⏳ 操作过于频繁，请稍后再试（防抖 {debounce_sec:.0f} 秒）")],
            )
            return True
        cfg.last_command_time[key] = now_ts

        # 与终端 unmute 共用同一套业务逻辑
        _was, _flushed, text = await mute.unmute_and_flush()
        await cfg.client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(msg_id)), Text(text="✅ " + text)],
        )
        return True

    elif cmd_text.startswith(".mute"):
        key = (msg_id, "mute")
        now_ts = time.time()
        debounce_sec = cfg.DEBOUNCE_SECONDS.get("mute", cfg.DEBOUNCE_SECONDS.get("close", 5))
        last_time = cfg.last_command_time.get(key, 0.0)
        if now_ts - last_time < debounce_sec:
            await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(msg_id)), Text(text=f"⏳ 操作过于频繁，请稍后再试（防抖 {debounce_sec:.0f} 秒）")],
            )
            return True
        cfg.last_command_time[key] = now_ts

        arg = cmd_text[len(".mute"):].strip()
        # 无参数 = 不限时静音；有参数 = 限时分钟数
        minutes: float | None = None
        if arg:
            try:
                minutes = float(arg)
            except ValueError:
                await cfg.client.send_group_msg(
                    group_id=str(gid),
                    message=[Reply(id=str(msg_id)), Text(text=f"❌ 无效时长：{arg}，示例：.mute 30；直接发送 .mute 表示不限时")],
                )
                return True
            if minutes <= 0:
                await cfg.client.send_group_msg(
                    group_id=str(gid),
                    message=[Reply(id=str(msg_id)), Text(text="❌ 静音时长必须大于 0 分钟；不限时请直接发送 .mute")],
                )
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
        await cfg.client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(msg_id)), Text(text=text)],
        )
        return True

    elif cmd_text.startswith(".list"):
        now = time.time()
        key = (msg_id, "list")
        debounce_list = cfg.DEBOUNCE_SECONDS.get("list", 5)
        last_time = cfg.last_command_time.get(key, 0.0)
        if now - last_time < debounce_list:
            await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[
                    Reply(id=str(msg_id)),
                    Text(text=f"⏳ 操作过于频繁，请稍后再试（防抖 {debounce_list:.0f} 秒）")
                ],
            )
            return True

        if not cfg.unreplied_customers:
            await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(msg_id)), Text(text="📭 当前没有待回复的客户。")],
            )
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
            resp = await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(msg_id)), Text(text=full_text)],
            )
            feedback_msg_id = resp.get("message_id")
            if feedback_msg_id:
                track_forward_message(feedback_msg_id, [], gid)
            cfg.last_command_time[key] = now
            log.info(".list 命令执行成功，返回 %d 名客户", len(customer_ids))
        except Exception as e:
            log.error("发送 .list 结果失败: %s", e, exc_info=True)
        return True

    elif cmd_text.startswith((".reload", ".update")):
        raw = cmd_text[1:]  # drop leading dot
        if raw.startswith("reload"):
            arg = raw[len("reload"):].strip()
            cmd_name = ".reload"
        else:
            arg = raw[len("update"):].strip()
            cmd_name = ".update"
        log.info("%s: user=%s group=%s arg=%s", cmd_name, event.user_id, gid, arg)
        # .reload / .update 可不带参数；cfg 为兼容旧写法
        if arg not in ("", "cfg"):
            await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(msg_id)), Text(text="❌ 用法：.reload（可省略 cfg）")],
            )
            return True

        _success, update_message = await cfg.run_reload_cfg()
        await cfg.client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(msg_id)), Text(text=update_message)],
        )
        return True

    session_op = split_session_op(cmd_text)

    if reply_id is None or not cmd_parts:
        if reply_id is None and session_op is not None:
            await handle_session_command_by_qq(event, gid, msg_id, session_op[0], session_op[1])
        return True

    customer_ids, err_msg = await resolve_target_from_reply(reply_id)
    if err_msg:
        await cfg.client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(msg_id)), Text(text=err_msg)],
        )
        return True

    if not customer_ids:
        await cfg.client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(msg_id)), Text(text="内部错误：无法解析客户列表")],
        )
        return True

    if len(customer_ids) != 1:
        await cfg.client.send_group_msg(
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
            # 无内容：进入等待模式，收集用户后续消息
            resp = await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[Reply(id=str(msg_id)), Text(text="📝 请发送要回复给客户的消息内容：")],
            )
            prompt_msg_id = resp.get("message_id")
            cfg.pending_say[event.user_id] = {
                "prompt_msg_id": prompt_msg_id,
                "customer_id": customer_id,
                "reply_id": reply_id,
                "group_id": gid,
                "via": "group_reply",
                "target_source": "reply",
                "debounce_key": (reply_id, "say"),
            }
            await history.record_command(
                customer_id, event.user_id, "say",
                mode="pending", reply_id=reply_id, group_id=gid, via="group_reply",
            )
            if prompt_msg_id:
                await add_emoji_to_message(prompt_msg_id, [
                    cfg.EMOJI_MAPPING["cancel"],
                ])
            return True

        key = (reply_id, "say")
        last_time = cfg.last_command_time.get(key, 0)
        if now - last_time < cfg.DEBOUNCE_SECONDS["say"]:
            await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[
                    Reply(id=str(msg_id)),
                    Text(text=f"⏳ 操作过于频繁，请稍后再试（防抖 {cfg.DEBOUNCE_SECONDS['say']} 秒）")
                ],
            )
            return True

        customer_msg_id: int | None = None
        try:
            send_resp = await cfg.client.send_private_msg(
                user_id=str(customer_id),
                message=segments,
            )
            raw_mid = send_resp.get("message_id") if send_resp else None
            customer_msg_id = int(raw_mid) if raw_mid is not None else None
            await history.record_command(
                customer_id, event.user_id, "say",
                mode="content", reply_id=reply_id, group_id=gid, via="group_reply",
            )
            if customer_msg_id is not None:
                await history.record_staff_reply(customer_id, customer_msg_id, time.time(), segments, actor_uid=event.user_id)
            closed = await close_session(customer_id, send_closing=False, close_reason="say")
            cfg.last_command_time[key] = now
            feedback = build_say_feedback(customer_id, closed, customer_msg_id is not None)
        except Exception as e:
            log.error("发送私聊消息失败: customer=%s, err=%s", customer_id, e, exc_info=True)
            feedback = f"❌ 发送失败：{e}"

        resp = await cfg.client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(msg_id)), Text(text=feedback)],
        )
        feedback_msg_id = resp.get("message_id")
        if feedback_msg_id:
            track_forward_message(feedback_msg_id, [customer_id], gid)
            if customer_msg_id is not None:
                register_recallable_send(feedback_msg_id, customer_msg_id, customer_id, gid, event.user_id)
                asyncio.create_task(add_emoji_to_message(feedback_msg_id, [cfg.EMOJI_MAPPING["recall"]]))
        return True

    # ---- .bye ----
    elif cmd_text.startswith(".bye"):
        key = (reply_id, "bye")
        last_time = cfg.last_command_time.get(key, 0)
        if now - last_time < cfg.DEBOUNCE_SECONDS["bye"]:
            await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[
                    Reply(id=str(msg_id)),
                    Text(text=f"⏳ 操作过于频繁，请稍后再试（防抖 {cfg.DEBOUNCE_SECONDS['bye']} 秒）")
                ],
            )
            return True

        feedback = await handle_bye_command(gid, msg_id, customer_id, operator_uid=event.user_id, via="group_reply")
        if not feedback.startswith("❌"):
            cfg.last_command_time[key] = now
        asyncio.create_task(send_and_track_feedback(gid, msg_id, feedback, customer_id))
        return True

    # ---- .close ----
    elif cmd_text.startswith(".close"):
        key = (reply_id, "close")
        last_time = cfg.last_command_time.get(key, 0)
        debounce_sec = cfg.DEBOUNCE_SECONDS.get("close", 5)
        if now - last_time < debounce_sec:
            await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[
                    Reply(id=str(msg_id)),
                    Text(text=f"⏳ 操作过于频繁，请稍后再试（防抖 {debounce_sec} 秒）")
                ],
            )
            return True

        feedback = await handle_close_command(gid, msg_id, customer_id, operator_uid=event.user_id, via="group_reply")
        if not feedback.startswith("❌"):
            cfg.last_command_time[key] = now
        asyncio.create_task(send_and_track_feedback(gid, msg_id, feedback, customer_id))
        return True

    # ---- .more ----
    elif cmd_text.startswith(".more"):
        key = (reply_id, "more")
        last_time = cfg.last_command_time.get(key, 0)
        if now - last_time < cfg.DEBOUNCE_SECONDS["more"]:
            await cfg.client.send_group_msg(
                group_id=str(gid),
                message=[
                    Reply(id=str(msg_id)),
                    Text(text=f"⏳ 操作过于频繁，请稍后再试（防抖 {cfg.DEBOUNCE_SECONDS['more']} 秒）")
                ],
            )
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
        else:
            if feedback:
                asyncio.create_task(send_and_track_feedback(gid, msg_id, feedback, customer_id))
        return True

    else:
        return True

