import json
import os
from typing import cast

from .config import (
    log,
    STATE_FILE,
    unreplied_customers,
    monitored_forwards,
    monitored_forward_order,
    last_command_time,
    delayed_notifications,
    last_night_summary_sent_date,
)
from .models import (
    CustomerData,
    StateCustomerData,
    StateForwardData,
    StateDelayedNotification,
    AppState,
)


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
            "session_id": data.get("session_id"),
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
                "session_id": cust.get("session_id"),
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
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
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

    # 恢复 unreplied_customers（旧版状态文件无 session_id 字段，恢复后由 history.recover_sessions 补建）
    for qq_str, cust_state in state.get("unreplied_customers", {}).items():
        qq = int(qq_str)
        unreplied_customers[qq] = {
            "last_active": cust_state["last_active"],
            "msg_ids": cust_state["msg_ids"],
            "is_newly_reported": cust_state["is_newly_reported"],
            "reported_milestones": set(cust_state["reported_milestones"]),
            "pending_since": cust_state["pending_since"],
            "session_id": cust_state.get("session_id"),
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
                "session_id": cust_state.get("session_id"),
            }
            customers.append((qq, cust))
        delayed_notifications.append({
            "type": notif_state["type"],
            "customers": customers,
            "milestone": notif_state["milestone"],
            "timestamp": notif_state["timestamp"],
        })

    # 使用 global 声明以修改模块级变量
    import src.config as cfg
    cfg.last_night_summary_sent_date = state.get("last_night_summary_sent_date", "")

    log.info("状态恢复完成：待回复客户 %d 人，监听转发 %d 条",
             len(unreplied_customers), len(monitored_forwards))
