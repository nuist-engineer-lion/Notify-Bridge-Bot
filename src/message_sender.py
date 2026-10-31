import time
import asyncio
import random
import statistics

from nonebot.adapters.onebot.v11 import MessageSegment

from . import config as cfg
from . import ai
from .config import (
    log,
    STARTED_AT,
    monitored_forward_order,
    last_command_time,
    client,
)
from .models import CustomerData
from .messages import message_label, message_segments
from .utils import format_duration, get_current_available_members
from .state import save_state
from . import history
from . import mute


# ======================= 历史消息获取 =======================

async def get_recent_message_ids(
    user_id: int,
    count: int = 200,
    max_age_seconds: int | None = None,
    *,
    messages_by_id: dict[str, dict] | None = None,
) -> list[int]:
    """
    通过 API 获取与指定用户的最近消息，筛选出最近 max_age_seconds 秒内的消息（双方消息）。
    返回按时间正序排列的 message_id 列表。
    messages_by_id 可接收完整历史缓存，供转发时补全引用原文。
    """
    now = time.time()
    if max_age_seconds is None:
        max_age_seconds = cfg.RECENT_MESSAGE_MAX_AGE
    try:
        resp = await client.call_api(
            "get_friend_msg_history",
            user_id=str(user_id),
            count=count,
            parse_mult_msg=True,
        )
        messages = resp.get("messages", [])
        if not messages:
            return []
        messages.sort(key=lambda m: m.get("time", 0))
        filtered = [
            m for m in messages
            if (now - m.get("time", 0)) <= max_age_seconds
        ]
        msg_ids = [m["message_id"] for m in filtered if "message_id" in m]
        if messages_by_id is not None:
            # 保留整个拉取窗口，引用目标可能在提醒时间范围之外。
            messages_by_id.update({
                str(m["message_id"]): m for m in messages if "message_id" in m
            })
        log.debug("客户 %d 历史消息: 获取到 %d 条，过滤后 %d 条（%d 秒内）",
                  user_id, len(messages), len(msg_ids), max_age_seconds)
        return msg_ids
    except Exception as e:
        log.error("获取客户 %d 历史消息失败: %s", user_id, e, exc_info=True)
        return []


# ======================= 合并转发监听管理 =======================

def track_forward_message(message_id: int, customer_ids: list[int], group_id: int) -> None:
    if message_id in cfg.monitored_forwards:
        try:
            monitored_forward_order.remove(message_id)
        except ValueError:
            pass

    monitored_forward_order.append(message_id)
    cfg.monitored_forwards[message_id] = {
        "customer_ids": customer_ids,
        "group_id": group_id,
        "created_at": time.time(),
    }

    while len(monitored_forward_order) > cfg.MONITORED_FORWARD_LIMIT:
        expired_id = monitored_forward_order.popleft()
        cfg.monitored_forwards.pop(expired_id, None)

    log.debug("监听合并转发已更新: message_id=%s, 当前监听数=%d", message_id, len(monitored_forward_order))
    save_state()


def pop_tracked_forward(message_id: int) -> dict | None:
    """移除监听的合并转发，并清理其防抖记录"""
    data = cfg.monitored_forwards.pop(message_id, None)
    if data is None:
        return None

    try:
        monitored_forward_order.remove(message_id)
    except ValueError:
        pass

    keys_to_remove = [key for key in last_command_time if key[0] == message_id]
    for key in keys_to_remove:
        del last_command_time[key]

    save_state()
    return data


# ======================= 合并转发构造 =======================

def _forward_segments(content) -> list[dict]:
    """统一历史 API 的数组、单段和 CQ 字符串格式；不改动原消息。"""
    if isinstance(content, (str, dict, list, tuple)):
        return message_segments(content, tolerant=True)
    return []


