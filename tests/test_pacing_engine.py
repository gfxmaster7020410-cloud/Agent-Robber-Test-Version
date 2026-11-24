from __future__ import annotations

import asyncio
import json
from pathlib import Path

from robber.agent_loop import AgentLoopManager
from robber.engine.pacing_engine import CoolDownScheduler, PacingEngine, looks_like_horizon
from robber.handoff_ledger import HandoffLedger
from robber.state_graph import StateGraphTracker


def _paper(tmp_path: Path, *, pages: int = 200, rows: int = 40) -> Path:
    lines = [f"Page {page} line {row} " + ("word " * 16) for page in range(pages) for row in range(rows)]
    target = tmp_path / "volume.txt"
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def _blocks(tmp_path: Path, *, lines: int = 150) -> Path:
    target = tmp_path / "blocks.txt"
    body = "\n".join(f"Chapter {index // 50} row {index} " + ("x" * 8) for index in range(lines))
    target.write_text(body, encoding="utf-8")
    return target


async def _noop_sleep(_seconds: float) -> None:
    return None


def test_looks_like_horizon_rejects_batch_mark_and_hello(tmp_path: Path) -> None:
    engine = PacingEngine(tmp_path)
    assert looks_like_horizon("hello", root=tmp_path) is False
    assert engine.looks_like_horizon("[HORIZON_BATCH 1/2] continue") is False
    assert engine.looks_like_horizon("long-horizon research paper") is True
    assert engine.looks_like_horizon("use micro-batch pacing") is True


def test_decompose_200_page_mmap_document(tmp_path: Path) -> None:
    target = _paper(tmp_path)
    assert target.stat().st_size > 80_000
    engine = PacingEngine(tmp_path)
    chunks = engine.decompose("read volume.txt", path=target)
    macros = {chunk.macro_id for chunk in chunks}
    assert len(chunks) >= 100
    assert len(macros) >= 2
    assert engine.looks_like_horizon("read volume.txt") is True
    blob = json.dumps([chunk.public() for chunk in chunks])
    assert '"text"' not in blob


def test_cooldown_grows_after_heavy_streak() -> None:
    scheduler = CoolDownScheduler(base=0.4, maximum=8.0, heavy_after=2)
    waits: list[float] = []
    for _ in range(5):
        scheduler.mark_heavy()
        waits.append(scheduler.interval())
    assert waits[0] == 0.4
    assert waits[1] > waits[0]
    assert waits[-1] >= waits[1]
    assert waits[-1] <= 8.0


def test_run_records_timeouts_and_continues(tmp_path: Path) -> None:
    target = _blocks(tmp_path, lines=120)
    engine = PacingEngine(tmp_path)
    job = engine.create_job("read blocks.txt", path=target)
    assert job.total >= 2

    async def sleepy(_chunk, _job) -> str:
        await asyncio.sleep(10)
        return "late"

    async def go() -> None:
        await engine.run(job.query, analyze=sleepy, sleep=_noop_sleep, job=job, timeout=0.05)

    asyncio.run(go())
    assert job.timeouts
    assert job.status == "done"
    assert job.cursor == job.total
    assert len(job.insights) == job.total


