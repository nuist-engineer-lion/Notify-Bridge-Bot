import time
import asyncio
import logging
import statistics
from collections import deque
from typing import Any, TypedDict

from napcat import (
    NapCatClient,
    PrivateMessageEvent,
    FriendRequestEvent,
    GroupMsgEmojiLikeEvent,
    FriendPokeEvent,
    GroupMessageEvent, 
    GroupPokeEvent, 
    Message,
    NodeInline,
    NodeReference,
    Reply,
    Text,
)

# ================= 日志配置 =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("qq-redot")

WS_URL = "ws://10.254.100.21:3001"
WS_TOKEN = "TYnR3DXCeM9H~O.W"

INTERNAL_GROUP_ID = 1056221119 # 内部客服通知群号
WHITELIST = [123456789, 987654321] # 免监控的白名单 QQ
MILESTONES = [5, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 4320] # 迟滞里程碑(分钟)
MONITORED_FORWARD_LIMIT = 10
CLOSING_MESSAGE = "本次会话暂时结束。感谢您的支持与信任，再见。（请勿回复）"

# ================= 新增：程序启动时间 =================
STARTED_AT = time.time()

# ================= 新增：回复耗时记录（秒） =================
REPLY_DURATION_MAXLEN = 1000  # 保留最近 N 次回复耗时，防止列表无限增长
reply_durations: deque[float] = deque(maxlen=REPLY_DURATION_MAXLEN)

class CustomerData(TypedDict):
    last_active: float
    msg_ids: list[int]
    is_newly_reported: bool
    reported_milestones: set[int]
    pending_since: float           # 新增：本轮开始等待回复的时间戳


class ForwardMonitorData(TypedDict):
    customer_ids: list[int]
    group_id: int
    created_at: float

# 内存字典：存储未回复的客户状态
unreplied_customers: dict[int, CustomerData] = {}
monitored_forward_order: deque[int] = deque()
monitored_forwards: dict[int, ForwardMonitorData] = {}

# 客户端对象
client: NapCatClient = NapCatClient(WS_URL, WS_TOKEN)


def serialize_message_segments(segments: list[Message]) -> Any:
    """将 SDK 消息段序列化为 NodeInline 所需的原始 OB11 结构。"""
    return [dict(segment) for segment in segments]


def track_forward_message(message_id: int, customer_ids: list[int], group_id: int) -> None:
    if message_id in monitored_forwards:
        try:
            monitored_forward_order.remove(message_id)
        except ValueError:
            pass

    monitored_forward_order.append(message_id)
    monitored_forwards[message_id] = {
        "customer_ids": customer_ids,
        "group_id": group_id,
        "created_at": time.time(),
    }

    while len(monitored_forward_order) > MONITORED_FORWARD_LIMIT:
        expired_id = monitored_forward_order.popleft()
        monitored_forwards.pop(expired_id, None)

    log.debug("监听合并转发已更新: message_id=%s, 当前监听数=%d", message_id, len(monitored_forward_order))


def pop_tracked_forward(message_id: int) -> ForwardMonitorData | None:
    data = monitored_forwards.pop(message_id, None)
    if data is None:
        return None

    try:
        monitored_forward_order.remove(message_id)
    except ValueError:
        pass
    return data

# ================= 新增：格式化时长 =================
def format_duration(seconds: float) -> str:
    """将秒数转换为易读的文本（如 3分12秒、1小时05分）"""
    if seconds < 60:
        return f"{int(seconds)}秒"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}分{secs:02d}秒"
    elif seconds < 86400:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}小时{mins:02d}分"
    else:
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        return f"{days}天{hours}小时"

# ================= 新增：结束会话统一处理 =================
async def close_session(user_id: int, send_closing: bool = False) -> bool:
    """
    从 unreplied_customers 中移除客户，记录耗时（如果存在）。
    如果 send_closing=True，则向该客户发送结束语。
    返回是否成功结束（即客户原本在队列中）。
    """
    data = unreplied_customers.pop(user_id, None)
    if data is None:
        return False

    # 记录回复耗时
    pending = data["pending_since"]
    elapsed = time.time() - pending
    reply_durations.append(elapsed)
    log.info("会话结束: user_id=%s, 耗时=%.1f秒", user_id, elapsed)

    if send_closing:
        try:
            await client.send_private_msg(
                user_id=str(user_id),
                message=CLOSING_MESSAGE,
            )
            log.info("自动发送结束语成功: user_id=%s", user_id)
        except Exception as e:
            log.error("自动发送结束语失败: user_id=%s, err=%s", user_id, e, exc_info=True)

    return True