async def build_forward_history_nodes(
    message_ids: list[int],
    messages_by_id: dict[str, dict] | None = None,
) -> list[MessageSegment]:
    """保留原生引用，并补全跨会话转发所需的引用预览。"""
    # 原生引用兼容基线（2026-10-31，仅源码核对，QQ 显示效果待实测）：
    # SnowLuma/SnowLuma v2.2.1 @8ffd314260e9e2b6967eeca494d87aa5be046c34；
    # NapNeko/NapCatQQ main @98d781d19815d1625b24d717ce280ecf0a4f6d0c。
    # 下方 SnowLuma 路径相对 packages/onebot/src/，NapCat 路径相对 packages/napcat-onebot/。
    cache: dict[str, dict | None] = dict(messages_by_id or {})

    def text(value: str) -> dict:
        return {"type": "text", "data": {"text": value}}

    def segments(message: dict) -> list[dict]:
        content = message.get("message")
        if content is None:
            content = message.get("raw_message")
        return _forward_segments(content)

    def sender_info(message: dict) -> dict:
        sender = message.get("sender")
        return sender if isinstance(sender, dict) else {}

    async def load(message_id: str) -> dict | None:
        if message_id not in cache:
            try:
                detail = await client.call_api("get_msg", message_id=message_id)
                cache[message_id] = detail if isinstance(detail, dict) else None
            except Exception as e:
                log.warning("获取转发引用消息 %s 失败: %s", message_id, e)
                cache[message_id] = None
        return cache[message_id]

    def quote_preview(content) -> str:
        parts = []
        for segment in _forward_segments(content):
            kind, data = segment["type"], segment["data"]
            if kind == "text":
                parts.append(str(data.get("text") or ""))
            elif kind == "at":
                qq = data.get("qq")
                parts.append("@全体成员" if qq == "all" else f"@{qq or '未知用户'}")
            else:
                # 兼容 SnowLuma 2.2.1：reply-preview.ts 将引用媒体转为文字摘要，
                # 拒绝嵌套 reply/node；此处统一成 text，避免再次上传媒体或解析失败。
                label = message_label(kind)
                name = data.get("name")
                if kind in ("file", "onlinefile") and isinstance(name, str) and name:
                    label += f"：{name}"
                parts.append(f"[{label}]")
        return "".join(parts)

    def quote_integer(value, *, minimum: int, maximum: int) -> str | None:
        # 兼容 SnowLuma 2.2.1 message-parser.ts::parseForwardReply/replyInteger：
        # qq 必须为正安全整数，time 必须为 uint32 秒；省略不合法的可选元数据。
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return None
        try:
            number = int(value)
        except ValueError:
            return None
        return str(number) if minimum <= number <= maximum else None

    async def prepare_content(
        content: list[dict], current_id: str, native_ids: dict[str, None],
    ) -> list[dict]:
        result = []
        for segment in content:
            kind, data = segment["type"], segment["data"]
            if kind != "reply":
                if kind in ("text", "face", "at"):
                    required = {"text": "text", "face": "id", "at": "qq"}[kind]
                    if data.get(required) is not None:
                        result.append(segment)
                        continue
                if kind == "image":
                    # 收到的 file 可能只是媒体标识；重新构造时优先使用可发送的地址。
                    source = data.get("file")
                    if not isinstance(source, str) or not source.startswith(
                        ("http://", "https://", "file://", "base64://")
                    ):
                        source = data.get("url")
                    if isinstance(source, str) and source.startswith(
                        ("http://", "https://", "file://", "base64://")
                    ):
                        result.append({"type": "image", "data": {**data, "file": source}})
                        continue
                # 特殊消息不可与说明文字混发。保留原生节点，也保护未知扩展类型。
                label = message_label(kind)
                name = data.get("name")
                if kind in ("file", "onlinefile") and isinstance(name, str) and name:
                    label += f"：{name}"
                native_ids[current_id] = None
                result.append(text(f"\n【{label}，见下方原消息 {current_id}】\n"))
                continue
            key = str(data["id"]) if data.get("id") is not None else None
            preview = quote_preview(data.get("message"))
            quoted = cache.get(key) if key is not None else None
            if not preview and key is not None:
                quoted = await load(key)
            if not preview and quoted is not None:
                preview = quote_preview(segments(quoted))
            if not preview:
                preview = "（原消息内容不可用）"

            # 兼容 SnowLuma 2.2.1 message-parser.ts::parseForwardReply：
            # message 优先于 id，qq/time 用于构造原生引用预览。
            # 兼容 NapCat @98d781d api/msg.ts 的 reply 转换器：
            # id 可定位原会话；seq 优先在目标会话查询，因此这里保留 id、移除 seq。
            reply_data = {"message": [text(preview)]}
            if key is not None:
                reply_data["id"] = key
            quoted = quoted or {}
            qq = quote_integer(
                data.get("qq", sender_info(quoted).get("user_id")),
                minimum=1, maximum=2**53 - 1,
            )
            timestamp = quote_integer(
                data.get("time", quoted.get("time")),
                minimum=0, maximum=2**32 - 1,
            )
            if qq is not None:
                reply_data["qq"] = qq
            if timestamp is not None:
                reply_data["time"] = timestamp
            result.append({"type": "reply", "data": reply_data})
        return result

    nodes: list[MessageSegment] = []
    for message_id in message_ids:
        key = str(message_id)
        message = await load(key)
        content = segments(message) if message is not None else []
        if not any(segment.get("type") == "reply" for segment in content):
            nodes.append(MessageSegment.node(key))
            continue
        sender = sender_info(message)
        nickname = sender.get("card") or sender.get("nickname") or sender.get("user_id") or "未知发送者"
        native_ids: dict[str, None] = {}
        prepared = await prepare_content(content, key, native_ids)
        # 兼容 SnowLuma 2.2.1 modules/message-actions.ts::parseForwardNodes：
        # node.id 路径未提供引用 ID 解析回调，故含引用时重建节点并显式携带 message。
        # 保留原发送者、时间和原生引用；正文特殊内容通过相邻原消息节点保留。
        nodes.append(MessageSegment.node_custom(
            nickname=str(nickname),
            user_id=str(sender.get("user_id") or client.self_id),
            content=prepared,
        ))
        if message.get("time") is not None:
            nodes[-1].data["time"] = message["time"]
        nodes.extend(MessageSegment.node(native_id) for native_id in native_ids)
    return nodes


