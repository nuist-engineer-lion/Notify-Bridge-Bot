import time
import asyncio
import logging
import statistics
import random
import yaml
import json
import os
from datetime import datetime, timedelta
from collections import deque
from typing import Any, TypedDict, Iterable, Union, cast

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
    At,
    UnknownMessageSegment,
)

# ================= 日志配置 =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("qq-redot")

# ================= 加载配置 =================
def load_config(path: str = "config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # 转换 availability 中的时段列表为元组
    avail = {}
    for qq, days in cfg["availability"].items():
        avail[int(qq)] = {}
        for day, slots in days.items():
            avail[int(qq)][day] = [tuple(slot) for slot in slots]
    cfg["availability"] = avail
    return cfg

config = load_config()

WS_URL = config["ws_url"]
WS_TOKEN = config["ws_token"]
INTERNAL_GROUP_ID = config["internal_group_id"]
WHITELIST = config["whitelist"]
MILESTONES = config["milestones"]
MONITORED_FORWARD_LIMIT = config["monitored_forward_limit"]
CLOSING_MESSAGE = config["closing_message"]
DEBOUNCE_SECONDS = config["debounce_seconds"]
PROCESSED_FRIEND_REQUESTS_EXPIRE = config["processed_friend_requests_expire"]
REPLY_DURATION_MAXLEN = config["reply_duration_maxlen"]
AVAILABILITY = config["availability"]
MAX_LISTEN_AGE = config.get("max_listen_age", 86400)   # 24小时
EMOJI_MAPPING = config.get("emoji_mapping", {"close": 128, "more": 127, "bye": 100})
EMOJI_TO_CMD = {v: k for k, v in EMOJI_MAPPING.items()}

# ================= 夜间模式配置 =================
NIGHT_MODE = config.get("night_mode", {})
NIGHT_START = NIGHT_MODE.get("start", "22:00")
NIGHT_END = NIGHT_MODE.get("end", "08:00")
NIGHT_SUMMARY_TIME = NIGHT_MODE.get("summary_time", "08:00")

# ================= 可配置的存档与状态持久化 =================
ARCHIVE_DIR = config.get("archive_dir", "archives")
STATE_FILE = config.get("state_file", "state.json")
RECENT_MESSAGE_MAX_AGE = config.get("recent_message_max_age", 86400)  # 默认1天

# ================= 星期映射 =================
WEEKDAY_MAP = {
    0: "monday",
    1: "tuesday",
    2: "wednesday",
    3: "thursday",
    4: "friday",
    5: "saturday",
    6: "sunday",
}

# ================= 程序启动时间 =================
STARTED_AT = time.time()

# 好友申请去重缓存（flag -> 处理时间戳）
processed_friend_requests: dict[str, float] = {}

# 记录用户通过好友申请的时间戳（用于忽略刚通过申请后的第一条消息）
friend_approve_time: dict[int, float] = {}

# ================= 回复耗时记录（秒） =================
reply_durations: deque[float] = deque(maxlen=REPLY_DURATION_MAXLEN)

class CustomerData(TypedDict):
    last_active: float
    msg_ids: list[int]
    is_newly_reported: bool
    reported_milestones: set[int]
    pending_since: float  # 本轮开始等待回复的时间戳


class ForwardMonitorData(TypedDict):
    customer_ids: list[int]
    group_id: int
    created_at: float

class DelayedNotification(TypedDict):
    type: str
    customers: list[tuple[int, CustomerData]]
    milestone: int | None
    timestamp: float

# 用于存档的消息记录类型（简化）
MessageRecord = dict[str, Any]

# ================= 状态文件结构定义（用于精确类型恢复） =================
class StateCustomerData(TypedDict):
    last_active: float
    msg_ids: list[int]
    is_newly_reported: bool
    reported_milestones: list[int]  # JSON 中为 list
    pending_since: float

class StateForwardData(TypedDict):
    customer_ids: list[int]
    group_id: int
    created_at: float

class StateDelayedNotification(TypedDict):
    type: str
    customers: list[tuple[int, StateCustomerData]]  # 存储的是 StateCustomerData
    milestone: int | None
    timestamp: float

class AppState(TypedDict):
    unreplied_customers: dict[str, StateCustomerData]
    monitored_forwards: dict[str, StateForwardData]
    monitored_forward_order: list[int]
    last_command_time: dict[str, float]
    delayed_notifications: list[StateDelayedNotification]
    last_night_summary_sent_date: str

# 内存字典：存储未回复的客户状态
unreplied_customers: dict[int, CustomerData] = {}
monitored_forward_order: deque[int] = deque()
monitored_forwards: dict[int, ForwardMonitorData] = {}
last_command_time: dict[tuple[int, str], float] = {}

# 夜间通知延后缓存
delayed_notifications: list[DelayedNotification] = []
last_night_summary_sent_date: str = ""

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
    save_state()  # 状态变更后持久化


def pop_tracked_forward(message_id: int) -> ForwardMonitorData | None:
    """移除监听的合并转发，并清理其防抖记录"""
    data = monitored_forwards.pop(message_id, None)
    if data is None:
        return None

    try:
        monitored_forward_order.remove(message_id)
    except ValueError:
        pass

    # 清理该消息相关的所有防抖记录
    keys_to_remove = [key for key in last_command_time if key[0] == message_id]
    for key in keys_to_remove:
        del last_command_time[key]

    save_state()  # 状态变更后持久化
    return data


# ================= 格式化时长 =================
def format_duration(seconds: float) -> str:
    """将秒数转换为易读的文本"""
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


# ================= 本地存档 =================
async def archive_session(user_id: int, msg_ids: list[int], pending_since: float) -> None:
    """将会话记录存档到本地 JSON 文件"""
    if not msg_ids:
        log.warning(f"存档跳过：客户 {user_id} 无消息记录")
        return

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"{user_id}_{date_str}.json"
    filepath = os.path.join(ARCHIVE_DIR, filename)

    messages_data: list[MessageRecord] = []
    for msg_id in msg_ids:
        try:
            msg_detail = await client.get_msg(message_id=str(msg_id))
            record: MessageRecord = {
                "message_id": msg_id,
                "time": msg_detail.get("time"),
                "sender_nickname": msg_detail.get("sender", {}).get("nickname", ""),
                "sender_id": msg_detail.get("sender", {}).get("user_id"),
                "message": msg_detail.get("message", []),
            }
            messages_data.append(record)
        except Exception as e:
            log.error(f"存档时获取消息 {msg_id} 失败: {e}")

    messages_data.sort(key=lambda x: x.get("time", 0))

    existing_sessions: list[MessageRecord] = []
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, list):
                    existing_sessions = cast(list[MessageRecord], loaded)
        except Exception:
            pass

    session_record: MessageRecord = {
        "session_closed_at": time.time(),
        "pending_duration": time.time() - pending_since,
        "message_count": len(messages_data),
        "messages": messages_data
    }
    existing_sessions.append(session_record)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(existing_sessions, f, ensure_ascii=False, indent=2)
        log.info(f"会话已存档: {filepath}")
    except Exception as e:
        log.error(f"会话存档写入失败: {e}")