# ================= 新增：发送状态面板 =================
async def send_status_panel(group_id: int):
    """向指定群发送机器人状态统计面板"""
    uptime = time.time() - STARTED_AT
    uptime_str = format_duration(uptime)

    pending_count = len(unreplied_customers)
    total_replies = len(reply_durations)

    # 计算统计值
    if total_replies > 0:
        min_dur = min(reply_durations)
        max_dur = max(reply_durations)
        avg_dur = statistics.mean(reply_durations)
        median_dur = statistics.median(reply_durations)

        min_str = format_duration(min_dur)
        max_str = format_duration(max_dur)
        avg_str = format_duration(avg_dur)
        median_str = format_duration(median_dur)
    else:
        min_str = max_str = avg_str = median_str = "暂无数据"

    # 可选：当前监听中的合并转发数量
    monitored_count = len(monitored_forwards)

    panel = (
        "🤖 机器人状态面板\n"
        f"• 运行时长：{uptime_str}\n"
        f"• 待回复客户数：{pending_count}\n"
        f"• 已完结会话数：{total_replies}\n"
        f"• 最短回复耗时：{min_str}\n"
        f"• 最长回复耗时：{max_str}\n"
        f"• 平均回复耗时：{avg_str}\n"
        f"• 中位数耗时：{median_str}\n"
        f"• 监听合并转发：{monitored_count}"
    )

    try:
        await client.send_group_msg(
            group_id=str(group_id),
            message=panel,
        )
        log.info("状态面板已发送至群 %d", group_id)
    except Exception as e:
        log.error("发送状态面板失败: %s", e, exc_info=True)


# ================= 辅助函数：构造嵌套合并转发 =================
async def send_nested_forward(group_id: int, customer_list: list[tuple[int, CustomerData]], summary_text: str) -> int | None:
    """
    构造嵌套合并转发并发送
    customer_list: list[tuple[int, CustomerData]]
    """
    if not customer_list:
        log.debug("send_nested_forward: customer_list 为空，跳过发送")
        return None

    log.info("开始构造合并转发 -> 群 %d, 共 %d 名客户", group_id, len(customer_list))
    outer_nodes: list[Message] = [
        NodeInline(
            nickname="客服系统提示",
            user_id=str(client.self_id),
            content=serialize_message_segments([Text(text=summary_text)]),
        ),
        NodeInline(
            nickname="快捷传送门",
            user_id=str(client.self_id),
            content=serialize_message_segments([Text(text="https://lion-qq.laysath.cn")]),
        ),
    ]

    # 3. 为每个客户构造一个“子合并转发”节点
    for qq, data in customer_list:
        # 内层节点：该客户的所有消息 ID (使用 id 引用不需要 content)
        inner_nodes: list[Message] = [
            NodeReference(id=str(msg_id))
            for msg_id in data["msg_ids"]
        ]

        outer_nodes.append(
            NodeInline(
                nickname="客户",
                user_id=str(qq),
                content=serialize_message_segments(inner_nodes),
            )
        )

    # 调用 SDK 混入的 send_group_forward_msg
    log.debug("合并转发节点数: %d (1 摘要 + %d 客户)", len(outer_nodes), len(customer_list))
    try:
        response = await client.send_group_forward_msg(
            group_id=str(group_id),
            messages=outer_nodes,
        )
        message_id = response["message_id"]
        log.info("合并转发发送成功 -> 群 %d, message_id=%s", group_id, message_id)
        return message_id
    except Exception as e:
        log.error("发送合并转发失败: %s", e, exc_info=True)
        return None