async def get_customer_profiles(user_ids: list[int]) -> dict[int, tuple[str, str]]:
    """批量读取客户昵称和好友备注；资料获取失败不影响转发。"""
    friends = {}
    try:
        friends = {
            int(info["user_id"]): info
            for info in await client.call_api("get_friend_list")
        }
    except Exception as e:
        log.warning("获取好友资料失败，将尝试获取客户昵称: %s", e)

    profiles = {}
    for user_id in user_ids:
        info = friends.get(user_id, {})
        nickname = info.get("nickname") or ""
        remark = info.get("remark") or "（无）"
        if not nickname:
            try:
                stranger = await client.call_api("get_stranger_info", user_id=str(user_id))
                nickname = stranger.get("nickname") or ""
            except Exception as e:
                log.warning("获取客户 %d 昵称失败: %s", user_id, e)
        profiles[user_id] = (
            str(nickname or "未知昵称"),
            str(remark if user_id in friends else "（未获取到）"),
        )
    return profiles


def format_customer_profile(user_id: int, nickname: str, remark: str) -> str:
    return f"QQ号：{user_id}\n昵称：{nickname}\n备注：{remark}"


async def send_nested_forward(group_id: int, customer_list: list[tuple[int, CustomerData]], summary_text: str, max_age_seconds: int | None = None) -> int | None:
    """
    构造嵌套合并转发并发送，每个客户的消息通过 get_recent_message_ids 获取最近指定时间内的消息
    """
    if max_age_seconds is None:
        max_age_seconds = cfg.RECENT_MESSAGE_MAX_AGE

    if not customer_list:
        log.debug("send_nested_forward: customer_list 为空，跳过发送")
        return None

    log.info("开始构造合并转发 -> 群 %d, 共 %d 名客户", group_id, len(customer_list))

    profiles = await get_customer_profiles([qq for qq, _ in customer_list])
    outer_nodes: list[MessageSegment] = [
        MessageSegment.node_custom(
            nickname="客服系统提示",
            user_id=str(client.self_id),
            content=message_segments([MessageSegment.text(summary_text)]),
        ),
    ]
    forwarded_customers = []
    customer_message_ids: dict[int, list[int]] = {}
    messages_by_id: dict[str, dict] = {}

    for qq, data in customer_list:
        msg_ids = await get_recent_message_ids(
            qq, count=200, max_age_seconds=max_age_seconds,
            messages_by_id=messages_by_id,
        )
        if not msg_ids:
            log.warning("客户 %d 无%d秒内消息，跳过该客户节点", qq, max_age_seconds)
            continue

        customer_message_ids[qq] = msg_ids
        forwarded_customers.append((qq, data))

    if not forwarded_customers:
        log.warning("所有客户均无%d秒内消息，取消发送合并转发", max_age_seconds)
        return None

    # AI 建议在合并转发第一层独立成条；失败不影响客户资料和历史消息。
    try:
        suggestions = await ai.suggest_for_customers(forwarded_customers)
    except Exception as e:
        log.warning("AI建议生成失败，本次提醒不含建议: %s", e)
        suggestions = {}

    suggestion_count = 0
    for qq, _ in forwarded_customers:
        nickname, remark = profiles[qq]
        outer_nodes.append(
            MessageSegment.node_custom(
                nickname="客户信息",
                user_id=str(client.self_id),
                content=message_segments([
                    MessageSegment.text(format_customer_profile(qq, nickname, remark)),
                ]),
            )
        )
        suggestion = suggestions.get(qq)
        if suggestion:
            outer_nodes.append(
                MessageSegment.node_custom(
                    nickname="AI建议",
                    user_id=str(client.self_id),
                    content=message_segments([
                        MessageSegment.text(f"🤖 AI建议回复（客户 {qq}）：\n{suggestion}"),
                    ]),
                )
            )
            suggestion_count += 1
        outer_nodes.append(
            MessageSegment.node_custom(
                nickname=nickname,
                user_id=str(qq),
                content=message_segments(
                    await build_forward_history_nodes(customer_message_ids[qq], messages_by_id)
                ),
            )
        )
    log.info(
        "提醒合并转发: 独立 AI 建议节点 %d 条 / 客户 %d 名",
        suggestion_count, len(forwarded_customers),
    )

    try:
        response = await client.call_api(
            "send_group_forward_msg",
            group_id=str(group_id),
            messages=outer_nodes,
        )
        message_id = response["message_id"]
        log.info("合并转发发送成功 -> 群 %d, message_id=%s", group_id, message_id)
        asyncio.create_task(add_emoji_to_message(message_id, action_emoji_ids()))
        return message_id
    except Exception as e:
        log.error("发送合并转发失败: %s", e, exc_info=True)
        return None


