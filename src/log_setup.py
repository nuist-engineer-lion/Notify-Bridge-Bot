"""应用日志、NoneBot 日志和交互控制台共用的输出设置。"""

import logging
import re
import sys
from copy import copy

from nonebot.log import default_filter, logger as nonebot_logger, logger_id

project_logger = logging.getLogger("Notify-Bridge-Bot")
console_handler = logging.StreamHandler(sys.stdout)
_nonebot_sink: int | None = None


def get_logger(module: str) -> logging.Logger:
    return project_logger.getChild(module.removeprefix("src."))


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record = copy(record)
        record.name = record.name.removeprefix(project_logger.name + ".")
        # 适配器可能在连接日志和异常末尾输出含鉴权参数的完整 WS 地址。
        return re.sub(r"\bwss?://[^\s<>]+", "[OneBot WS]", super().format(record), flags=re.I)


def _forward_nonebot_log(message) -> None:
    record = message.record
    exception = record["exception"]
    get_logger("nonebot").log(
        record["level"].no, record["message"],
        exc_info=(exception.type, exception.value, exception.traceback) if exception else None,
    )


def _needs_console_copy(record: logging.LogRecord) -> bool:
    """宿主已有日志设置时，给项目补上缺失的控制台输出，保留文件等其他去向。"""
    if record.name != project_logger.name and not record.name.startswith(project_logger.name + "."):
        return False
    for handler in logging.getLogger().handlers:
        if handler is console_handler or not isinstance(handler, logging.StreamHandler):
            continue
        if handler.level > record.levelno or handler.filters:
            continue
        if handler.stream is sys.stdout:
            return False
        if handler.stream is sys.stderr and sys.stdout.isatty() and sys.stderr.isatty():
            return False
    return True


def configure_logging() -> None:
    global _nonebot_sink
    if _nonebot_sink is not None:
        return
    console_handler.setFormatter(ConsoleFormatter(
        "%(asctime)s [%(levelname)s] %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger()
    if root.handlers:
        console_handler.addFilter(_needs_console_copy)
        root.addHandler(console_handler)
    else:
        logging.basicConfig(level=logging.INFO, handlers=[console_handler])
    nonebot_logger.remove(logger_id)
    _nonebot_sink = nonebot_logger.add(
        _forward_nonebot_log, level=0, filter=default_filter, format="{message}", backtrace=False, diagnose=False,
    )
