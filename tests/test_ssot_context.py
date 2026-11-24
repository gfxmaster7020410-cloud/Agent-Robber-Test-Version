from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from robber.ssot import FileMicroLock
from robber.ssot_context import (
    DeadlockRecoveryWatchdog,
    MemoryMappedChunkStreamer,
    atomic_section_lock,
    stream_document,
)


def _volume(tmp_path: Path, *, pages: int = 200, rows: int = 40) -> Path:
    lines = [f"Page {page} line {row} " + ("word " * 8) for page in range(pages) for row in range(rows)]
    target = tmp_path / "volume.txt"
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def test_mmap_streams_200_page_file_in_bounded_windows(tmp_path: Path) -> None:
    target = _volume(tmp_path)
    assert target.stat().st_size > 80_000
    streamer = MemoryMappedChunkStreamer(target, window=4096)
    windows = 0
    widest = 0
    total = 0
    for chunk in streamer.iter_windows():
        windows += 1
        widest = max(widest, len(chunk))
        total += len(chunk)
        assert len(chunk) <= 4096
    assert windows >= 20
    assert widest <= 4096
    assert total == target.stat().st_size
    excerpt = streamer.extract_lines(1, 3)
    assert "Page 0 line 0" in excerpt
    assert "Page 199 line 39" not in excerpt
    streamed = list(stream_document(target, lines_per_chunk=50, max_chunks=400, window=4096))
    assert len(streamed) >= 100
    assert streamed[0].kind == "mmap"


def test_concurrent_atomic_locks(tmp_path: Path) -> None:
    order: list[int] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            with atomic_section_lock("handoff-write", tmp_path, timeout=5.0):
                order.append(index)
                time.sleep(0.01)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(order) == list(range(6))
    lock_path = tmp_path / ".robber" / "locks" / "handoff-write.lock"
    probe = FileMicroLock(lock_path)
    assert probe.acquire(blocking=False) is True
    probe.release()


def test_watchdog_breaks_stale_locks_and_background_loop(tmp_path: Path) -> None:
    lock_dir = tmp_path / ".robber" / "locks"
    lock_dir.mkdir(parents=True)
    stale = lock_dir / "dead.lock"
    stale.write_text('{"pid": 1, "ts": 1.0}', encoding="utf-8")
    watchdog = DeadlockRecoveryWatchdog(lock_dir, stale_after=0.05)
    assert watchdog.sweep(now=time.time()) == 1
    assert not stale.exists()
    stale.write_text('{"pid": 1, "ts": 1.0}', encoding="utf-8")
    watchdog.start(interval=0.05)
    try:
        deadline = time.time() + 2.0
        while stale.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert not stale.exists()
        assert watchdog.sweeps >= 1
    finally:
        watchdog.stop()


def test_ssot_index_and_lock_tolerate_bad_bytes(tmp_path: Path) -> None:
    from robber.ssot import SsotIndex

    robber = tmp_path / ".robber"
    robber.mkdir()
    (robber / "ssot.json").write_bytes(b'{"files": 0, "chunks": [\xff]}')
    index = SsotIndex(tmp_path)
    index.load()
    assert index.files == 0
    lock = FileMicroLock(robber / "locks" / "ssot-reindex.lock")
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_bytes(b"\xff\xfe")
    assert lock.stale(now=time.time(), timeout=0.0) in {True, False}
