import asyncio
import time
import json
import logging
import math
import os
import subprocess
import shutil
from collections import deque
from typing import Any

import yaml

from napcat.types import Text, Image, Face, At, Poke

from .models import CustomerData, ForwardMonitorData, DelayedNotification, RecallableSendData
from .onebot import OneBotClient

# ================= 日志配置 =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("Notify-Bridge-Bot")

# ================= 加载配置 =================
CONFIG_PATH = "config.yaml"
RELOAD_STATUS_PATH = "archives/reload-status.json"
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
            timeout=15,
        )
    except Exception:
        return None
    return result.stdout.strip()


def _combine_command_output(stdout: str | None, stderr: str | None) -> str:
    parts = []
    if stdout:
        stdout = stdout.strip()
        if stdout:
            parts.append(stdout)
    if stderr:
        stderr = stderr.strip()
        if stderr:
            parts.append(stderr)
    return "\n".join(parts)


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
DEFAULT_EMOJI_MAPPING = {"close": 128, "more": 127, "bye": 100, "say": 123, "cancel": 32, "recall": 89, "say_timeout": 25}
EMOJI_MAPPING = dict(DEFAULT_EMOJI_MAPPING)
EMOJI_TO_CMD = {v: k for k, v in EMOJI_MAPPING.items() if k != "say_timeout"}
RECALL_WINDOW_SECONDS = 60  # .say 发送成功后允许通过表情撤回的时间窗口（秒）
SAY_INPUT_TIMEOUT_SECONDS = 30.0
# 过期撤回记录额外保留时长：窗口结束后仍可识别延迟点击并回复超时提示
RECALL_CLEANUP_GRACE_SECONDS = 300

# ================= 夜间模式配置 =================
NIGHT_MODE: dict[str, str] = {}
NIGHT_START = "22:00"
NIGHT_END = "08:00"
NIGHT_SUMMARY_TIME = "08:00"

# ================= AI 回复建议（OpenAI 兼容接口） =================
# 空字典表示未启用；ai.py 每次调用时读取，可通过 .reload cfg 在线开关/换模型
AI_SUGGESTION: dict[str, Any] = {}

# ================= 通知 PR（通知群消息自动转主页通知） =================
# 空字典表示未启用；notice_pr.py 每次调用时读取，可通过 .reload cfg 在线开关/改群号
NOTICE_PR: dict[str, Any] = {}

# ================= 可配置的存档与状态持久化 =================
ARCHIVE_DIR = "archives"
STATE_FILE = "state.json"
RECENT_MESSAGE_MAX_AGE = 86400  # 默认1天
# 会话库保留期（天），0 = 永久保留；超期会话及其事件由巡检任务定期清理
ARCHIVE_RETENTION_DAYS = 0


