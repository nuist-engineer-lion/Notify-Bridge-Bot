"""应用日志、NoneBot 日志和交互控制台共用的输出设置。"""

import logging
import re

from nonebot.log import default_filter, logger as nonebot_logger, logger_id

project_logger = logging.getLogger("Notify-Bridge-Bot")
console_handler = logging.StreamHandler()
_nonebot_sink: int | None = None


def get_logger(module: str) -> logging.Logger:
    return project_logger.getChild(module.removeprefix("src."))


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # 适配器可能在连接日志和异常末尾输出含鉴权参数的完整 WS 地址。
        return re.sub(r"\bwss?://[^\s<>]+", "[OneBot WS]", super().format(record), flags=re.I)


def _forward_nonebot_log(message) -> None:
    record = message.record
    exception = record["exception"]
    get_logger("nonebot").log(
        record["level"].no, record["message"],
        exc_info=(exception.type, exception.value, exception.traceback) if exception else None,
    )


def configure_logging() -> None:
    global _nonebot_sink
    if _nonebot_sink is not None:
        return
    console_handler.setFormatter(ConsoleFormatter(
        "%(asctime)s [%(levelname)s] %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.basicConfig(level=logging.INFO, handlers=[console_handler])
    nonebot_logger.remove(logger_id)
    _nonebot_sink = nonebot_logger.add(
        _forward_nonebot_log, level=0, filter=default_filter, format="{message}", backtrace=False, diagnose=False,
    )
