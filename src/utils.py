from datetime import datetime

from .config import (
    log,
    AVAILABILITY,
    WEEKDAY_MAP,
    NIGHT_START,
    NIGHT_END,
)


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


def is_night_time() -> bool:
    """判断当前时间是否处于夜间免打扰时段"""
    now = datetime.now().time()
    start = datetime.strptime(NIGHT_START, "%H:%M").time()
    end = datetime.strptime(NIGHT_END, "%H:%M").time()

    if start <= end:
        return start <= now <= end
    else:
        return now >= start or now <= end