# ================= 核心逻辑：定时巡检任务 =================
async def monitor_loop():
    """每分钟执行一次的巡检死循环"""
    log.info("巡检任务已启动，每 60 秒执行一次")
    while True:
        await asyncio.sleep(60)
        
        if not client.is_running:
            log.debug("巡检跳过: 客户端未运行")
            continue
        if not unreplied_customers:
            log.debug("巡检跳过: 当前无未回复客户")
            continue

        log.info("===== 巡检开始 ===== 当前未回复客户数: %d", len(unreplied_customers))
        now = time.time()
        new_customers_to_report: list[tuple[int, CustomerData]] = []

        # ================= 阶段 1：新客户防抖通报 =================
        # 筛选防抖时间 >= 1分钟且未报过的新客户
        for qq, data in unreplied_customers.items():
            elapsed_min = (now - data["last_active"]) / 60.0
            if not data["is_newly_reported"] and elapsed_min >= 1.0:
                new_customers_to_report.append((qq, data))

        if new_customers_to_report:
            log.info("阶段1: 发现 %d 名新客户待通报: %s",
                     len(new_customers_to_report),
                     [qq for qq, _ in new_customers_to_report])
            # 标记已通报
            for qq, data in new_customers_to_report:
                data["is_newly_reported"] = True
            
            # 按时间更近的优先排序 (last_active 越大越靠前)
            new_customers_to_report.sort(key=lambda x: x[1]["last_active"], reverse=True)
            
            # 建议只通报新触发的客户，避免每次都全量刷屏
            summary = f"📢 刚刚有 {len(new_customers_to_report)} 名客户发来消息，请及时回复！"
            message_id = await send_nested_forward(INTERNAL_GROUP_ID, new_customers_to_report, summary)
            if message_id is not None:
                track_forward_message(
                    message_id=message_id,
                    customer_ids=[qq for qq, _ in new_customers_to_report],
                    group_id=INTERNAL_GROUP_ID,
                )
        else:
            log.debug("阶段1: 无新客户需要通报")
            
        # ================= 阶段 2：迟滞里程碑通报 =================

        # 初始化按里程碑分组的字典
        milestone_groups: dict[int, list[tuple[int, CustomerData]]] = {m: [] for m in MILESTONES}

        for qq, data in unreplied_customers.items():
            elapsed_min = (now - data["last_active"]) / 60.0
            log.debug("  客户 %d: 已等待 %.1f 分钟, 消息数 %d", qq, elapsed_min, len(data["msg_ids"]))

            # 找到当前已达到的最大里程碑，仅在该里程碑尚未播报时触发
            for m in sorted(MILESTONES, reverse=True):
                if elapsed_min >= m:
                    if m not in data["reported_milestones"]:
                        data["reported_milestones"].add(m)
                        log.debug("    -> 命中里程碑 %d 分钟", m)
                        milestone_groups[m].append((qq, data))
                    break
        
        # 检查各列表，如果不为空则单独通报
        for m, delay_list in milestone_groups.items():
            if delay_list:
                # 排序：时间更近的优先
                delay_list.sort(key=lambda x: x[1]["last_active"], reverse=True)
                
                unit = f"{m}分钟" if m < 60 else f"{m//60}小时"
                log.warning("阶段2: 里程碑 %s 触发, 涉及 %d 名客户: %s",
                            unit, len(delay_list), [qq for qq, _ in delay_list])
                summary = f"⚠️ 以下 {len(delay_list)} 名客户已等待长达 {unit}！"
                message_id = await send_nested_forward(INTERNAL_GROUP_ID, delay_list, summary)
                if message_id is not None:
                    track_forward_message(
                        message_id=message_id,
                        customer_ids=[qq for qq, _ in delay_list],
                        group_id=INTERNAL_GROUP_ID,
                    )

        log.info("===== 巡检结束 =====")

async def resolve_target_from_reply(reply_id: int) -> tuple[list[int] | None, str | None]:
    """
    根据被引用的消息ID解析对应的客户列表。
    返回 (customer_ids, error_message)，若成功则 error_message 为 None。
    """
    # 先从监听的合并转发中查找
    data = monitored_forwards.get(reply_id)
    if data is not None:
        return data["customer_ids"], None

    # 未找到，尝试查询消息详情
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


