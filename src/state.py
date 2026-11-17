import json
import os
import tempfile
import time
from typing import cast

from . import config as cfg
from .config import (
    log,
    unreplied_customers,
    monitored_forward_order,
    last_command_time,
    delayed_notifications,
)
from .models import (
    CustomerData,
    StateCustomerData,
    StateForwardData,
    StateDelayedNotification,
    AppState,
)


def _serialize_customer(data: CustomerData) -> StateCustomerData:
    return {
        "last_active": data["last_active"],
        "msg_ids": data["msg_ids"],
        "is_newly_reported": data["is_newly_reported"],
        "reported_milestones": list(data["reported_milestones"]),
        "pending_since": data["pending_since"],
        "session_id": data.get("session_id"),
    }


def _restore_customer(data: StateCustomerData) -> CustomerData:
    return {
        "last_active": data["last_active"],
        "msg_ids": data["msg_ids"],
        "is_newly_reported": data["is_newly_reported"],
        "reported_milestones": set(data["reported_milestones"]),
        "pending_since": data["pending_since"],
        "session_id": data.get("session_id"),
    }


def save_state() -> None:
    """通过同目录临时文件原子替换状态，写入失败时保留上一次完整快照。"""
    # 转换 set 为 list 以便 JSON 序列化
    serializable_customers = {
        str(qq): _serialize_customer(data) for qq, data in unreplied_customers.items()
    }

    # 转换 monitored_forwards 的键为字符串
    serializable_forwards: dict[str, StateForwardData] = {
        str(mid): data for mid, data in cfg.monitored_forwards.items()
    }

    # 转换 last_command_time 的键为字符串（兼容 2 元组；非 2 元组跳过，不中断落盘）
    # 编码为 f"{msg_id}_{cmd}"；读回时用 rsplit("_", 1)，可正确还原 "__all___say" 等键
    serializable_last_cmd: dict[str, float] = {}
    for key, ts in last_command_time.items():
        if not isinstance(key, tuple) or len(key) != 2:
            continue
        msg_id, cmd = key
        serializable_last_cmd[f"{msg_id}_{cmd}"] = ts

    # 转换 delayed_notifications 中的 CustomerData 为 StateCustomerData
    serializable_delayed: list[StateDelayedNotification] = []
    for notif in delayed_notifications:
        serializable_delayed.append({
            "type": notif["type"],
            "customers": [(qq, _serialize_customer(cust)) for qq, cust in notif["customers"]],
            "milestone": notif["milestone"],
            "timestamp": notif["timestamp"],
        })

    state: AppState = {
        "unreplied_customers": serializable_customers,
        "monitored_forwards": serializable_forwards,
        "monitored_forward_order": list(monitored_forward_order),
        "last_command_time": serializable_last_cmd,
        "delayed_notifications": serializable_delayed,
        "last_night_summary_sent_date": cfg.last_night_summary_sent_date,
        "mute_until": cfg.mute_until,
    }

    temp_path: str | None = None
    try:
        path = os.path.abspath(cfg.STATE_FILE)
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory,
            prefix=f".{os.path.basename(path)}.", suffix=".tmp", delete=False,
        ) as f:
            temp_path = f.name
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
        temp_path = None
        log.debug("状态已保存至 %s", cfg.STATE_FILE)
    except Exception as e:
        log.error("状态保存失败: %s", e, exc_info=True)
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError as e:
                log.warning("清理状态临时文件失败: %s", e)


def load_state() -> None:
    """从本地文件恢复状态"""
    if not os.path.exists(cfg.STATE_FILE):
        log.info("未找到状态文件 %s，将使用全新状态启动", cfg.STATE_FILE)
        return

    try:
        with open(cfg.STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # 使用 TypedDict 进行类型断言
        state = cast(AppState, raw)
    except Exception as e:
        log.error("状态文件读取失败: %s", e, exc_info=True)
        return

    # 恢复 unreplied_customers（旧版状态文件无 session_id 字段，恢复后由 history.recover_sessions 补建）
    for qq_str, cust_state in state.get("unreplied_customers", {}).items():
        qq = int(qq_str)
        unreplied_customers[qq] = _restore_customer(cust_state)

    # 恢复 monitored_forwards
    cfg.monitored_forwards.clear()
    for mid_str, fwd_state in state.get("monitored_forwards", {}).items():
        mid = int(mid_str)
        cfg.monitored_forwards[mid] = {
            "customer_ids": fwd_state["customer_ids"],
            "group_id": fwd_state["group_id"],
            "created_at": fwd_state["created_at"],
        }
    monitored_forward_order.clear()
    monitored_forward_order.extend(state.get("monitored_forward_order", []))

    # 恢复 last_command_time
    # 用 rsplit 保留 cmd 中可能的下划线，并兼容 __all__ 群组防抖键（如 "__all___say"）
    last_command_time.clear()
    for key_str, ts in state.get("last_command_time", {}).items():
        parts = key_str.rsplit("_", 1)
        if len(parts) != 2:
            continue
        mid_raw, cmd = parts
        if mid_raw == "__all__":
            last_command_time[("__all__", cmd)] = ts
            continue
        try:
            last_command_time[(int(mid_raw), cmd)] = ts
        except ValueError:
            continue

    # 恢复 delayed_notifications
    delayed_notifications.clear()
    for notif_state in state.get("delayed_notifications", []):
        delayed_notifications.append({
            "type": notif_state["type"],
            "customers": [(qq, _restore_customer(cust)) for qq, cust in notif_state["customers"]],
            "milestone": notif_state["milestone"],
            "timestamp": notif_state["timestamp"],
        })

    # 使用 global 声明以修改模块级变量
    cfg.last_night_summary_sent_date = state.get("last_night_summary_sent_date", "")
    cfg.mute_until = float(state.get("mute_until", 0) or 0)

    mute_desc = "未静音"
    if cfg.mute_until != 0:
        if cfg.mute_until < 0:
            mute_desc = "不限时静音"
        elif time.time() < cfg.mute_until:
            mute_desc = f"静音至 {time.strftime('%H:%M:%S', time.localtime(cfg.mute_until))}"
        else:
            mute_desc = "静音已过期（待汇总）"

    log.info("状态恢复完成：待回复客户 %d 人，监听转发 %d 条，%s",
             len(unreplied_customers), len(cfg.monitored_forwards), mute_desc)