# ================= 状态持久化 =================
def save_state() -> None:
    """将当前内存状态保存到本地文件，以便重启后恢复"""
    # 转换 set 为 list 以便 JSON 序列化
    serializable_customers: dict[str, StateCustomerData] = {}
    for qq, data in unreplied_customers.items():
        serializable_customers[str(qq)] = {
            "last_active": data["last_active"],
            "msg_ids": data["msg_ids"],
            "is_newly_reported": data["is_newly_reported"],
            "reported_milestones": list(data["reported_milestones"]),
            "pending_since": data["pending_since"],
        }

    # 转换 monitored_forwards 的键为字符串
    serializable_forwards: dict[str, StateForwardData] = {
        str(mid): data for mid, data in monitored_forwards.items()
    }

    # 转换 last_command_time 的键为字符串
    serializable_last_cmd: dict[str, float] = {}
    for (msg_id, cmd), ts in last_command_time.items():
        serializable_last_cmd[f"{msg_id}_{cmd}"] = ts

    # 转换 delayed_notifications 中的 CustomerData 为 StateCustomerData
    serializable_delayed: list[StateDelayedNotification] = []
    for notif in delayed_notifications:
        customers_state: list[tuple[int, StateCustomerData]] = []
        for qq, cust in notif["customers"]:
            cust_state: StateCustomerData = {
                "last_active": cust["last_active"],
                "msg_ids": cust["msg_ids"],
                "is_newly_reported": cust["is_newly_reported"],
                "reported_milestones": list(cust["reported_milestones"]),
                "pending_since": cust["pending_since"],
            }
            customers_state.append((qq, cust_state))
        serializable_delayed.append({
            "type": notif["type"],
            "customers": customers_state,
            "milestone": notif["milestone"],
            "timestamp": notif["timestamp"],
        })

    state: AppState = {
        "unreplied_customers": serializable_customers,
        "monitored_forwards": serializable_forwards,
        "monitored_forward_order": list(monitored_forward_order),
        "last_command_time": serializable_last_cmd,
        "delayed_notifications": serializable_delayed,
        "last_night_summary_sent_date": last_night_summary_sent_date,
    }

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        log.debug("状态已保存至 %s", STATE_FILE)
    except Exception as e:
        log.error("状态保存失败: %s", e, exc_info=True)


