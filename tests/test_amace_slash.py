from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import robber.commands.handlers  # noqa: F401
from robber.commands.registry import ExecutionContext, get_registry
from robber.handoff_ledger import HandoffLedger
from robber.ssot import DeadlockRecoveryWatchdog, MemoryMappedChunkStreamer
from robber.state_graph import StateGraphTracker


def _ctx(tmp_path: Path, args: list[str] | None = None) -> ExecutionContext:
    host = MagicMock()
    host.broadcast = AsyncMock()
    host.inject = MagicMock()
    return ExecutionContext(
        args=list(args or []),
        session_id="test",
        workspace=tmp_path,
        state={"mode": "act", "context_window": 200_000},
        host=host,
    )


def test_amace_slash_commands_registered() -> None:
    names = {item.name for item in get_registry().all()}
    for name in ("audit", "heal", "snapshots", "restore", "local", "sync", "handoff", "graph", "loop", "horizon", "resolve", "plugin"):
        assert name in names


def test_heal_sync_graph_handoff_resolve(tmp_path: Path) -> None:
    lock_dir = tmp_path / ".robber" / "locks"
    lock_dir.mkdir(parents=True)
    stale = lock_dir / "dead.lock"
    stale.write_text('{"pid": 1, "ts": 1.0}', encoding="utf-8")
    (tmp_path / "mod.py").write_text("def x():\n    return 1\n", encoding="utf-8")
    StateGraphTracker(tmp_path).commit("v1 hypothesis", summary="seed")
    ledger = HandoffLedger(tmp_path)
    ledger.post("```diff\n+++ b/a.py\n+a\n```\nlatency budget 80", source="coding", target="*")
    ledger.post("latency budget 120", source="planning", target="coding")

    async def go() -> None:
        registry = get_registry()
        heal = await registry.dispatch("heal", _ctx(tmp_path))
        assert heal.ok
        assert "stale lock" in heal.content
        audit = await registry.dispatch("audit", _ctx(tmp_path))
        assert "AMACE audit" in audit.content
        graph = await registry.dispatch("graph", _ctx(tmp_path))
        assert graph.ok
        assert "v1.0" in graph.content or "hypothesis" in graph.content
        handoff = await registry.dispatch("handoff", _ctx(tmp_path, ["coding"]))
        assert handoff.ok
        assert "payload" in handoff.content.lower() or "planning" in handoff.content
        resolve = await registry.dispatch("resolve", _ctx(tmp_path))
        assert resolve.ok
        assert "ACRM" in resolve.content
        sync = await registry.dispatch("sync", _ctx(tmp_path))
        assert sync.ok
        assert "SSOT synced" in sync.content

    asyncio.run(go())
    assert DeadlockRecoveryWatchdog(lock_dir).sweep() == 0


def test_horizon_status_slash(tmp_path: Path) -> None:
    async def go() -> None:
        result = await get_registry().dispatch("horizon", _ctx(tmp_path))
        assert result.ok
        assert "horizon" in result.content.lower()

    asyncio.run(go())


def test_mmap_200_page_document(tmp_path: Path) -> None:
    lines = [f"Page {page} line {row} " + ("word " * 16) for page in range(200) for row in range(40)]
    target = tmp_path / "volume.txt"
    target.write_text("\n".join(lines), encoding="utf-8")
    assert target.stat().st_size > 80_000
    chunks = list(
        MemoryMappedChunkStreamer(target, window=8192).iter_line_chunks(
            "volume.txt",
            lines_per_chunk=50,
            max_chunks=400,
        )
    )
    assert len(chunks) >= 100
    assert chunks[0].kind == "mmap"
    assert "Page 0" in chunks[0].text
    assert chunks[-1].end >= 1000


def test_mmap_empty_file_is_a_noop(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    streamer = MemoryMappedChunkStreamer(empty)
    assert list(streamer.iter_windows()) == []
    assert list(streamer.iter_line_chunks("empty.txt")) == []
