import asyncio
import logging

from src.main import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger("Notify-Bridge-Bot").info("程序已手动停止。")