def load_state() -> None:
    """从本地文件恢复状态"""
    global last_night_summary_sent_date
    if not os.path.exists(STATE_FILE):
        log.info("未找到状态文件 %s，将使用全新状态启动", STATE_FILE)
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # 使用 TypedDict 进行类型断言
        state = cast(AppState, raw)
    except Exception as e:
        log.error("状态文件读取失败: %s", e, exc_info=True)
        return

    # 恢复 unreplied_customers
    for qq_str, cust_state in state.get("unreplied_customers", {}).items():
        qq = int(qq_str)
        unreplied_customers[qq] = {
            "last_active": cust_state["last_active"],
            "msg_ids": cust_state["msg_ids"],
            "is_newly_reported": cust_state["is_newly_reported"],
            "reported_milestones": set(cust_state["reported_milestones"]),
            "pending_since": cust_state["pending_since"],
        }

    # 恢复 monitored_forwards
    for mid_str, fwd_state in state.get("monitored_forwards", {}).items():
        mid = int(mid_str)
        monitored_forwards[mid] = {
            "customer_ids": fwd_state["customer_ids"],
            "group_id": fwd_state["group_id"],
            "created_at": fwd_state["created_at"],
        }
    monitored_forward_order.extend(state.get("monitored_forward_order", []))

    # 恢复 last_command_time
    for key_str, ts in state.get("last_command_time", {}).items():
        parts = key_str.split("_")
        if len(parts) == 2:
            msg_id = int(parts[0])
            cmd = parts[1]
            last_command_time[(msg_id, cmd)] = ts

    # 恢复 delayed_notifications
    for notif_state in state.get("delayed_notifications", []):
        customers: list[tuple[int, CustomerData]] = []
        for qq, cust_state in notif_state["customers"]:
            cust: CustomerData = {
                "last_active": cust_state["last_active"],
                "msg_ids": cust_state["msg_ids"],
                "is_newly_reported": cust_state["is_newly_reported"],
                "reported_milestones": set(cust_state["reported_milestones"]),
                "pending_since": cust_state["pending_since"],
            }
            customers.append((qq, cust))
        delayed_notifications.append({
            "type": notif_state["type"],
            "customers": customers,
            "milestone": notif_state["milestone"],
            "timestamp": notif_state["timestamp"],
        })

    last_night_summary_sent_date = state.get("last_night_summary_sent_date", "")

    log.info("状态恢复完成：待回复客户 %d 人，监听转发 %d 条",
             len(unreplied_customers), len(monitored_forwards))


