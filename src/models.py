from typing import Any, TypedDict


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
    customers: list[tuple[int, "CustomerData"]]
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
    customers: list[tuple[int, "StateCustomerData"]]  # 存储的是 StateCustomerData
    milestone: int | None
    timestamp: float


class AppState(TypedDict):
    unreplied_customers: dict[str, "StateCustomerData"]
    monitored_forwards: dict[str, "StateForwardData"]
    monitored_forward_order: list[int]
    last_command_time: dict[str, float]
    delayed_notifications: list["StateDelayedNotification"]
    last_night_summary_sent_date: str