def _apply_config(new_config: dict[str, Any], *, initial: bool) -> list[str]:
    global config
    global WS_URL, WS_TOKEN, INTERNAL_GROUP_ID, WHITELIST, MILESTONES
    global MONITORED_FORWARD_LIMIT, CLOSING_MESSAGE, WELCOME_MESSAGE
    global DEBOUNCE_SECONDS, PROCESSED_FRIEND_REQUESTS_EXPIRE
    global FRIEND_WELCOME_DELAY, FRIEND_WELCOME_RETRIES, FRIEND_WELCOME_RETRY_INTERVAL
    global FRIEND_COUNT_LIMIT, REPLY_DURATION_MAXLEN, AVAILABILITY, MAX_LISTEN_AGE
    global EMOJI_MAPPING, EMOJI_TO_CMD, RECALL_WINDOW_SECONDS, NIGHT_MODE, NIGHT_START, NIGHT_END
    global NIGHT_SUMMARY_TIME, ARCHIVE_DIR, STATE_FILE, RECENT_MESSAGE_MAX_AGE
    global ARCHIVE_RETENTION_DAYS
    global AI_SUGGESTION, NOTICE_PR, reply_durations
    global SAY_INPUT_TIMEOUT_SECONDS

    applied_config = dict(new_config)
    say_input_timeout = float(applied_config.get("say_input_timeout_seconds", 30))
    if not math.isfinite(say_input_timeout) or say_input_timeout <= 0:
        raise ValueError("say_input_timeout_seconds 必须为大于 0 的有限秒数")
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
    EMOJI_MAPPING = dict(config.get("emoji_mapping", DEFAULT_EMOJI_MAPPING))
    # 未显式配置撤回表情时回退到默认值，保证旧配置也能使用新功能
    EMOJI_MAPPING.setdefault("recall", DEFAULT_EMOJI_MAPPING["recall"])
    # 超时表情仅标记等待结束；旧配置缺少该项时使用“困”。
    EMOJI_MAPPING.setdefault("say_timeout", DEFAULT_EMOJI_MAPPING["say_timeout"])
    EMOJI_TO_CMD = {v: k for k, v in EMOJI_MAPPING.items() if k != "say_timeout"}
    RECALL_WINDOW_SECONDS = int(config.get("recall_window_seconds", 60))
    SAY_INPUT_TIMEOUT_SECONDS = say_input_timeout

    # ================= 夜间模式配置 =================
    NIGHT_MODE = dict(config.get("night_mode", {}))
    NIGHT_START = NIGHT_MODE.get("start", "22:00")
    NIGHT_END = NIGHT_MODE.get("end", "08:00")
    NIGHT_SUMMARY_TIME = NIGHT_MODE.get("summary_time", "08:00")

    # ================= AI 回复建议 =================
    AI_SUGGESTION = dict(config.get("ai_suggestion", {}) or {})

    # ================= 通知 PR =================
    NOTICE_PR = dict(config.get("notice_pr", {}) or {})

    # ================= 可配置的存档与状态持久化 =================
    ARCHIVE_DIR = config.get("archive_dir", "archives")
    STATE_FILE = config.get("state_file", "state.json")
    RECENT_MESSAGE_MAX_AGE = config.get("recent_message_max_age", 86400)  # 默认1天
    ARCHIVE_RETENTION_DAYS = int(config.get("archive_retention_days", 0))

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
        write_reload_status(ok=False, source="mtime_watch", error=str(e))
        return False

    _config_mtime = current_mtime
    _failed_config_mtime = None
    write_reload_status(ok=True, source="mtime_watch", restart_only_changes=restart_only_changes)

    if restart_only_changes:
        log.warning("配置已重载(mtime)，以下项需重启后生效: %s", ", ".join(sorted(restart_only_changes)))
    else:
        log.info("配置已重载并生效 (source=mtime_watch)")
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

# ================= 运行时状态 =================
# 内存字典：存储未回复的客户状态
unreplied_customers: dict[int, CustomerData] = {}
monitored_forward_order: deque[int] = deque()
monitored_forwards: dict[int, ForwardMonitorData] = {}
# 键：消息 ID / 客户 QQ，或 "__all__"（群组批量操作防抖）
last_command_time: dict[tuple[int | str, str], float] = {}
# 等待 .say 内容的用户：{user_id: {prompt_msg_id, customer_id, reply_id, group_id}}
pending_say: dict[int, dict] = {}

# 可通过表情限时撤回的 .say 发送记录：{feedback_msg_id: RecallableSendData}
recallable_sends: dict[int, RecallableSendData] = {}

# 夜间通知延后缓存
delayed_notifications: list[DelayedNotification] = []
last_night_summary_sent_date: str = ""

# 临时静音截止时间戳（epoch 秒）；0 未静音；-1 不限时静音
mute_until: float = 0.0

_apply_config(load_config(CONFIG_PATH), initial=True)

async def _local_history(user_id: int, count: int) -> list[dict]:
    from .storage import get_recent_messages
    return await get_recent_messages(user_id, count)


client: OneBotClient = OneBotClient(WS_URL, WS_TOKEN, history_provider=_local_history)
_config_mtime = _get_config_mtime(CONFIG_PATH)
_failed_config_mtime: float | None = None


def _reload_status_file() -> str:
    return os.path.join(REPO_ROOT, RELOAD_STATUS_PATH)


def write_reload_status(
    *,
    ok: bool,
    source: str,
    restart_only_changes: list[str] | None = None,
    error: str | None = None,
) -> None:
    """Write a small status file for deploy verification / ops checks."""
    payload = {
        "ok": ok,
        "source": source,
        "timestamp": time.time(),
        "restart_only_changes": list(restart_only_changes or []),
        "error": error,
        "config_mtime": _get_config_mtime(CONFIG_PATH),
    }
    status_path = _reload_status_file()
    os.makedirs(os.path.dirname(status_path), exist_ok=True)
    tmp_path = f"{status_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, status_path)


