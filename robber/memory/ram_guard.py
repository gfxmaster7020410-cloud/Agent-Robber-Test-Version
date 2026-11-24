"""Early RSS shrink trigger at 1.2 GB (hard ceiling stays 1.5 GB)."""



from __future__ import annotations



import inspect

import logging

import threading

from pathlib import Path

from typing import Any



from robber.config import COMBINED_RSS_LIMIT, RAM_GUARD_BYTES, TELEMETRY_INTERVAL_SECONDS

from robber.jail import workspace_root

from robber.ssot_db import default_ssot_path

from robber.telemetry import reclaim_local, rss_bytes



logger = logging.getLogger(__name__)



_GUARD: RamGuard | None = None





class RamGuard:

    """Background inspector: shrink SQLite, Python GC, and Rust indexer caches."""



    def __init__(

        self,

        root: Path | str | None = None,

        *,

        ssot_path: Path | str | None = None,

        threshold: int = RAM_GUARD_BYTES,

        limit: int = COMBINED_RSS_LIMIT,

        interval: float = TELEMETRY_INTERVAL_SECONDS,

        hub: Any = None,

    ) -> None:

        self.root = Path(root).resolve() if root else workspace_root()

        self.ssot_path = Path(ssot_path) if ssot_path else default_ssot_path(self.root)

        self.threshold = max(1, int(threshold))

        self.limit = max(self.threshold, int(limit))

        self.interval = max(0.5, float(interval))

        self.hub = hub

        self._stop = threading.Event()

        self._thread: threading.Thread | None = None

        self.last: dict[str, Any] = {"rss": 0, "reclaims": 0}



    def inspect(self) -> dict[str, Any]:

        used = int(rss_bytes())

        snap = {

            "rss": used,

            "threshold": self.threshold,

            "limit": self.limit,

            "hot": used >= self.threshold,

            "over_limit": used >= self.limit,

            "reclaims": int(self.last.get("reclaims") or 0),

        }

        self.last = {**self.last, **snap}

        return snap



    def maybe_reclaim(self, *, force: bool = False) -> dict[str, Any]:

        info = self.inspect()

        if not force and not info["hot"]:

            return {**info, "reclaimed": False}

        try:

            from robber.memory.context_graph import clear_graph_cache



            clear_graph_cache()

        except Exception:

            logger.debug("graph cache flush skipped")

        local = reclaim_local(self.ssot_path)

        self.last["reclaims"] = int(self.last.get("reclaims") or 0) + 1

        report = {**info, **local, "reclaimed": True, "reclaims": self.last["reclaims"]}

        self._emit(report)

        return report



    def start(self, hub: Any = None) -> None:

        if hub is not None:

            self.hub = hub

        if self._thread is not None:

            return

        self._stop.clear()

        self._thread = threading.Thread(target=self._loop, name="robber-ram-guard", daemon=True)

        self._thread.start()



    def stop(self) -> None:

        self._stop.set()

        thread = self._thread

        self._thread = None

        if thread is not None:

            thread.join(timeout=2.0)



    def _loop(self) -> None:

        while not self._stop.wait(self.interval):

            try:

                self.maybe_reclaim()

            except Exception:

                logger.debug("ram guard tick skipped")



    def _emit(self, report: dict[str, Any]) -> None:

        mb = int(report.get("rss") or 0) / (1024 * 1024)

        text = (

            f"[RAM-GUARD] reclaim rss={mb:.0f}MB gc={report.get('gc')} "

            f"sqlite={report.get('sqlite_shrink')} rust={report.get('rust_caches')}"

        )

        logger.info(text)

        hub = self.hub

        broadcast = getattr(hub, "broadcast", None) if hub is not None else None

        if broadcast is None or inspect.iscoroutinefunction(broadcast):

            return

        payload = {

            "type": "message",

            "event": "STATE_ACK",

            "role": "system",

            "content": text,

            "text": text,

            "stream_kind": "ack",

            "append": False,

        }

        try:

            result = broadcast(payload)

            if inspect.iscoroutine(result):

                result.close()

        except Exception:

            logger.debug("ram guard emit skipped")





def get_ram_guard(root: Path | str | None = None) -> RamGuard:

    global _GUARD

    if _GUARD is None:

        _GUARD = RamGuard(root)

    return _GUARD





def bind_ram_guard(guard: RamGuard | None) -> None:

    global _GUARD

    _GUARD = guard