async def send_forward_from_message_ids(
    group_id: int, title: str, user_id: int, message_ids: list[int],
    *, messages: list[dict] | None = None,
) -> int | None:
    """根据消息ID列表发送合并转发到群"""
    if not message_ids:
        return None
    profiles = await get_customer_profiles([user_id])
    nickname, remark = profiles[user_id]
    history_nodes = await build_forward_history_nodes(
        message_ids,
        {str(m["message_id"]): m for m in messages or [] if "message_id" in m},
    )
    outer_nodes: list[MessageSegment] = [
        MessageSegment.node_custom(
            nickname="客服系统提示",
            user_id=str(client.self_id),
            content=message_segments([
                MessageSegment.text(f"{title}\n\n{format_customer_profile(user_id, nickname, remark)}"),
            ]),
        ),
        MessageSegment.node_custom(
            nickname=nickname,
            user_id=str(user_id),
            content=message_segments(history_nodes),
        )
    ]
    try:
        response = await client.call_api(
            "send_group_forward_msg",
            group_id=str(group_id),
            messages=outer_nodes,
        )
        return response["message_id"]
    except Exception as e:
        log.error("发送合并转发失败: %s", e, exc_info=True)
        return None


async def add_emoji_to_message(message_id: int, emoji_ids: list[int]) -> None:
    """为指定消息添加多个表情贴纸"""
    for emoji_id in emoji_ids:
        try:
            await client.call_api(
                "set_msg_emoji_like", message_id=str(message_id), emoji_id=str(emoji_id), set=True,
            )
            log.debug("已为消息 %s 添加表情 %s", message_id, emoji_id)
        except Exception as e:
            log.error("添加表情失败: message_id=%s, emoji_id=%s, err=%s", message_id, emoji_id, e)


