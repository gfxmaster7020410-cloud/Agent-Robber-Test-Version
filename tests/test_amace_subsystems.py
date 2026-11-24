from __future__ import annotations

import time
from pathlib import Path

import pytest

from robber.conflict_resolver import ConflictResolutionMatrix
from robber.handoff_ledger import HandoffLedger
from robber.ssot import (
    DeadlockRecoveryWatchdog,
    FileMicroLock,
    MemoryMappedChunkStreamer,
    QuantumSafeChecksum,
    SsotIndex,
    atomic_section_lock,
)
from robber.ssot_context import adaptive_relevance_threshold, bidirectional_gap_scan
from robber.state_graph import StateGraphTracker


def test_quantum_safe_checksum_roundtrip() -> None:
    digest = QuantumSafeChecksum.digest("ledger-block")
    assert len(digest) == 64
    assert QuantumSafeChecksum.verify("ledger-block", digest)
    assert not QuantumSafeChecksum.verify("tampered", digest)


def test_stale_lock_sweep_and_atomic_section(tmp_path: Path) -> None:
    lock_dir = tmp_path / ".robber" / "locks"
    lock_dir.mkdir(parents=True)
    stale = lock_dir / "dead.lock"
    stale.write_text('{"pid": 1, "ts": 1.0}', encoding="utf-8")
    recovered = DeadlockRecoveryWatchdog(lock_dir, stale_after=30.0).sweep(now=time.time())
    assert recovered == 1
    assert not stale.exists()

    with atomic_section_lock("ssot-reindex", tmp_path, timeout=2.0):
        held = FileMicroLock(lock_dir / "ssot-reindex.lock")
        assert held.acquire(blocking=False) is False
    again = FileMicroLock(lock_dir / "ssot-reindex.lock")
    assert again.acquire(blocking=False) is True
    again.release()


def test_atomic_lock_times_out_when_held(tmp_path: Path) -> None:
    lock_dir = tmp_path / ".robber" / "locks"
    lock_dir.mkdir(parents=True)
    held = FileMicroLock(lock_dir / "ssot-reindex.lock")
    assert held.acquire(blocking=False) is True
    try:
        with pytest.raises(TimeoutError):
            with atomic_section_lock("ssot-reindex", tmp_path, timeout=0.12):
                pass
    finally:
        held.release()


def test_mmap_streamer_chunks_without_read_text(tmp_path: Path) -> None:
    target = tmp_path / "book.txt"
    target.write_text("\n".join(f"page {i} " * 20 for i in range(120)), encoding="utf-8")
    streamer = MemoryMappedChunkStreamer(target, window=2048)
    windows = list(streamer.iter_windows())
    assert windows
    chunks = list(streamer.iter_line_chunks("book.txt", lines_per_chunk=30))
    assert len(chunks) >= 4
    assert chunks[0].kind == "mmap"
    assert "page 0" in chunks[0].text


def test_ssot_reindex_uses_lock(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("def x():\n    return 1\n", encoding="utf-8")
    index = SsotIndex(tmp_path)
    stats = index.reindex()
    assert int(stats["files"]) >= 1
    assert (tmp_path / ".robber" / "locks" / "ssot-reindex.lock").exists()


def test_adaptive_threshold_and_gap_scan() -> None:
    assert adaptive_relevance_threshold(2) == 0.0
    assert adaptive_relevance_threshold(16) == 0.18
    assert adaptive_relevance_threshold(1000) > adaptive_relevance_threshold(16)
    ledger = (
        "[UNHANDLED] planning->* write tests for mmap streamer\n"
        "TODO: document lock sweep\n"
        + ("noise\n" * 60)
        + "FIXME: tail gap still open\n"
    )
    gaps = bidirectional_gap_scan(ledger)
    assert any("mmap" in item.lower() for item in gaps)
    assert any("tail gap" in item.lower() for item in gaps)


def test_state_graph_branch_delta_and_rollback(tmp_path: Path) -> None:
    tracker = StateGraphTracker(tmp_path)
    first = tracker.commit("alpha hypothesis", summary="start")
    assert first.version == "v1.0"
    second = tracker.commit("alpha hypothesis\nbeta revision", summary="evolve")
    assert second.version == "v1.1"
    assert any(line.startswith("+") for line in second.deltas)
    branched = tracker.branch("explore", from_id=first.id)
    assert branched.branch == "explore"
    tracker.rollback(first.id)
    assert tracker.current() is not None
    assert tracker.current().id == first.id
    final = tracker.mark_final(second.id)
    assert final is not None
    assert final.version == "v-final"


def test_handoff_ledger_consume_and_prune(tmp_path: Path) -> None:
    ledger = HandoffLedger(tmp_path)
    block = ledger.post("TODO: claim this task", source="planning", target="coding")
    assert block.verify()
    assert ledger.pending("coding")
    ledger.consume(block.id, "coding")
    assert not ledger.pending("coding")
    block.created_at = 0.0
    ledger.blocks = [block]
    dropped = ledger.prune(now=time.time())
    assert dropped >= 1


def test_conflict_matrix_detects_factual_and_review_block() -> None:
    matrix = ConflictResolutionMatrix()
    report = matrix.report(
        {
            "planning": "latency budget 120",
            "coding": "```diff\n+++ b/mod.py\n+ok\n```\nlatency budget 80",
            "review": "Missing tests on the patch",
        }
    )
    kinds = set(report["kinds"])
    assert "factual" in kinds or "logical" in kinds
    assert report["count"] >= 1
    reconciled = matrix.reconcile(
        {
            "coding": "```diff\n+++ b/a.py\n+a\n```",
            "planning": "```diff\n+++ b/b.py\n+b\n```",
        }
    )
    assert "coding" in reconciled
