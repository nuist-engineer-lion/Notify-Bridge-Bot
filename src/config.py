import asyncio
import time
import json
import math
import os
import subprocess
import shutil
import sys
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import yaml

from nonebot.adapters.onebot.v11 import MessageSegment

from .models import CustomerData, ForwardMonitorData, DelayedNotification, RecallableSendData
from .onebot import OneBotClient

from .log_setup import configure_logging, get_logger, project_logger

log = get_logger(__name__)
configure_logging()

# ================= 加载配置 =================
CONFIG_PATH = "config.yaml"
RELOAD_STATUS_PATH = "archives/reload-status.json"
RESTART_REQUIRED_KEYS = {"ws_url", "ws_token", "ws_receive_timeout", "archive_dir", "state_file"}
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
        try:
            cfg = yaml.safe_load(f)
        except yaml.MarkedYAMLError as e:
            mark = e.problem_mark
            location = f"（第 {mark.line + 1} 行，第 {mark.column + 1} 列）" if mark else ""
            raise ValueError(f"配置文件 YAML 格式错误{location}") from None
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


def parse_message_config(raw):
    """将配置中的消息值转换为 OneBot 消息格式。

    支持两种格式：
    - 字符串：直接返回字符串（去除末尾多余换行）
    - 列表：每个元素为 {"type": "text", "data": {...}}，返回消息段对象列表
    """
    if isinstance(raw, str):
        return raw.rstrip("\n")
    if isinstance(raw, list):
        segments = []
        for item in raw:
            segments.append(MessageSegment(item["type"].lower(), dict(item.get("data", {}))))
        return segments
    return raw


CLOSING_MESSAGE = ""
WELCOME_MESSAGE = ""
DEBOUNCE_SECONDS: dict[str, int | float] = {}
PROCESSED_FRIEND_REQUESTS_EXPIRE = 0
FRIEND_NOTIFICATION_ENABLED = True
FRIEND_WELCOME_DELAY = 3
FRIEND_WELCOME_RETRIES = 3
FRIEND_WELCOME_RETRY_INTERVAL = 3
FRIEND_COUNT_LIMIT = 3000
MONITORED_GROUPS: dict[str, int] = {}
MONITORED_GROUP_CASCADES: dict[str, tuple[int, ...]] = {}
MORNING_REPORT_GROUP_IDS: tuple[int, ...] = ()
MORNING_REPORT_CHART_ENABLED = False
REPLY_DURATION_MAXLEN = 0
AVAILABILITY: dict[int, dict[str, list[tuple[str, str]]]] = {}
MAX_LISTEN_AGE = 86400   # 24小时
DEFAULT_EMOJI_MAPPING = {"close": 128, "more": 127, "bye": 100, "say": 123, "say_replied": 428, "cancel": 32, "recall": 89, "say_timeout": 25}
EMOJI_MAPPING = dict(DEFAULT_EMOJI_MAPPING)
EMOJI_TO_CMD = {v: "say" if k == "say_replied" else k for k, v in EMOJI_MAPPING.items() if k != "say_timeout"}
RECALL_WINDOW_SECONDS = 60  # .say 发送成功后允许通过表情撤回的时间窗口（秒）
SAY_INPUT_TIMEOUT_SECONDS = 30.0
BYE_CONFIRM_TIMEOUT_SECONDS = 30.0
DEFAULT_BYE_CONFIRM_EMOJI_POOL = (351, 314, 99, 9, 319, 318, 324, 339, 287, 272, 424, 66)
BYE_CONFIRM_EMOJI_POOL = DEFAULT_BYE_CONFIRM_EMOJI_POOL
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


def _parse_group_ids(value: Any, key: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        type(group_id) is not int or not 0 < group_id < 2**63 for group_id in value
    ):
        raise ValueError(f"{key} 必须为正整数群号列表")
    return tuple(dict.fromkeys(value))