# ================= 结束会话统一处理 =================
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

    # 异步存档，不阻塞主流程
    asyncio.create_task(archive_session(user_id, data["msg_ids"], pending))

    if send_closing:
        try:
            await client.send_private_msg(
                user_id=str(user_id),
                message=CLOSING_MESSAGE,
            )
            log.info("自动发送结束语成功: user_id=%s", user_id)
        except Exception as e:
            log.error("自动发送结束语失败: user_id=%s, err=%s", user_id, e, exc_info=True)

    save_state()  # 状态变更后持久化
    return True


# ================= 发送状态面板 =================
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

    # 当前监听中的合并转发数量
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
        f"• 监听合并转发：{monitored_count}\n"
        f"使用 .help 查看可用命令"
    )

    try:
        await client.send_group_msg(
            group_id=str(group_id),
            message=panel,
        )
        log.info("状态面板已发送至群 %d", group_id)
    except Exception as e:
        log.error("发送状态面板失败: %s", e, exc_info=True)

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

# ================= 辅助函数：获取客户最近的消息ID列表 =================
async def get_recent_message_ids(user_id: int, count: int = 200, max_age_seconds: int = RECENT_MESSAGE_MAX_AGE) -> list[int]:
    """
    通过 API 获取与指定用户的最近消息，筛选出最近 max_age_seconds 秒内的消息（双方消息）。
    返回按时间正序排列的 message_id 列表。
    """
    now = time.time()
    try:
        resp = await client.get_friend_msg_history(
            user_id=str(user_id),
            count=count,
            parse_mult_msg=True,
        )
        messages = resp.get("messages", [])
        if not messages:
            return []
        # 按时间正序排序
        messages.sort(key=lambda m: m.get("time", 0))
        # 筛选最近 max_age_seconds 秒内的消息
        filtered = [
            m for m in messages
            if (now - m.get("time", 0)) <= max_age_seconds
        ]
        msg_ids = [m["message_id"] for m in filtered if "message_id" in m]
        log.debug("客户 %d 历史消息: 获取到 %d 条，过滤后 %d 条（%d 秒内）",
                  user_id, len(messages), len(msg_ids), max_age_seconds)
        return msg_ids
    except Exception as e:
        log.error("获取客户 %d 历史消息失败: %s", user_id, e, exc_info=True)
        return []
    
# ================= 辅助函数：构造嵌套合并转发 =================
async def send_nested_forward(group_id: int, customer_list: list[tuple[int, CustomerData]], summary_text: str) -> int | None:
    """
    构造嵌套合并转发并发送，每个客户的消息通过 get_recent_message_ids 获取最近指定时间内的消息
    """
    if not customer_list:
        log.debug("send_nested_forward: customer_list 为空，跳过发送")
        return None

    log.info("开始构造合并转发 -> 群 %d, 共 %d 名客户", group_id, len(customer_list))
    outer_nodes: list[Message] = [
        NodeInline(
            nickname="快捷传送门",
            user_id=str(client.self_id),
            content=serialize_message_segments([Text(text="https://lion-qq.laysath.cn")]),
        ),
    ]

    for qq, _ in customer_list:
        # 获取最近指定时间内的消息ID
        msg_ids = await get_recent_message_ids(qq, count=200, max_age_seconds=RECENT_MESSAGE_MAX_AGE)
        if not msg_ids:
            log.warning("客户 %d 无%d秒内消息，跳过该客户节点", qq, RECENT_MESSAGE_MAX_AGE)
            continue

        inner_nodes: list[Message] = [
            NodeReference(id=str(msg_id))
            for msg_id in msg_ids
        ]

        outer_nodes.append(
            NodeInline(
                nickname="客户",
                user_id=str(qq),
                content=serialize_message_segments(inner_nodes),
            )
        )

    if len(outer_nodes) <= 1:
        log.warning("所有客户均无%d秒内消息，取消发送合并转发", RECENT_MESSAGE_MAX_AGE)
        return None

    try:
        response = await client.send_group_forward_msg(
            group_id=str(group_id),
            messages=outer_nodes,
        )
        message_id = response["message_id"]
        log.info("合并转发发送成功 -> 群 %d, message_id=%s", group_id, message_id)
        asyncio.create_task(add_emoji_to_message(message_id, list(EMOJI_MAPPING.values())))
        return message_id
    except Exception as e:
        log.error("发送合并转发失败: %s", e, exc_info=True)
        return None


