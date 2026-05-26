import time
import logging
from collections import deque

import yaml

from napcat import NapCatClient
from napcat.types import Text, Image, Face, At, Poke

from .models import CustomerData, ForwardMonitorData, DelayedNotification

# ================= 日志配置 =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("Notify-Bridge-Bot")

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

# ================= 结构化消息类型映射 =================
SEGMENT_TYPE_MAP = {
    "text": Text,
    "image": Image,
    "face": Face,
    "at": At,
    "poke": Poke,
}


def parse_message_config(raw):
    """将配置中的消息值转换为 napcat SDK 可接受的格式。

    支持两种格式：
    - 字符串：直接返回字符串（去除末尾多余换行）
    - 列表：每个元素为 {"type": "text", "data": {...}}，返回 napcat 消息段对象列表
    """
    if isinstance(raw, str):
        return raw.rstrip("\n")
    if isinstance(raw, list):
        segments = []
        for item in raw:
            seg_type = item["type"]
            seg_data = item.get("data", {})
            cls = SEGMENT_TYPE_MAP[seg_type.lower()]
            segments.append(cls(**seg_data))
        return segments
    return raw


CLOSING_MESSAGE = parse_message_config(config["closing_message"])
WELCOME_MESSAGE = parse_message_config(config["welcome_message"])
DEBOUNCE_SECONDS = config["debounce_seconds"]
PROCESSED_FRIEND_REQUESTS_EXPIRE = config["processed_friend_requests_expire"]
FRIEND_WELCOME_DELAY = config.get("friend_welcome_delay", 3)
FRIEND_WELCOME_RETRIES = config.get("friend_welcome_retries", 3)
FRIEND_WELCOME_RETRY_INTERVAL = config.get("friend_welcome_retry_interval", 3)
FRIEND_COUNT_LIMIT = config.get("friend_count_limit", 3000)
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

# 好友数量缓存（启动时初始化，通过事件增量更新）
friend_count: int = 0

# ================= 回复耗时记录（秒） =================
reply_durations: deque[float] = deque(maxlen=REPLY_DURATION_MAXLEN)

# ================= 运行时状态 =================
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