def _apply_config(new_config: dict[str, Any], *, initial: bool) -> list[str]:
    """完整构造候选配置后统一生效，任何解析失败都保留当前运行状态。"""
    applied_config = dict(new_config)
    level = applied_config.get("log_level", "INFO")
    if not isinstance(level, str) or level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("log_level 必须为 DEBUG、INFO、WARNING、ERROR 或 CRITICAL")
    friend_notification_enabled = applied_config.get("friend_notification_enabled", True)
    if not isinstance(friend_notification_enabled, bool):
        raise ValueError("friend_notification_enabled 必须为布尔值 true 或 false")
    morning_report_chart_enabled = applied_config.get("morning_report_chart_enabled", False)
    if not isinstance(morning_report_chart_enabled, bool):
        raise ValueError("morning_report_chart_enabled 必须为布尔值 true 或 false")
    group_config = applied_config.get("monitored_group_ids", {})
    if isinstance(group_config, list):
        group_config = {str(group_id): group_id for group_id in _parse_group_ids(group_config, "monitored_group_ids")}
    if not isinstance(group_config, dict):
        raise ValueError("monitored_group_ids 必须为名称到群号或群号列表的映射")
    monitored_groups: dict[str, int] = {}
    monitored_group_cascades: dict[str, tuple[int, ...]] = {}
    for name, value in group_config.items():
        if not isinstance(name, str) or not name.strip() or not name.isprintable() or name == "好友":
            raise ValueError("monitored_group_ids 必须使用有效且独立的显示名称")
        if type(value) is int and 0 < value < 2**63:
            monitored_groups[name] = value
        else:
            group_ids = _parse_group_ids(value, f"monitored_group_ids.{name}")
            if len(group_ids) < 2:
                raise ValueError(f"合计 {name} 至少需要两个不同的群")
            monitored_group_cascades[name] = tuple(sorted(group_ids))
    cascades = applied_config.get("monitored_group_cascades", {})
    if not isinstance(cascades, dict):
        raise ValueError("monitored_group_cascades 必须为级联名称到群名列表的映射")
    for name, sources in cascades.items():
        if (
            not isinstance(name, str) or not name.strip() or not name.isprintable()
            or name == "好友" or name in group_config
            or not isinstance(sources, list)
            or any(not isinstance(source, str) or source not in monitored_groups for source in sources)
        ):
            raise ValueError("monitored_group_cascades 须使用独立的级联名称，并引用已配置的群名")
        group_ids = tuple(sorted({monitored_groups[source] for source in sources}))
        if len(group_ids) < 2:
            raise ValueError(f"级联 {name} 至少需要两个不同的群")
        monitored_group_cascades[name] = group_ids
    morning_report_group_ids = _parse_group_ids(
        applied_config.get("morning_report_group_ids", [applied_config["internal_group_id"]]),
        "morning_report_group_ids",
    )
    say_input_timeout = float(applied_config.get("say_input_timeout_seconds", 30))
    if not math.isfinite(say_input_timeout) or say_input_timeout <= 0:
        raise ValueError("say_input_timeout_seconds 必须为大于 0 的有限秒数")
    bye_confirm_timeout = float(applied_config.get("bye_confirm_timeout_seconds", 30))
    if not math.isfinite(bye_confirm_timeout) or bye_confirm_timeout <= 0:
        raise ValueError("bye_confirm_timeout_seconds 必须为大于 0 的有限秒数")
    bye_confirm_pool = applied_config.get("bye_confirm_emoji_pool", DEFAULT_BYE_CONFIRM_EMOJI_POOL)
    if (
        not isinstance(bye_confirm_pool, (list, tuple))
        or any(type(eid) is not int or eid <= 0 for eid in bye_confirm_pool)
        or len(bye_confirm_pool) < 5
        or len(set(bye_confirm_pool)) != len(bye_confirm_pool)
    ):
        raise ValueError("bye_confirm_emoji_pool 必须包含至少 5 个互不重复的正整数表情 ID")
    bye_confirm_pool = tuple(bye_confirm_pool)
    ws_receive_timeout = float(applied_config.get("ws_receive_timeout", 90))
    if not math.isfinite(ws_receive_timeout) or ws_receive_timeout <= 0:
        raise ValueError("ws_receive_timeout 必须为大于 0 的有限秒数")
    applied_config["ws_receive_timeout"] = ws_receive_timeout
    restart_only_changes: list[str] = []

    if not initial and config:
        for key in RESTART_REQUIRED_KEYS:
            if applied_config.get(key) != config.get(key):
                restart_only_changes.append(key)
                applied_config[key] = config.get(key)

    emoji_mapping = dict(applied_config.get("emoji_mapping", DEFAULT_EMOJI_MAPPING))
    # 未显式配置撤回表情时回退到默认值，保证旧配置也能使用新功能
    emoji_mapping.setdefault("recall", DEFAULT_EMOJI_MAPPING["recall"])
    # 超时表情仅标记等待结束；旧配置缺少该项时使用“困”。
    emoji_mapping.setdefault("say_timeout", DEFAULT_EMOJI_MAPPING["say_timeout"])
    # 已回复消息使用“收到”替换 say，点击后仍进入 say 流程。
    emoji_mapping.setdefault("say_replied", DEFAULT_EMOJI_MAPPING["say_replied"])
    night_mode = dict(applied_config.get("night_mode", {}))
    debounce = applied_config["debounce_seconds"]
    if not isinstance(debounce, dict) or not {"say", "bye", "more"}.issubset(debounce):
        raise ValueError("debounce_seconds 必须包含 say、bye、more 的防抖秒数")
    for value in debounce.values():
        if type(value) not in (int, float) or value < 0 or (isinstance(value, float) and not math.isfinite(value)):
            raise ValueError("debounce_seconds 的值必须为非负有限数值")
    # 此阶段只创建局部值；消息解析、反向表及数值转换均可能失败。
    snapshot = {
        "config": applied_config,
        "WS_URL": applied_config["ws_url"],
        "WS_TOKEN": applied_config["ws_token"],
        "INTERNAL_GROUP_ID": applied_config["internal_group_id"],
        "WHITELIST": list(applied_config["whitelist"]),
        "MILESTONES": list(applied_config["milestones"]),
        "MONITORED_FORWARD_LIMIT": applied_config["monitored_forward_limit"],
        "CLOSING_MESSAGE": parse_message_config(applied_config["closing_message"]),
        "WELCOME_MESSAGE": parse_message_config(applied_config["welcome_message"]),
        "DEBOUNCE_SECONDS": dict(debounce),
        "MONITORED_GROUPS": monitored_groups,
        "MONITORED_GROUP_CASCADES": monitored_group_cascades,
        "MORNING_REPORT_GROUP_IDS": morning_report_group_ids,
        "MORNING_REPORT_CHART_ENABLED": morning_report_chart_enabled,
        "BYE_CONFIRM_TIMEOUT_SECONDS": bye_confirm_timeout,
        "BYE_CONFIRM_EMOJI_POOL": bye_confirm_pool,
        "PROCESSED_FRIEND_REQUESTS_EXPIRE": applied_config["processed_friend_requests_expire"],
        "FRIEND_NOTIFICATION_ENABLED": friend_notification_enabled,
        "FRIEND_WELCOME_DELAY": applied_config.get("friend_welcome_delay", 3),
        "FRIEND_WELCOME_RETRIES": applied_config.get("friend_welcome_retries", 3),
        "FRIEND_WELCOME_RETRY_INTERVAL": applied_config.get("friend_welcome_retry_interval", 3),
        "FRIEND_COUNT_LIMIT": applied_config.get("friend_count_limit", 3000),
        "REPLY_DURATION_MAXLEN": applied_config["reply_duration_maxlen"],
        "AVAILABILITY": applied_config["availability"],
        "MAX_LISTEN_AGE": applied_config.get("max_listen_age", 86400),
        "EMOJI_MAPPING": emoji_mapping,
        "EMOJI_TO_CMD": {
            v: "say" if k == "say_replied" else k
            for k, v in emoji_mapping.items() if k != "say_timeout"
        },
        "RECALL_WINDOW_SECONDS": int(applied_config.get("recall_window_seconds", 60)),
        "SAY_INPUT_TIMEOUT_SECONDS": say_input_timeout,
        "NIGHT_MODE": night_mode,
        "NIGHT_START": night_mode.get("start", "22:00"),
        "NIGHT_END": night_mode.get("end", "08:00"),
        "NIGHT_SUMMARY_TIME": night_mode.get("summary_time", "08:00"),
        "AI_SUGGESTION": dict(applied_config.get("ai_suggestion", {}) or {}),
        "NOTICE_PR": dict(applied_config.get("notice_pr", {}) or {}),
        "ARCHIVE_DIR": applied_config.get("archive_dir", "archives"),
        "STATE_FILE": applied_config.get("state_file", "state.json"),
        "RECENT_MESSAGE_MAX_AGE": applied_config.get("recent_message_max_age", 86400),
        "ARCHIVE_RETENTION_DAYS": int(applied_config.get("archive_retention_days", 0)),
    }
    next_maxlen = snapshot["REPLY_DURATION_MAXLEN"]
    if initial:
        next_durations = deque(maxlen=next_maxlen)
    elif next_maxlen != REPLY_DURATION_MAXLEN:
        next_durations = deque(reply_durations, maxlen=next_maxlen)
    else:
        next_durations = reply_durations
    snapshot["reply_durations"] = next_durations

    # 无 await：验证全部通过后一次性发布，失败路径不需要回滚。
    globals().update(snapshot)
    project_logger.setLevel(level.upper())

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
            log.error("配置重载失败，继续沿用旧配置: source=mtime_watch error=%s", type(e).__name__)
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