def test_checkpoint_resume_skips_finished_insights(tmp_path: Path) -> None:
    target = _blocks(tmp_path, lines=120)
    engine = PacingEngine(tmp_path)
    job = engine.create_job("read blocks.txt", path=target)
    analyzed: list[str] = []

    async def first_wave(chunk, current) -> str:
        analyzed.append(chunk.id)
        engine.request_pause()
        return f"seen {chunk.id}"

    async def go_pause() -> None:
        await engine.run(job.query, analyze=first_wave, sleep=_noop_sleep, job=job)

    asyncio.run(go_pause())
    assert job.status == "paused"
    assert job.cursor == 1
    assert job.cursor < job.total
    first_id = next(iter(job.insights))
    engine.checkpoint(job, reason="tactical-pause")
    graph = StateGraphTracker(tmp_path)
    assert any("HORIZON" in node.payload for node in graph.nodes.values())
    ledger = HandoffLedger(tmp_path)
    assert any(block.kind == "horizon_gap" for block in ledger.blocks)
    gaps = engine.pending_gaps(job)
    assert any("horizon gap" in item.lower() for item in gaps)

    loaded_engine = PacingEngine(tmp_path)
    loaded = loaded_engine.load_job(job.id)
    assert loaded is not None
    assert loaded.cursor == 1
    assert loaded.insights[first_id].startswith("seen ")

    async def rest(chunk, current) -> str:
        analyzed.append(chunk.id)
        return f"later {chunk.id}"

    async def go_resume() -> None:
        loaded_engine.pause_requested = False
        loaded.status = "running"
        await loaded_engine.run(loaded.query, analyze=rest, sleep=_noop_sleep, job=loaded)

    asyncio.run(go_resume())
    assert loaded.status == "done"
    assert first_id in loaded.insights
    assert loaded.insights[first_id].startswith("seen ")
    assert analyzed[0] == first_id
    assert analyzed.count(first_id) == 1


def test_synthesis_runs_acrm_on_conflicting_insights(tmp_path: Path) -> None:
    engine = PacingEngine(tmp_path)
    job = engine.create_job("long-horizon compare latency")
    left = job.chunks[0]
    job.insights[left.id] = "latency budget 80"
    extra = left
    if job.total < 2:
        from robber.engine.pacing_engine import MicroChunk

        extra = MicroChunk("h0001", 1, left.macro_id, "Block 2", "prompt", 2, 2, "tail")
        job.chunks.append(extra)
    job.insights[extra.id] = "latency budget 120"
    text = engine.synthesize(job)
    assert "HORIZON SYNTHESIS" in text
    assert "conflicts=" in text
    assert "80" in text and "120" in text
    assert "[ACRM]" in text or "disagree" in text


def test_horizon_json_omits_corpus_text(tmp_path: Path) -> None:
    target = _paper(tmp_path, pages=20, rows=40)
    engine = PacingEngine(tmp_path)
    job = engine.create_job("read volume.txt", path=target)
    blob = (tmp_path / ".robber" / "horizon" / f"{job.id}.json").read_text(encoding="utf-8")
    row = json.loads(blob)
    assert "text" not in row["chunks"][0]
    assert "preview" in row["chunks"][0]


def test_agent_loop_horizon_uses_batch_mark(tmp_path: Path) -> None:
    loop = AgentLoopManager(hub=None, state={"workspace": str(tmp_path)})
    seen: list[str] = []

    async def turn(prompt: str) -> None:
        seen.append(prompt)
        loop.state["last_output"] = "batch-ok"

    async def dispatch(_sender, _raw: str) -> None:
        return None

    loop.bind_runtime(run_turn=turn, dispatch=dispatch)

    async def go() -> None:
        job = await loop.start_horizon("long-horizon summarize the protocol")
        assert loop._horizon_task is not None
        await loop._horizon_task
        return job

    job = asyncio.run(go())
    assert job.status == "done"
    assert seen
    assert seen[0].startswith("[HORIZON_BATCH")
    assert "long-horizon summarize the protocol" in seen[0]
    assert loop.tree()["horizon"]["status"] == "done"
    assert looks_like_horizon(seen[0], root=tmp_path) is False


def test_execute_prompt_hello_is_not_horizon(tmp_path: Path) -> None:
    loop = AgentLoopManager(hub=None, state={"workspace": str(tmp_path)})
    calls = {"n": 0}

    async def turn(_prompt: str) -> None:
        calls["n"] += 1

    async def dispatch(_sender, _raw: str) -> None:
        return None

    loop.bind_runtime(run_turn=turn, dispatch=dispatch)
    asyncio.run(loop.execute_prompt("hello"))
    assert calls["n"] == 1
    assert loop.pacing.job is None
    assert loop._horizon_task is None