# ================= 获取当前可用成员 =================
def get_current_available_members() -> list[int]:
    """根据当前系统时间，返回所有可用的成员QQ列表"""
    now = datetime.now()
    weekday_name = WEEKDAY_MAP[now.weekday()]
    current_time = now.time()

    available: list[int] = []
    for qq, schedule in AVAILABILITY.items():
        time_slots = schedule.get(weekday_name, [])
        for start_str, end_str in time_slots:
            try:
                start = datetime.strptime(start_str, "%H:%M").time()
                end = datetime.strptime(end_str, "%H:%M").time()
                if start <= current_time <= end:
                    available.append(qq)
                    break
            except Exception as e:
                log.error("解析时间段失败: qq=%s, slot=%s-%s, err=%s", qq, start_str, end_str, e)
                continue
    return available


# ================= 发送带 @ 的提醒 =================
async def send_reminder_with_at(group_id: int, summary: str, customer_list: list[tuple[int, CustomerData]]) -> int | None:
    """
    发送提醒：先尝试 @ 一位当前可用成员，再发送合并转发。
    若无可用成员，则直接发送合并转发（无 @）。
    """
    available = get_current_available_members()
    if available:
        chosen = random.choice(available)
        # 构造 @ 消息，使用库提供的消息段类
        at_msg: list[Message] = [
            At(qq=str(chosen)),
            Text(text=f" {summary}"),
        ]
        try:
            await client.send_group_msg(group_id=str(group_id), message=at_msg)
            log.info("已发送 @%d 提醒: %s", chosen, summary)
        except Exception as e:
            log.error("发送 @ 提醒失败: %s", e, exc_info=True)
    else:
        log.debug("当前无可用成员，直接发送合并转发")

    # 发送合并转发
    message_id = await send_nested_forward(group_id, customer_list, summary)
    if message_id is not None:
        track_forward_message(
            message_id=message_id,
            customer_ids=[qq for qq, _ in customer_list],
            group_id=group_id,
        )
    return message_id


# ================= 夜间模式辅助函数 =================
def is_night_time() -> bool:
    """判断当前时间是否处于夜间免打扰时段"""
    now = datetime.now().time()
    start = datetime.strptime(NIGHT_START, "%H:%M").time()
    end = datetime.strptime(NIGHT_END, "%H:%M").time()

    if start <= end:
        return start <= now <= end
    else:
        return now >= start or now <= end