# 旧版状态文件中的好友晨报基准，首次写入 SQLite 时使用
friend_summary_baseline: set[int] | None = None
last_friend_summary_date: str = ""

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

client: OneBotClient = OneBotClient(
    WS_URL, WS_TOKEN, ws_receive_timeout=config["ws_receive_timeout"],
)
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
        log.error("配置重载失败，继续沿用旧配置: source=%s error=%s", source, type(e).__name__)
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
    """在线程中拉取、解密并同步依赖；所有步骤成功后才由调用方重载配置。"""
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

    uv = shutil.which("uv")
    if not uv:
        # systemd 的 PATH 可能不含 uv 默认安装目录。
        candidate = os.path.join(os.path.expanduser("~"), ".local", "bin", "uv.exe" if os.name == "nt" else "uv")
        if os.path.isfile(candidate):
            uv = candidate
    if not uv:
        return False, "❌ 远程更新失败：找不到 uv，请安装 uv 或配置服务的 PATH，后续步骤已停止"

    env = os.environ.copy()
    # Reload explicitly below; do not wait for the mtime watcher from the script.
    env["WAIT_RELOAD"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if sys.prefix != sys.base_prefix:
        # .reboot 沿用当前解释器，因此同步实际运行的虚拟环境，而非另一个 .venv。
        env["UV_PROJECT_ENVIRONMENT"] = os.path.abspath(sys.prefix)
        env["VIRTUAL_ENV"] = os.path.abspath(sys.prefix)
    old_head = get_current_git_head()
    for stage, command, timeout in (
        ("git pull", ["git", "pull", "--ff-only"], 120),
        ("apply-encrypted-config", apply_command, 60),
        ("uv sync", [uv, "sync", "--python", sys.executable], 180),
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
            log.error("远程配置更新超时: stage=%s timeout=%ds", stage, timeout)
            return False, message
        except OSError as exc:
            message = f"❌ 远程配置更新失败：无法执行 {stage}，后续步骤已停止\n{exc}"
            log.error("远程配置更新无法执行: stage=%s errno=%s", stage, exc.errno)
            return False, message
        if result.returncode:
            detail = _command_feedback(result.stdout, result.stderr) or "命令未输出错误详情"
            message = f"❌ 远程配置更新失败：{stage} 退出码 {result.returncode}，后续步骤已停止\n{detail}"
            log.error("远程配置更新失败: stage=%s exit_code=%s", stage, result.returncode)
            return False, message
        log.info("远程配置更新：%s 完成", stage)

    new_head = get_current_git_head()
    if old_head and new_head:
        if old_head == new_head:
            return True, f"Git: {new_head[:7]}（未变化）"
        return True, f"Git: {old_head[:7]} → {new_head[:7]}"
    return True, f"Git: {(old_head or '未知')[:7]} → {(new_head or '未知')[:7]}"


async def run_reload_cfg(
    *, remote: bool = False, on_started: Callable[[], Awaitable[None]] | None = None,
) -> tuple[bool, str]:
    """群/控制台共用：远程模式先拉取、解密、同步依赖，再重载配置。"""
    if _reload_lock.locked():
        return False, "❌ 配置更新正在执行，请稍后重试"
    async with _reload_lock:
        git_message = ""
        if remote:
            if on_started is not None:
                await on_started()
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

    lines = ["🔄 已拉取远程仓库、解密、同步依赖并重载配置" if remote else "🔄 配置已重载"]
    if remote:
        lines.append("代码变更需重启进程后生效")
    if restart_only_changes:
        lines.extend([
            "",
            "以下配置需重启后生效：",
            ", ".join(sorted(restart_only_changes)),
        ])
    return True, "\n".join(lines)