def force_reload_config(path: str = CONFIG_PATH, *, source: str = "manual") -> tuple[bool, list[str], str | None]:
    """Reload config.yaml from disk without git pull/decrypt."""
    global _config_mtime, _failed_config_mtime

    current_mtime = _get_config_mtime(path)
    if current_mtime is None:
        err = f"config file not found: {path}"
        write_reload_status(ok=False, source=source, error=err)
        return False, [], err

    try:
        restart_only_changes = reload_config(path)
    except Exception as e:
        _failed_config_mtime = current_mtime
        err = str(e)
        log.error("配置重载失败，继续沿用旧配置: %s", e, exc_info=True)
        write_reload_status(ok=False, source=source, error=err)
        return False, [], err

    _config_mtime = current_mtime
    _failed_config_mtime = None
    write_reload_status(ok=True, source=source, restart_only_changes=restart_only_changes)
    if restart_only_changes:
        log.warning(
            "配置已重载(source=%s)，以下项需重启后生效: %s",
            source,
            ", ".join(sorted(restart_only_changes)),
        )
    else:
        log.info("配置已重载并生效 (source=%s)", source)
    return True, restart_only_changes, None


def maybe_reload_config_from_disk() -> bool:
    """Poll config mtime and reload when the plaintext file changes."""
    return reload_config_if_changed()


_reload_lock = asyncio.Lock()


def _command_feedback(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    """Decode timeout output too, and keep command replies within a useful size."""
    def decode(value: str | bytes | None) -> str | None:
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value

    output = _combine_command_output(decode(stdout), decode(stderr))
    limit = 3000
    if len(output) > limit:
        output = output[:limit] + "\n… 输出已截断"
    return output


def _apply_remote_config() -> tuple[bool, str]:
    """Pull and decrypt in a worker thread, returning errors or commit hashes."""
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            return False, "❌ 远程配置更新失败：找不到 PowerShell"
        apply_command = [
            shell, "-NoProfile", "-NonInteractive", "-File",
            os.path.join(REPO_ROOT, "scripts", "apply-encrypted-config.ps1"),
        ]
    else:
        apply_command = ["bash", os.path.join(REPO_ROOT, "scripts", "apply-encrypted-config")]

    env = os.environ.copy()
    # Reload explicitly below; do not wait for the mtime watcher from the script.
    env["WAIT_RELOAD"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    old_head = get_current_git_head()
    for stage, command, timeout in (
        ("git pull", ["git", "pull", "--ff-only"], 120),
        ("apply-encrypted-config", apply_command, 60),
    ):
        try:
            result = subprocess.run(
                command, cwd=REPO_ROOT, env=env, capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            detail = _command_feedback(exc.stdout, exc.stderr)
            message = f"❌ 远程配置更新失败：{stage} 超时（{timeout} 秒），后续步骤已停止"
            if detail:
                message += f"\n{detail}"
            log.error("%s", message)
            return False, message
        except OSError as exc:
            message = f"❌ 远程配置更新失败：无法执行 {stage}，后续步骤已停止\n{exc}"
            log.error("%s", message)
            return False, message
        if result.returncode:
            detail = _command_feedback(result.stdout, result.stderr) or "命令未输出错误详情"
            message = f"❌ 远程配置更新失败：{stage} 退出码 {result.returncode}，后续步骤已停止\n{detail}"
            log.error("%s", message)
            return False, message
        log.info("远程配置更新：%s 完成", stage)

    new_head = get_current_git_head()
    if old_head and new_head:
        if old_head == new_head:
            return True, f"Git: {new_head[:7]}（未变化）"
        return True, f"Git: {old_head[:7]} → {new_head[:7]}"
    return True, f"Git: {(old_head or '未知')[:7]} → {(new_head or '未知')[:7]}"


async def run_reload_cfg(*, remote: bool = False) -> tuple[bool, str]:
    """群/控制台共用：可先拉取仓库并解密，再重载配置。"""
    if _reload_lock.locked():
        return False, "❌ 配置更新正在执行，请稍后重试"
    async with _reload_lock:
        git_message = ""
        if remote:
            ok, git_message = await asyncio.to_thread(_apply_remote_config)
            if not ok:
                return False, git_message
        ok, message = _reload_cfg_result(remote=remote)
        if git_message:
            message += f"\n{git_message}"
        return ok, message


def _reload_cfg_result(*, remote: bool) -> tuple[bool, str]:
    ok, restart_only_changes, error = force_reload_config(source="group_command")
    if not ok:
        return False, f"❌ 配置重载失败：{error}"

    lines = ["🔄 已拉取远程仓库、解密并重载配置" if remote else "🔄 配置已重载"]
    if remote:
        lines.append("代码变更需重启进程后生效")
    if restart_only_changes:
        lines.extend([
            "",
            "以下配置需重启后生效：",
            ", ".join(sorted(restart_only_changes)),
        ])
    return True, "\n".join(lines)