# ================= 核心逻辑：定时巡检任务 =================
async def monitor_loop():
    global last_night_summary_sent_date
    log.info("巡检任务已启动，每 60 秒执行一次")
    while True:
        await asyncio.sleep(60)
        
        if not client.is_running:
            log.debug("巡检跳过: 客户端未运行")
            continue

        # ===== 清理超时的监听消息 =====
        now = time.time()
        to_remove = [mid for mid, data in monitored_forwards.items() if now - data["created_at"] > MAX_LISTEN_AGE]
        for mid in to_remove:
            pop_tracked_forward(mid)
        if to_remove:
            log.debug("已清理 %d 条过期监听消息", len(to_remove))

        if not unreplied_customers:
            log.debug("巡检跳过: 当前无未回复客户")
        else:
            log.info("===== 巡检开始 =====\n当前未回复客户数: %d", len(unreplied_customers))

        # 清理过期好友申请缓存
        expired_flags = [flag for flag, ts in processed_friend_requests.items() if now - ts > PROCESSED_FRIEND_REQUESTS_EXPIRE]
        for flag in expired_flags:
            del processed_friend_requests[flag]

        # 清理过期的好友通过时间记录
        expired_approves = [uid for uid, ts in friend_approve_time.items() if now - ts > PROCESSED_FRIEND_REQUESTS_EXPIRE]
        for uid in expired_approves:
            del friend_approve_time[uid]

        # 内部函数：处理通报（立即或延后）
        def handle_notification(notify_type: str, customers: list[tuple[int, CustomerData]], milestone: int | None = None) -> None:
            if is_night_time():
                notif: DelayedNotification = {
                    "type": notify_type,
                    "customers": customers,
                    "milestone": milestone,
                    "timestamp": now,
                }
                delayed_notifications.append(notif)
                log.info(f"夜间模式：{notify_type} 通知已延后，涉及 {len(customers)} 名客户")
            else:
                if notify_type == "new_customers":
                    summary = f"📢 刚刚有 {len(customers)} 名客户发来消息，请及时回复！"
                    asyncio.create_task(send_reminder_with_at(INTERNAL_GROUP_ID, summary, customers))
                elif notify_type == "milestone":
                    if milestone is None:
                        log.error("里程碑通知缺少 milestone 参数，跳过")
                        return
                    unit = f"{milestone}分钟" if milestone < 60 else f"{milestone // 60}小时"
                    summary = f"⚠️ 以下 {len(customers)} 名客户已等待长达 {unit}！"
                    asyncio.create_task(send_reminder_with_at(INTERNAL_GROUP_ID, summary, customers))

        if unreplied_customers:
            # ===== 阶段 1：新客户防抖通报 =====
            new_customers_to_report: list[tuple[int, CustomerData]] = []
            for qq, data in unreplied_customers.items():
                elapsed_min = (now - data["last_active"]) / 60.0
                if not data["is_newly_reported"] and elapsed_min >= 1.0:
                    new_customers_to_report.append((qq, data))
                    data["is_newly_reported"] = True

            if new_customers_to_report:
                new_customers_to_report.sort(key=lambda x: x[1]["last_active"], reverse=True)
                handle_notification("new_customers", new_customers_to_report)
            else:
                log.debug("阶段1: 无新客户需要通报")

            # ===== 阶段 2：迟滞里程碑通报 =====
            milestone_groups: dict[int, list[tuple[int, CustomerData]]] = {m: [] for m in MILESTONES}
            for qq, data in unreplied_customers.items():
                elapsed_min = (now - data["last_active"]) / 60.0
                for m in sorted(MILESTONES, reverse=True):
                    if elapsed_min >= m:
                        if m not in data["reported_milestones"]:
                            data["reported_milestones"].add(m)
                            milestone_groups[m].append((qq, data))
                        break

            for m, delay_list in milestone_groups.items():
                if delay_list:
                    delay_list.sort(key=lambda x: x[1]["last_active"], reverse=True)
                    handle_notification("milestone", delay_list, milestone=m)

            log.info("===== 巡检结束 =====")

        # ===== 夜间汇总发送检查 =====
        summary_time = datetime.strptime(NIGHT_SUMMARY_TIME, "%H:%M").time()
        now_time = datetime.now().time()
        today_str = datetime.now().strftime("%Y%m%d")

        summary_dt = datetime.combine(datetime.today(), summary_time)
        lower_bound = (summary_dt - timedelta(minutes=5)).time()
        upper_bound = (summary_dt + timedelta(minutes=5)).time()

        in_summary_window = False
        if lower_bound <= upper_bound:
            in_summary_window = lower_bound <= now_time <= upper_bound
        else:
            in_summary_window = now_time >= lower_bound or now_time <= upper_bound

        if in_summary_window and last_night_summary_sent_date != today_str:
            if delayed_notifications:
                total_customers: set[int] = set()
                for notif in delayed_notifications:
                    for qq, _ in notif["customers"]:
                        total_customers.add(qq)

                summary_text = f"🌙 夜间免打扰时段汇总：共有 {len(total_customers)} 名客户发来消息，请及时处理。"
                try:
                    await client.send_group_msg(group_id=str(INTERNAL_GROUP_ID), message=summary_text)
                    log.info(f"夜间汇总已发送：{summary_text}")
                except Exception as e:
                    log.error(f"发送夜间汇总失败: {e}")

                delayed_notifications.clear()
            else:
                log.debug("夜间汇总时间到，但无延后通知")

            last_night_summary_sent_date = today_str

        # 定期保存状态（每轮巡检后）
        save_state()


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

