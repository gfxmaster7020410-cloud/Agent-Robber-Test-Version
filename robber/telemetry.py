"""8GB-class RSS monitor and reclaim hooks for the polyglot stack.

Combined working set of Python + Go + Rust + Node must stay under 1.5 GB.
This module never logs secrets.
"""

from __future__ import annotations

import argparse
import gc
import os
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from robber.config import COMBINED_RSS_LIMIT, RAM_GUARD_BYTES, TELEMETRY_INTERVAL_SECONDS
from robber.ssot_db import SsotDB, default_ssot_path, get_ssot_db

PidSource = Callable[[], Iterable[int]]


def rss_bytes(pid: int | None = None) -> int:
    target = int(pid or os.getpid())
    if target <= 0:
        return 0
    if sys.platform == "win32":
        return _rss_windows(target)
    status = Path(f"/proc/{target}/status")
    if not status.is_file():
        return 0
    try:
        text = status.read_text(encoding="ascii", errors="replace")
    except OSError:
        return 0
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2:
                return int(parts[1]) * 1024
    return 0


def _rss_windows(pid: int) -> int:
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("page_fault_count", wintypes.DWORD),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
        ]

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Counters),
        wintypes.DWORD,
    ]
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return 0
    try:
        info = Counters()
        info.cb = ctypes.sizeof(Counters)
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(info), info.cb)
        if not ok:
            return 0
        return int(info.working_set_size)
    finally:
        kernel32.CloseHandle(handle)


def sample_pids(pids: Iterable[int]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total = 0
    seen: set[int] = set()
    for pid in pids:
        n = int(pid)
        if n <= 0 or n in seen:
            continue
        seen.add(n)
        used = rss_bytes(n)
        total += used
        rows.append({"pid": n, "rss": used})
    return {"pids": rows, "combined": total, "limit": COMBINED_RSS_LIMIT}


def reclaim_local(ssot_path: Path | None = None) -> dict[str, Any]:
    """Aggressive reclaim: SQLite shrink, Python GC, drop Rust cdylib caches."""
    collected = gc.collect()
    rust = False
    try:
        from robber.indexer_bridge import release_caches

        release_caches()
        rust = True
    except Exception:
        rust = False
    shrink = False
    try:
        db = get_ssot_db(path=ssot_path) if ssot_path else get_ssot_db()
        db.shrink_memory()
        shrink = True
    except Exception:
        if ssot_path is not None:
            extra: SsotDB | None = None
            try:
                extra = SsotDB(ssot_path).connect()
                extra.shrink_memory()
                shrink = True
            except Exception:
                shrink = False
            finally:
                if extra is not None:
                    extra.close()
    return {"gc": collected, "sqlite_shrink": shrink, "rust_caches": rust}


class MemoryGuard:
    def __init__(
        self,
        pids: PidSource,
        *,
        ssot_path: Path | None = None,
        brain_url: str = "",
        limit: int = COMBINED_RSS_LIMIT,
        interval: float = TELEMETRY_INTERVAL_SECONDS,
    ) -> None:
        self._pids = pids
        self.ssot_path = ssot_path
        self.brain_url = (brain_url or "").rstrip("/")
        self.limit = int(limit)
        self.interval = max(0.5, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: dict[str, Any] = {"combined": 0, "reclaims": 0}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="robber-telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)

    def snapshot(self) -> dict[str, Any]:
        snap = sample_pids(self._pids())
        snap["reclaims"] = int(self.last.get("reclaims") or 0)
        self.last = {**self.last, **snap}
        return snap

    def reclaim(self) -> dict[str, Any]:
        local = reclaim_local(self.ssot_path)
        remote = False
        if self.brain_url:
            try:
                urllib.request.urlopen(
                    f"{self.brain_url}/api/system/health?reclaim=1",
                    timeout=2,
                ).read(4096)
                remote = True
            except Exception:
                remote = False
        self.last["reclaims"] = int(self.last.get("reclaims") or 0) + 1
        local["brain"] = remote
        return local

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            snap = self.snapshot()
            if int(snap.get("combined") or 0) > RAM_GUARD_BYTES:
                self.reclaim()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Robber RSS safeguard")
    parser.add_argument("--pid", action="append", default=[], help="PID to watch (repeatable)")
    parser.add_argument("--ssot", default="", help="path to ssot.db")
    parser.add_argument("--brain", default="", help="python brain base url")
    parser.add_argument("--once", action="store_true", help="sample once and exit")
    parser.add_argument("--reclaim", action="store_true", help="force reclaim then exit")
    args = parser.parse_args(argv)
    pids = [int(p) for p in args.pid] or [os.getpid()]
    ssot = Path(args.ssot) if args.ssot else default_ssot_path()
    guard = MemoryGuard(lambda: pids, ssot_path=ssot, brain_url=args.brain)
    if args.reclaim:
        print(guard.reclaim())
        return 0
    snap = guard.snapshot()
    print(
        "rss combined={combined} limit={limit} pids={n}".format(
            combined=snap["combined"],
            limit=snap["limit"],
            n=len(snap["pids"]),
        )
    )
    if args.once:
        return 0
    guard.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        guard.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
