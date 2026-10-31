"""活跃客户资料：按 QQ 后台查询，发送消息只读取本地缓存。"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass

from . import config as cfg


@dataclass
class _Profile:
    nickname: str = ""
    nickname_at: float = float("-inf")
    # None 表示尚未取得备注，空字符串表示已查询且未设置备注。
    remark: str | None = None
    refresh_after: float = 0.0


class CustomerProfileCache:
    def __init__(
        self, *, ttl: float = 900, timeout: float = 3, retry_delay: float = 60,
        concurrency: int = 4, max_entries: int = 2048,
    ) -> None:
        self._ttl = ttl
        self._timeout = timeout
        self._retry_delay = retry_delay
        self._max_entries = max_entries
        self._entries: OrderedDict[int, _Profile] = OrderedDict()
        self._tasks: dict[int, asyncio.Task] = {}
        self._slots = asyncio.Semaphore(concurrency)
        self._closed = False

    def _entry(self, user_id: int) -> _Profile:
        if user_id not in self._entries:
            self._entries[user_id] = _Profile()
        self._entries.move_to_end(user_id)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return self._entries[user_id]

    @staticmethod
    def _remember_nickname(entry: _Profile, nickname, observed_at: float) -> None:
        if isinstance(nickname, str) and nickname and observed_at >= entry.nickname_at:
            entry.nickname = nickname
            entry.nickname_at = observed_at

    def observe(self, user_id: int, nickname: str = "", *, observed_at: float | None = None) -> None:
        """记录私聊自带的昵称并预取资料；不等待网络。"""
        entry = self._entry(user_id)
        self._remember_nickname(entry, nickname, time.time() if observed_at is None else observed_at)
        self._schedule(user_id, entry)

    def prefetch(self, user_ids: Iterable[int]) -> None:
        for user_id in user_ids:
            self._schedule(user_id, self._entry(user_id))

    def get_profiles(
        self, user_ids: Iterable[int], *, messages: Iterable[dict] = (),
    ) -> dict[int, tuple[str, str]]:
        """立即返回资料快照，缺失或过期资料在后台补齐。"""
        entries = {user_id: self._entry(user_id) for user_id in user_ids}
        for message in messages:
            sender = message.get("sender")
            if not isinstance(sender, dict):
                continue
            try:
                entry = entries.get(int(sender["user_id"]))
                observed_at = float(message.get("time", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if entry is not None:
                self._remember_nickname(entry, sender.get("nickname"), observed_at)
        profiles = {}
        for user_id, entry in entries.items():
            self._schedule(user_id, entry)
            profiles[user_id] = (
                entry.nickname or str(user_id),
                "（未获取到）" if entry.remark is None else entry.remark or "（无）",
            )
        return profiles

    def _schedule(self, user_id: int, entry: _Profile) -> None:
        if (
            self._closed or user_id in self._tasks
            or time.monotonic() < entry.refresh_after
            or len(self._tasks) >= self._max_entries
        ):
            return
        self._tasks[user_id] = asyncio.create_task(
            self._refresh(user_id, entry), name=f"customer-profile-{user_id}",
        )

    async def _refresh(self, user_id: int, entry: _Profile) -> None:
        try:
            async with self._slots:
                requested_at = time.time()
                # 同时限制适配器和整个调用的耗时，不依赖协议端自行处理超时。
                async with asyncio.timeout(self._timeout):
                    info = await cfg.client.call_api(
                        "get_stranger_info", user_id=str(user_id), _timeout=self._timeout,
                    )
                if (
                    not isinstance(info, dict)
                    or not isinstance(info.get("nickname"), str)
                    or not info["nickname"]
                ):
                    raise ValueError("用户资料未返回有效昵称")
                self._remember_nickname(entry, info["nickname"], requested_at)
                # 不支持 remark 字段的协议端仍显示“未获取到”，不误报“无备注”。
                if isinstance(info.get("remark"), str):
                    entry.remark = info["remark"]
                entry.refresh_after = time.monotonic() + self._ttl
        except Exception as e:
            entry.refresh_after = time.monotonic() + self._retry_delay
            cfg.log.warning(
                "后台获取客户 %d 资料失败，%.0f 秒后可重试，继续使用缓存: %s",
                user_id, self._retry_delay, e,
            )
        finally:
            self._tasks.pop(user_id, None)

    async def close(self) -> None:
        """关停时释放正在查询或排队的任务。"""
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._entries.clear()


cache = CustomerProfileCache()
