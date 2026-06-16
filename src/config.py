import time
import logging
import os
import subprocess
from collections import deque
from typing import Any

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
CONFIG_PATH = "config.yaml"
RESTART_REQUIRED_KEYS = {"ws_url", "ws_token", "archive_dir", "state_file"}
GIT_LOG_LIMIT = 8
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _normalize_availability(raw_availability: dict[Any, Any]) -> dict[int, dict[str, list[tuple[str, str]]]]:
    avail: dict[int, dict[str, list[tuple[str, str]]]] = {}
    for qq, days in raw_availability.items():
        avail[int(qq)] = {}
        for day, slots in days.items():
            avail[int(qq)][day] = [tuple(slot) for slot in slots]
    return avail


def load_config(path: str = CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml must contain a top-level mapping")
    # 转换 availability 中的时段列表为元组
    cfg["availability"] = _normalize_availability(cfg["availability"])
    return cfg


def _get_config_mtime(path: str = CONFIG_PATH) -> float | None:
    try:
        return os.path.getmtime(path)
    except FileNotFoundError:
        return None


def _run_git_command(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except Exception:
        return None
    return result.stdout.strip()


def get_current_git_head() -> str | None:
    return _run_git_command(["rev-parse", "HEAD"])


def build_git_update_message(old_head: str | None, new_head: str | None, max_commits: int = GIT_LOG_LIMIT) -> str | None:
    if not old_head or not new_head or old_head == new_head:
        return None

    commit_log = _run_git_command(["--no-pager", "log", "--oneline", "--reverse", f"{old_head}..{new_head}"])
    if not commit_log:
        return f"Git 更新: {old_head[:7]} -> {new_head[:7]}"

    commits = [line.strip() for line in commit_log.splitlines() if line.strip()]
    shown_commits = commits[:max_commits]
    stat = _run_git_command(["diff", "--shortstat", f"{old_head}..{new_head}"])

    lines = [f"Git 更新: {old_head[:7]} -> {new_head[:7]}"]
    if stat:
        lines.append(stat)
    lines.append("提交日志:")
    lines.extend(shown_commits)
    if len(commits) > max_commits:
        lines.append(f"... 另有 {len(commits) - max_commits} 个提交")
    return "\n".join(lines)


config = load_config()

WS_URL = ""
WS_TOKEN = ""
INTERNAL_GROUP_ID = 0
WHITELIST: list[int] = []
MILESTONES: list[int] = []
MONITORED_FORWARD_LIMIT = 0

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


CLOSING_MESSAGE = ""
WELCOME_MESSAGE = ""
DEBOUNCE_SECONDS: dict[str, int] = {}
PROCESSED_FRIEND_REQUESTS_EXPIRE = 0
FRIEND_WELCOME_DELAY = 3
FRIEND_WELCOME_RETRIES = 3
FRIEND_WELCOME_RETRY_INTERVAL = 3
FRIEND_COUNT_LIMIT = 3000
REPLY_DURATION_MAXLEN = 0
AVAILABILITY: dict[int, dict[str, list[tuple[str, str]]]] = {}
MAX_LISTEN_AGE = 86400   # 24小时
EMOJI_MAPPING = {"close": 128, "more": 127, "bye": 100, "say": 123, "cancel": 32}
EMOJI_TO_CMD = {v: k for k, v in EMOJI_MAPPING.items()}

# ================= 夜间模式配置 =================
NIGHT_MODE: dict[str, str] = {}
NIGHT_START = "22:00"
NIGHT_END = "08:00"
NIGHT_SUMMARY_TIME = "08:00"

# ================= 可配置的存档与状态持久化 =================
ARCHIVE_DIR = "archives"
STATE_FILE = "state.json"
RECENT_MESSAGE_MAX_AGE = 86400  # 默认1天


def _apply_config(new_config: dict[str, Any], *, initial: bool) -> list[str]:
    global config
    global WS_URL, WS_TOKEN, INTERNAL_GROUP_ID, WHITELIST, MILESTONES
    global MONITORED_FORWARD_LIMIT, CLOSING_MESSAGE, WELCOME_MESSAGE
    global DEBOUNCE_SECONDS, PROCESSED_FRIEND_REQUESTS_EXPIRE
    global FRIEND_WELCOME_DELAY, FRIEND_WELCOME_RETRIES, FRIEND_WELCOME_RETRY_INTERVAL
    global FRIEND_COUNT_LIMIT, REPLY_DURATION_MAXLEN, AVAILABILITY, MAX_LISTEN_AGE
    global EMOJI_MAPPING, EMOJI_TO_CMD, NIGHT_MODE, NIGHT_START, NIGHT_END
    global NIGHT_SUMMARY_TIME, ARCHIVE_DIR, STATE_FILE, RECENT_MESSAGE_MAX_AGE
    global reply_durations

    applied_config = dict(new_config)
    restart_only_changes: list[str] = []

    if not initial and config:
        for key in RESTART_REQUIRED_KEYS:
            if applied_config.get(key) != config.get(key):
                restart_only_changes.append(key)
                applied_config[key] = config.get(key)

    previous_maxlen = REPLY_DURATION_MAXLEN
    config = applied_config

    WS_URL = config["ws_url"]
    WS_TOKEN = config["ws_token"]
    INTERNAL_GROUP_ID = config["internal_group_id"]
    WHITELIST = list(config["whitelist"])
    MILESTONES = list(config["milestones"])
    MONITORED_FORWARD_LIMIT = config["monitored_forward_limit"]

    CLOSING_MESSAGE = parse_message_config(config["closing_message"])
    WELCOME_MESSAGE = parse_message_config(config["welcome_message"])
    DEBOUNCE_SECONDS = dict(config["debounce_seconds"])
    PROCESSED_FRIEND_REQUESTS_EXPIRE = config["processed_friend_requests_expire"]
    FRIEND_WELCOME_DELAY = config.get("friend_welcome_delay", 3)
    FRIEND_WELCOME_RETRIES = config.get("friend_welcome_retries", 3)
    FRIEND_WELCOME_RETRY_INTERVAL = config.get("friend_welcome_retry_interval", 3)
    FRIEND_COUNT_LIMIT = config.get("friend_count_limit", 3000)
    REPLY_DURATION_MAXLEN = config["reply_duration_maxlen"]
    AVAILABILITY = config["availability"]
    MAX_LISTEN_AGE = config.get("max_listen_age", 86400)   # 24小时
    EMOJI_MAPPING = dict(config.get("emoji_mapping", {"close": 128, "more": 127, "bye": 100, "say": 123, "cancel": 32}))
    EMOJI_TO_CMD = {v: k for k, v in EMOJI_MAPPING.items()}

    # ================= 夜间模式配置 =================
    NIGHT_MODE = dict(config.get("night_mode", {}))
    NIGHT_START = NIGHT_MODE.get("start", "22:00")
    NIGHT_END = NIGHT_MODE.get("end", "08:00")
    NIGHT_SUMMARY_TIME = NIGHT_MODE.get("summary_time", "08:00")

    # ================= 可配置的存档与状态持久化 =================
    ARCHIVE_DIR = config.get("archive_dir", "archives")
    STATE_FILE = config.get("state_file", "state.json")
    RECENT_MESSAGE_MAX_AGE = config.get("recent_message_max_age", 86400)  # 默认1天

    if initial:
        reply_durations = deque(maxlen=REPLY_DURATION_MAXLEN)
    elif REPLY_DURATION_MAXLEN != previous_maxlen:
        reply_durations = deque(reply_durations, maxlen=REPLY_DURATION_MAXLEN)

    return restart_only_changes


def reload_config(path: str = CONFIG_PATH) -> list[str]:
    new_config = load_config(path)
    return _apply_config(new_config, initial=False)


def reload_config_if_changed(path: str = CONFIG_PATH) -> bool:
    global _config_mtime, _failed_config_mtime

    current_mtime = _get_config_mtime(path)
    if current_mtime is None:
        return False
    if _config_mtime is not None and current_mtime <= _config_mtime:
        return False

    try:
        restart_only_changes = reload_config(path)
    except Exception as e:
        if _failed_config_mtime != current_mtime:
            log.error("配置重载失败，继续沿用旧配置: %s", e, exc_info=True)
            _failed_config_mtime = current_mtime
        return False

    _config_mtime = current_mtime
    _failed_config_mtime = None

    if restart_only_changes:
        log.warning("配置文件已重载，但以下配置需重启后生效: %s", ", ".join(sorted(restart_only_changes)))
    else:
        log.info("配置文件已热更新生效")
    return True


reply_durations: deque[float] = deque()

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

# ================= 运行时状态 =================
# 内存字典：存储未回复的客户状态
unreplied_customers: dict[int, CustomerData] = {}
monitored_forward_order: deque[int] = deque()
monitored_forwards: dict[int, ForwardMonitorData] = {}
last_command_time: dict[tuple[int, str], float] = {}
# 等待 .say 内容的用户：{user_id: {prompt_msg_id, customer_id, reply_id, group_id}}
pending_say: dict[int, dict] = {}

# 夜间通知延后缓存
delayed_notifications: list[DelayedNotification] = []
last_night_summary_sent_date: str = ""

_apply_config(load_config(CONFIG_PATH), initial=True)

# 客户端对象
client: NapCatClient = NapCatClient(WS_URL, WS_TOKEN)
_config_mtime = _get_config_mtime(CONFIG_PATH)
_failed_config_mtime: float | None = None
_last_git_head = get_current_git_head()


def pull_updates() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", "pull"],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except Exception as e:
        return False, str(e)

    output = (result.stdout or "").strip()
    error_output = (result.stderr or "").strip()
    text = output if output else error_output
    return result.returncode == 0, text


def apply_config_reload_after_pull() -> tuple[bool, list[str], str | None]:
    global _config_mtime, _failed_config_mtime

    current_mtime = _get_config_mtime(CONFIG_PATH)
    restart_only_changes: list[str] = []

    try:
        restart_only_changes = reload_config(CONFIG_PATH)
    except Exception as e:
        _failed_config_mtime = current_mtime
        return False, [], str(e)

    _config_mtime = current_mtime
    _failed_config_mtime = None
    return True, restart_only_changes, None


async def run_update_cfg() -> tuple[bool, str]:
    old_head = get_current_git_head()
    success, pull_text = pull_updates()
    if not success:
        return False, f"❌ 更新失败：{pull_text}"

    new_head = get_current_git_head()
    git_update_message = build_git_update_message(old_head, new_head)
    reload_ok, restart_only_changes, reload_error = apply_config_reload_after_pull()
    if not reload_ok:
        return False, f"❌ 配置重载失败：{reload_error}"

    lines = ["🔄 配置更新已生效"]
    if git_update_message:
        lines.extend(["", git_update_message])
    if restart_only_changes:
        lines.extend(["", "以下配置需重启后生效：", ", ".join(sorted(restart_only_changes))])
    return True, "\n".join(lines)