async def send_forward_from_message_ids(group_id: int, title: str, user_id: int, message_ids: list[int]) -> int | None:
    """根据消息ID列表发送合并转发到群"""
    if not message_ids:
        return None
    outer_nodes: list[Message] = [
        NodeInline(
            nickname="客服系统提示",
            user_id=str(client.self_id),
            content=serialize_message_segments([Text(text=title)]),
        ),
        NodeInline(
            nickname="消息记录",
            user_id=str(user_id),
            content=serialize_message_segments([
                NodeReference(id=str(msg_id)) for msg_id in message_ids
            ]),
        )
    ]
    try:
        response = await client.send_group_forward_msg(
            group_id=str(group_id),
            messages=outer_nodes,
        )
        return response["message_id"]
    except Exception as e:
        log.error("发送合并转发失败: %s", e, exc_info=True)
        return None

async def main():
    log.info("程序启动, WS_URL=%s, 通知群=%d, 白名单=%s", WS_URL, INTERNAL_GROUP_ID, WHITELIST)
    log.info("里程碑阈值(分钟): %s", MILESTONES)

    # 启动定时巡检后台任务
    asyncio.create_task(monitor_loop())

    # 自动重连
    while True:
        log.info("正在连接 WebSocket...")
        async for event in client:
            log.debug("收到事件: type=%s, post_type=%s", type(event).__name__, getattr(event, 'post_type', '?'))
            match event:
                # 0. 自动通过好友申请
                case FriendRequestEvent(user_id=uid, comment=comment):
                    try:
                        await event.approve()
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
                # 1. 侦听内部群贴表情操作
                case GroupMsgEmojiLikeEvent(group_id=gid, message_id=mid, is_add=True) if gid == INTERNAL_GROUP_ID:
                    tracked_data = pop_tracked_forward(mid)
                    if tracked_data is not None:
                        customer_ids = tracked_data["customer_ids"]
                        if len(customer_ids) == 1:
                            customer_id = customer_ids[0]
                            # 结束会话（发送结束语并移除）
                            closed = await close_session(customer_id, send_closing=True)
                            if not closed:
                                log.warning("客户 %s 不在待回复队列中，但已发送结束语", customer_id)

                            feedback_text = (
                                f"✅ 已对客户 {customer_id} 发送结束语。"
                                if closed
                                else f"⚠️ 客户 {customer_id} 不在待回复队列，但已发送结束语。"
                            )
                            try:
                                await client.send_group_msg(
                                    group_id=str(gid),
                                    message=[Reply(id=str(mid)), Text(text=feedback_text)],
                                )
                            except Exception as feedback_err:
                                log.error("结束语群反馈失败: source_mid=%s, err=%s", mid, feedback_err, exc_info=True)
                        else:
                            try:
                                await client.send_group_msg(
                                    group_id=str(gid),
                                    message=[Reply(id=str(mid)), Text(text="暂不支持：该合并转发包含多个客户，请手动处理。")],
                                )
                            except Exception as unsupported_err:
                                log.error("多客户暂不支持反馈失败: source_mid=%s, err=%s", mid, unsupported_err, exc_info=True)
                    else:
                        try:
                            msg_detail = await client.get_msg(message_id=str(mid))
                            source_uid = int(str(msg_detail.get("user_id", 0)))
                            self_uid = int(str(client.self_id))
                            if source_uid == self_uid:
                                await client.send_group_msg(
                                    group_id=str(gid),
                                    message=[Reply(id=str(mid)), Text(text="操作已过期")],
                                )
                        except Exception as expired_err:
                            log.error("过期操作检测/反馈失败: source_mid=%s, err=%s", mid, expired_err, exc_info=True)

                # 2. 侦听客服的回复（清理字典并记录耗时）
                case PrivateMessageEvent(post_type="message_sent", target_id=tid) if tid in unreplied_customers:
                    await close_session(tid, send_closing=False)   # 客服已经回复，无需再次发送结束语
                    log.info("客服已回复 %s，移除提醒并记录耗时。剩余未回复: %d", tid, len(unreplied_customers))

                # 3. 接收客户消息 (加入/更新字典)
                case PrivateMessageEvent(post_type="message", user_id=uid, message_id=msg_id) if int(uid) not in WHITELIST:
                    uid = int(uid)
                    now = time.time()
                    if uid not in unreplied_customers:
                        unreplied_customers[uid] = {
                            "last_active": now,
                            "msg_ids": [msg_id],
                            "is_newly_reported": False,
                            "reported_milestones": set(),
                            "pending_since": now,          # 设置本轮开始时间
                        }
                        log.info("新增客户 %d 进入待回复队列 (msg_id=%s)。当前队列长度: %d",
                                 uid, msg_id, len(unreplied_customers))
                    else:
                        unreplied_customers[uid]["last_active"] = now
                        unreplied_customers[uid]["msg_ids"].append(msg_id)
                        unreplied_customers[uid]["is_newly_reported"] = False
                        unreplied_customers[uid]["reported_milestones"].clear()
                        # 注意：不更新 pending_since，保持本轮开始时间不变
                        log.info("客户 %d 追加消息 (msg_id=%s)，累计 %d 条，重置通报倒计时。",
                                 uid, msg_id, len(unreplied_customers[uid]["msg_ids"]))

                # 4. 侦听私聊戳一戳（自动发送结束语，并从队列移除）
                case FriendPokeEvent(sender_id=sid, target_id=tid) if tid == client.self_id and sid not in WHITELIST:
                    closed = await close_session(sid, send_closing=True)
                    if closed:
                        log.info("私聊戳一戳结束会话: user_id=%s", sid)
                    else:
                        # 客户不在队列中，但已发送结束语（根据原有逻辑）
                        log.info("私聊戳一戳自动发送结束语（客户不在队列）: user_id=%s", sid)

                # 5. 新增：内部群戳一戳 -> 发送状态面板
                case GroupPokeEvent(group_id=gid, target_id=tid) if gid == INTERNAL_GROUP_ID and tid == client.self_id:
                    log.info("内部群戳一戳触发状态面板: group=%d", gid)
                    await send_status_panel(gid)
                case GroupMessageEvent(group_id=gid, message=segments, message_id=msg_id) if gid == INTERNAL_GROUP_ID:
                    # 解析引用和命令
                    reply_id = None
                    cmd_parts: list[str] = []
                    for seg in segments:
                        if isinstance(seg, Reply) and seg.id is not None:
                            reply_id = int(seg.id)          # Reply.id 是字符串，转 int
                        elif isinstance(seg, Text):
                            cmd_parts.append(seg.text)
                    if reply_id is None or not cmd_parts:
                        continue

                    cmd_text = ''.join(cmd_parts).strip()
                    log.debug("群命令: reply_id=%s, cmd=%s", reply_id, cmd_text)

                    # 解析目标客户
                    customer_ids, err_msg = await resolve_target_from_reply(reply_id)
                    if err_msg:
                        await client.send_group_msg(
                            group_id=str(gid),
                            message=[Reply(id=str(msg_id)), Text(text=err_msg)],
                        )
                        continue

                    # 防御性检查：customer_ids 必须非空
                    if not customer_ids:
                        await client.send_group_msg(
                            group_id=str(gid),
                            message=[Reply(id=str(msg_id)), Text(text="内部错误：无法解析客户列表")],
                        )
                        continue

                    if len(customer_ids) != 1:
                        await client.send_group_msg(
                            group_id=str(gid),
                            message=[Reply(id=str(msg_id)), Text(text="暂不支持：该合并转发包含多个客户，请手动处理。")],
                        )
                        continue

                    customer_id = customer_ids[0]

                    # 根据命令分发
                    if cmd_text.startswith(".say"):
                        # 提取 .say 后的内容
                        content = cmd_text[4:].strip()   # 去掉 ".say"
                        if not content:
                            await client.send_group_msg(
                                group_id=str(gid),
                                message=[Reply(id=str(msg_id)), Text(text=".say 命令后需要附带要发送的消息内容")],
                            )
                            continue

                        # 发送私聊消息
                        try:
                            await client.send_private_msg(
                                user_id=str(customer_id),
                                message=content,
                            )
                            # 结束会话（不移除监听记录，因为后续可能还有操作？但为了统一，结束类命令应移除监听）
                            closed = await close_session(customer_id, send_closing=False)
                            pop_tracked_forward(reply_id)   # 移除原转发的监听
                            feedback = f"✅ 已向客户 {customer_id} 发送消息。" + ("（客户已在待回复队列）" if closed else "（客户不在待回复队列）")
                        except Exception as e:
                            log.error("发送私聊消息失败: customer=%s, err=%s", customer_id, e, exc_info=True)
                            feedback = f"❌ 发送失败：{e}"
                        # 发送群反馈，并将其加入监听
                        resp = await client.send_group_msg(
                            group_id=str(gid),
                            message=[Reply(id=str(msg_id)), Text(text=feedback)],
                        )
                        feedback_msg_id = resp.get("message_id")
                        if feedback_msg_id:
                            track_forward_message(feedback_msg_id, [customer_id], gid)

                    elif cmd_text.startswith(".bye"):
                        # 发送结束语
                        try:
                            await client.send_private_msg(
                                user_id=str(customer_id),
                                message=CLOSING_MESSAGE,
                            )
                            closed = await close_session(customer_id, send_closing=False)
                            pop_tracked_forward(reply_id)
                            feedback = f"✅ 已向客户 {customer_id} 发送结束语。" + ("（客户已在待回复队列）" if closed else "（客户不在待回复队列）")
                        except Exception as e:
                            log.error("发送结束语失败: customer=%s, err=%s", customer_id, e, exc_info=True)
                            feedback = f"❌ 发送失败：{e}"
                        # 发送群反馈，并将其加入监听
                        resp = await client.send_group_msg(
                            group_id=str(gid),
                            message=[Reply(id=str(msg_id)), Text(text=feedback)],
                        )
                        feedback_msg_id = resp.get("message_id")
                        if feedback_msg_id:
                            track_forward_message(feedback_msg_id, [customer_id], gid)

                    elif cmd_text.startswith(".more"):
                        # 获取最近100条聊天记录
                        try:
                            resp = await client.get_friend_msg_history(
                                user_id=str(customer_id),
                                count=100,
                                parse_mult_msg=True,       # 可选，确保合并转发被解析
                            )
                            messages = resp.get("messages", [])
                            if not messages:
                                await client.send_group_msg(
                                    group_id=str(gid),
                                    message=[Reply(id=str(msg_id)), Text(text=f"客户 {customer_id} 暂无更多历史消息")],
                                )
                                continue

                            # 按时间正序排序（假设返回的是倒序）
                            messages.sort(key=lambda m: m.get("time", 0))
                            msg_ids = [m["message_id"] for m in messages if "message_id" in m]

                            # 构造合并转发
                            title = f"客户 {customer_id} 的最近 {len(msg_ids)} 条消息"
                            new_fwd_id = await send_forward_from_message_ids(
                                group_id=gid,
                                title=title,
                                user_id=customer_id,
                                message_ids=msg_ids,
                            )
                            if new_fwd_id:
                                # 将新合并转发加入监听
                                track_forward_message(new_fwd_id, [customer_id], gid)
                                feedback = f"✅ 已发送客户 {customer_id} 的历史消息合并转发"
                            else:
                                feedback = f"❌ 构造合并转发失败"
                        except Exception as e:
                            log.error("获取历史消息失败: customer=%s, err=%s", customer_id, e, exc_info=True)
                            feedback = f"❌ 获取历史消息失败：{e}"
                        # 发送群反馈，并将其加入监听
                        resp = await client.send_group_msg(
                            group_id=str(gid),
                            message=[Reply(id=str(msg_id)), Text(text=feedback)],
                        )
                        feedback_msg_id = resp.get("message_id")
                        if feedback_msg_id:
                            track_forward_message(feedback_msg_id, [customer_id], gid)

                    else:
                        # 不是我们关心的命令，忽略
                        continue

                case _:
                    continue

        log.warning("连接断开或出错，5秒后重连...")
        await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("程序已手动停止。")