async def remove_emoji_from_message(message_id: int, emoji_ids: list[int]) -> None:
    """移除机器人在指定消息上添加的表情贴纸。"""
    for emoji_id in emoji_ids:
        try:
            await client.call_api(
                "set_msg_emoji_like",
                message_id=str(message_id), emoji_id=str(emoji_id), set=False,
            )
            log.debug("已为消息 %s 移除表情 %s", message_id, emoji_id)
        except Exception as e:
            log.error("移除表情失败: message_id=%s, emoji_id=%s, err=%s", message_id, emoji_id, e)


async def remove_completed_say_emoji(customer_id: int) -> None:
    """清理所有包含该客户的监听消息上的 say 表情。"""
    say_emoji_id = cfg.EMOJI_MAPPING.get("say")
    if say_emoji_id is None:
        return

    message_ids = [
        mid for mid, data in cfg.monitored_forwards.items()
        if customer_id in data["customer_ids"]
    ]
    for message_id in message_ids:
        await remove_emoji_from_message(message_id, [say_emoji_id])


def action_emoji_ids() -> list[int]:
    """可操作消息默认贴上的表情（不含仅用于提示消息的表情）。"""
    return [eid for cmd, eid in cfg.EMOJI_MAPPING.items() if cmd not in ("cancel", "recall", "say_timeout")]


def build_say_feedback(
    customer_id: int,
    closed: bool,
    recallable: bool,
    *,
    recall_hint: bool = True,
) -> str:
    """构造 .say 成功通报文本；recall_hint=False 时不附终端/非表情撤回提示。"""
    feedback = f"✅ 已向客户 {customer_id} 发送消息。" + ("（客户已在待回复队列）" if closed else "（客户不在待回复队列）")
    if recallable and recall_hint:
        feedback += f"\n🧹 {cfg.RECALL_WINDOW_SECONDS} 秒内点击本消息上的撤回表情可撤回该消息"
    return feedback


def register_recallable_send(feedback_msg_id: int, customer_msg_id: int, customer_id: int, group_id: int, operator_id: int) -> None:
    """记录一次可通过表情限时撤回的 .say 发送（以群内反馈消息 ID 为键）"""
    now = time.time()
    # 仅清理超过窗口+宽限期的旧记录；刚过窗口的仍保留，供延迟点击回复超时提示
    expired = [
        mid for mid, d in cfg.recallable_sends.items()
        if now - d["sent_at"] > cfg.RECALL_WINDOW_SECONDS + cfg.RECALL_CLEANUP_GRACE_SECONDS
    ]
    for mid in expired:
        cfg.recallable_sends.pop(mid, None)

    cfg.recallable_sends[feedback_msg_id] = {
        "customer_msg_id": customer_msg_id,
        "customer_id": customer_id,
        "group_id": group_id,
        "sent_at": now,
        "operator_id": operator_id,
    }


async def send_reminder_with_at(
    group_id: int,
    summary: str,
    customer_list: list[tuple[int, CustomerData]],
    max_age_seconds: int | None = None,
    notice_type: str | None = None,
    milestone: int | None = None,
) -> int | None:
    """
    发送提醒：先尝试 @ 一位当前可用成员，再发送合并转发。
    若无可用成员，则直接发送合并转发（无 @）。
    notice_type 用于把本次通知作为 bot_notice 事件记入各客户的会话周期。
    """
    available = get_current_available_members()
    if available:
        chosen = random.choice(available)
        at_msg: list[MessageSegment] = [
            MessageSegment.at(str(chosen)),
            MessageSegment.text(f" {summary}"),
        ]
        try:
            await client.call_api("send_group_msg", group_id=str(group_id), message=at_msg)
            log.info("已发送 @%d 提醒: %s", chosen, summary)
        except Exception as e:
            log.error("发送 @ 提醒失败: %s", e, exc_info=True)
    else:
        log.debug("当前无可用成员，直接发送合并转发")

    message_id = await send_nested_forward(group_id, customer_list, summary, max_age_seconds=max_age_seconds)
    if message_id is not None:
        track_forward_message(
            message_id=message_id,
            customer_ids=[qq for qq, _ in customer_list],
            group_id=group_id,
        )
        if notice_type:
            for qq, _data in customer_list:
                try:
                    await history.record_bot_notice(
                        qq, notice_type, milestone=milestone, group_msg_id=message_id, summary=summary,
                    )
                except Exception as e:
                    log.error("记录客户 %d 的通知事件失败: %s", qq, e)
    return message_id