def extract_message_text(segments: Iterable[Union[Message, UnknownMessageSegment]]) -> str:
    """
    从消息段可迭代对象中提取纯文本，用于日志记录。
    只提取 Text 段中的文本，忽略其他类型。
    """
    texts: list[str] = []
    for seg in segments:
        if isinstance(seg, Text):
            texts.append(seg.text)
    full = ''.join(texts).strip()
    if len(full) > 100:
        full = full[:100] + '…'
    return full or '<无文本内容>'

async def add_emoji_to_message(message_id: int, emoji_ids: list[int]) -> None:
    """为指定消息添加多个表情贴纸"""
    for emoji_id in emoji_ids:
        try:
            await client.set_msg_emoji_like(message_id=str(message_id), emoji_id=str(emoji_id))
            log.debug("已为消息 %s 添加表情 %s", message_id, emoji_id)
        except Exception as e:
            log.error("添加表情失败: message_id=%s, emoji_id=%s, err=%s", message_id, emoji_id, e)


# ================= 命令处理复用函数 =================
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


async def send_and_track_feedback(gid: int, reply_id: int, feedback: str, customer_id: int) -> None:
    """发送带引用的反馈消息，并将其加入监听列表"""
    try:
        resp = await client.send_group_msg(
            group_id=str(gid),
            message=[Reply(id=str(reply_id)), Text(text=feedback)],
        )
        feedback_msg_id = resp.get("message_id")
        if feedback_msg_id:
            track_forward_message(feedback_msg_id, [customer_id], gid)
    except Exception as e:
        log.error("发送反馈消息失败: %s", e)


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
            match event:
                # 0. 自动通过好友申请
                case FriendRequestEvent(user_id=uid, comment=comment, flag=flag):
                    log.info("收到好友申请：" + str(uid) + " " + comment)
                    now = time.time()
                    last_time = processed_friend_requests.get(flag)
                    if last_time and (now - last_time) < PROCESSED_FRIEND_REQUESTS_EXPIRE:
                        log.info("好友申请重复事件已忽略: flag=%s, user_id=%s", flag, uid)
                        continue
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

                # 1. 侦听内部群表情操作（点击预设表情执行对应命令）
                case GroupMsgEmojiLikeEvent(group_id=gid, message_id=mid, user_id=uid) if gid == INTERNAL_GROUP_ID and uid != client.self_id:
                    eid = getattr(event, 'emoji_id', None)
                    is_add = getattr(event, 'is_add', True)
                    if eid is None or not is_add:
                        continue

                    tracked_data = monitored_forwards.get(mid)
                    if tracked_data is None:
                        continue

                    customer_ids = tracked_data["customer_ids"]
                    if len(customer_ids) != 1:
                        try:
                            await client.send_group_msg(
                                group_id=str(gid),
                                message=[Reply(id=str(mid)), Text(text="暂不支持：该合并转发包含多个客户，请手动处理。")],
                            )
                        except Exception as e:
                            log.error("多客户反馈失败: %s", e)
                        continue

                    customer_id = customer_ids[0]
                    cmd = EMOJI_TO_CMD.get(eid)
                    if cmd is None:
                        continue

                    log.info("表情触发命令: cmd=%s, user=%d, customer=%d", cmd, uid, customer_id)

                    if cmd == "close":
                        feedback = await handle_close_command(gid, mid, customer_id)
                        asyncio.create_task(send_and_track_feedback(gid, mid, feedback, customer_id))
                    elif cmd == "bye":
                        feedback = await handle_bye_command(gid, mid, customer_id)
                        asyncio.create_task(send_and_track_feedback(gid, mid, feedback, customer_id))
                    elif cmd == "more":
                        success, feedback, new_fwd_id = await handle_more_command(gid, mid, customer_id)
                        if success:
                            if new_fwd_id:
                                track_forward_message(new_fwd_id, [customer_id], gid)
                                asyncio.create_task(add_emoji_to_message(new_fwd_id, list(EMOJI_MAPPING.values())))
                            if feedback:
                                asyncio.create_task(send_and_track_feedback(gid, mid, feedback, customer_id))
                        else:
                            if feedback:
                                asyncio.create_task(send_and_track_feedback(gid, mid, feedback, customer_id))

                # 2. 侦听客服的回复（清理字典并记录耗时）
                case PrivateMessageEvent(post_type="message_sent", target_id=tid) if tid in unreplied_customers:
                    await close_session(tid, send_closing=False)
                    log.info("客服已回复 %s，移除提醒并记录耗时。剩余未回复: %d", tid, len(unreplied_customers))

                # 3. 接收客户消息 (加入/更新字典)
                case PrivateMessageEvent(post_type="message", user_id=uid, message_id=msg_id, message=msg_segments) if int(uid) not in WHITELIST:
                    uid = int(uid)
                    now = time.time()
                    
                    # 检查用户是否刚通过好友申请（窗口期内忽略其消息）
                    if uid in friend_approve_time and (now - friend_approve_time[uid]) < PROCESSED_FRIEND_REQUESTS_EXPIRE:
                        log.info("忽略刚通过好友申请的用户 %d 的消息（窗口期 %d 秒），不加入队列",
                                 uid, PROCESSED_FRIEND_REQUESTS_EXPIRE)
                        continue
                    
                    if uid not in unreplied_customers:
                        # 直接将元组传入函数（因为函数现在接受 Iterable）
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

                # 4. 侦听私聊戳一戳（自动发送结束语，并从队列移除）
                case FriendPokeEvent(sender_id=sid, target_id=tid) if tid == client.self_id and sid not in WHITELIST:
                    closed = await close_session(sid, send_closing=True)
                    if closed:
                        log.info("私聊戳一戳结束会话: user_id=%s", sid)
                    else:
                        log.info("私聊戳一戳自动发送结束语（客户不在队列）: user_id=%s", sid)

                # 5. 内部群戳一戳 -> 发送状态面板
                case GroupPokeEvent(group_id=gid, target_id=tid) if gid == INTERNAL_GROUP_ID and tid == client.self_id:
                    log.info("内部群戳一戳触发状态面板: group=%d", gid)
                    await send_status_panel(gid)

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
                        continue
                    
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
                        continue

                    elif cmd_text.startswith(".list"):
                        # 显式获取当前时间，避免使用外部定义的 now
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
                            continue

                        # 检查是否有未回复客户
                        if not unreplied_customers:
                            await client.send_group_msg(
                                group_id=str(gid),
                                message=[Reply(id=str(msg_id)), Text(text="📭 当前没有待回复的客户。")],
                            )
                            continue

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

                    if reply_id is None or not cmd_parts:
                        continue
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
                    now = time.time()

                    # 根据命令分发
                    
                    if cmd_text.startswith(".say"):
                        content = cmd_text[4:].strip()
                        if not content:
                            await client.send_group_msg(
                                group_id=str(gid),
                                message=[Reply(id=str(msg_id)), Text(text=".say 命令后需要附带要发送的消息内容")],
                            )
                            continue

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
                            continue

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
                            continue

                        feedback = await handle_bye_command(gid, msg_id, customer_id)
                        last_command_time[key] = now
                        asyncio.create_task(send_and_track_feedback(gid, msg_id, feedback, customer_id))

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
                            continue

                        feedback = await handle_close_command(gid, msg_id, customer_id)
                        last_command_time[key] = now
                        asyncio.create_task(send_and_track_feedback(gid, msg_id, feedback, customer_id))

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
                            continue

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
