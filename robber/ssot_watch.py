"""Debounced SSOT reindex when jailed workspace files change."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from robber.ssot import get_index

RefreshFn = Callable[[dict[str, object]], Awaitable[None]]

_WATCHER: SsotWatcher | None = None


class SsotWatcher:
    def __init__(self, *, interval: float = 1.25, debounce: float = 0.65) -> None:
        self.interval = interval
        self.debounce = debounce
        self.running = False
        self.events = 0
        self.last_refresh = 0.0
        self._task: asyncio.Task[None] | None = None

    def public_stats(self) -> dict[str, Any]:
        return {
            "watching": self.running,
            "watch_events": self.events,
            "last_watch": self.last_refresh,
        }

    async def start(self, on_refresh: RefreshFn | None = None) -> None:
        await self.stop()
        self.running = True
        self._task = asyncio.create_task(self._loop(on_refresh))

    async def stop(self) -> None:
        self.running = False
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _loop(self, on_refresh: RefreshFn | None) -> None:
        previous: tuple[tuple[str, int, int], ...] | None = None
        try:
            while self.running:
                current = await asyncio.to_thread(get_index().fingerprint)
                if previous is not None and current != previous:
                    await asyncio.sleep(self.debounce)
                    current = await asyncio.to_thread(get_index().fingerprint)
                    if current != previous:
                        stats = await asyncio.to_thread(get_index().reindex)
                        self.events += 1
                        self.last_refresh = time.time()
                        if on_refresh is not None:
                            await on_refresh(stats)
                        previous = await asyncio.to_thread(get_index().fingerprint)
                        continue
                previous = current
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise


def get_ssot_watcher() -> SsotWatcher:
    global _WATCHER
    if _WATCHER is None:
        _WATCHER = SsotWatcher()
    return _WATCHER


def bind_ssot_watcher(watcher: SsotWatcher) -> SsotWatcher:
    global _WATCHER
    _WATCHER = watcher
    return watcher