async def send_and_track_feedback(gid: int, reply_id: int, feedback: str, customer_id: int) -> None:
    """发送带引用的反馈消息，并将其加入监听列表"""
    try:
        resp = await client.call_api(
            "send_group_msg",
            group_id=str(gid),
            message=[MessageSegment.reply(str(reply_id)), MessageSegment.text(feedback)],
        )
        feedback_msg_id = resp.get("message_id")
        if feedback_msg_id:
            track_forward_message(feedback_msg_id, [customer_id], gid)
    except Exception as e:
        log.error("发送反馈消息失败: %s", e)


# ======================= 会话管理 =======================

async def close_session(user_id: int, send_closing: bool = False, close_reason: str = "unknown") -> bool:
    """
    从 unreplied_customers 中移除客户，记录耗时（如果存在）。
    如果 send_closing=True，则向该客户发送结束语。
    close_reason 记录关闭来源（say/bye/close/direct_reply/customer_poke 等），
    并关闭会话周期、按权威时间段触发历史拉取对账。
    返回是否成功结束（即客户原本在队列中）。
    """
    data = cfg.unreplied_customers.pop(user_id, None)
    if data is None:
        return False

    now = time.time()
    pending = data["pending_since"]
    elapsed = now - pending
    cfg.reply_durations.append(elapsed)
    log.info("会话结束: user_id=%s, 耗时=%.1f秒, 原因=%s", user_id, elapsed, close_reason)

    session_id = data.get("session_id")

    if send_closing:
        try:
            resp = await client.call_api(
                "send_private_msg",
                user_id=str(user_id),
                message=cfg.CLOSING_MESSAGE,
            )
            log.info("自动发送结束语成功: user_id=%s", user_id)
            if session_id:
                mid = resp.get("message_id") if resp else None
                await history.record_bot_send(
                    user_id, int(mid) if mid is not None else None,
                    "closing_message", cfg.CLOSING_MESSAGE, session_id=session_id,
                )
        except Exception as e:
            log.error("自动发送结束语失败: user_id=%s, err=%s", user_id, e, exc_info=True)
            if session_id:
                await history.record_bot_send(
                    user_id, None, "closing_message", cfg.CLOSING_MESSAGE, ok=False, session_id=session_id,
                )

    if session_id:
        try:
            await history.close_session_record(session_id, user_id, pending, now, close_reason, data["msg_ids"])
        except Exception as e:
            log.error("关闭会话周期失败: session=%s, err=%s", session_id, e, exc_info=True)

    save_state()
    await remove_completed_say_emoji(user_id)
    return True


async def send_status_panel(group_id: int):
    """向指定群发送机器人状态统计面板"""
    uptime = time.time() - STARTED_AT
    uptime_str = format_duration(uptime)

    pending_count = len(cfg.unreplied_customers)
    total_replies = len(cfg.reply_durations)

    if total_replies > 0:
        min_str = format_duration(min(cfg.reply_durations))
        max_str = format_duration(max(cfg.reply_durations))
        avg_str = format_duration(statistics.mean(cfg.reply_durations))
        median_str = format_duration(statistics.median(cfg.reply_durations))
    else:
        min_str = max_str = avg_str = median_str = "暂无数据"

    monitored_count = len(cfg.monitored_forwards)

    panel = (
        "🤖 机器人状态面板\n"
        f"• 运行时长：{uptime_str}\n"
        f"• 待回复客户数：{pending_count}\n"
        f"• 已完结会话数：{total_replies}\n"
        f"• 最短回复耗时：{min_str}\n"
        f"• 最长回复耗时：{max_str}\n"
        f"• 平均回复耗时：{avg_str}\n"
        f"• 中位数耗时：{median_str}\n"
        f"• 监听合并转发：{monitored_count}\n"
        f"• 延后通知：{len(cfg.delayed_notifications)}\n"
        f"• 静音状态：{mute.describe_mute_status()}\n"
        f"使用 .help 查看可用命令"
    )

    try:
        await client.call_api(
            "send_group_msg",
            group_id=str(group_id),
            message=panel,
        )
        log.debug("状态面板已发送至群 %d", group_id)
    except Exception as e:
        log.error("发送状态面板失败: %s", e, exc_info=True)
