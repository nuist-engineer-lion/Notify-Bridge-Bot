import asyncio
import logging

from .main import main


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger("Notify-Bridge-Bot").info("程序已手动停止。")
