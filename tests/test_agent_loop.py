from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from robber.agent_loop import AutonomousAgentLoop, NonRecoverableError, StepResult
from robber.context_assembly import ContextAssembler, merge_payload
from robber.engine.pacing_engine import PacingEngine
from robber.handoff_ledger import HandoffLedger
from robber.planner import AutonomousPlanner
from robber.ssot_db import default_ssot_path, reset_ssot_cache
from robber.state_graph import StateGraphTracker
from robber.state_manager import IN_PROGRESS, PAUSED, PENDING, StateManager


@pytest.fixture(autouse=True)
def _close_ssot() -> None:
    yield
    reset_ssot_cache()


async def _noop_sleep(_seconds: float) -> None:
    return None


def test_pending_alias_maps_to_queued_running(tmp_path: Path) -> None:
    manager = StateManager(tmp_path)
    task = manager.put_task("pending alias", status="PENDING")
    assert task["status"] == "queued"
    running = manager.transition_task(task["id"], "IN_PROGRESS")
    assert running["status"] == "running"
    paused = manager.transition_task(task["id"], "PAUSED")
    assert paused["status"] == PAUSED
    assert IN_PROGRESS == "running"
    assert PENDING == "queued"


def test_context_assembler_truncates_and_pulls_ssot(tmp_path: Path) -> None:
    planner = AutonomousPlanner(tmp_path)
    plan = planner.plan("Refactor state graph and update tests")
    manager = planner.manager
    manager.db.put_codebase(
        project_id=manager.project_id,
        path="robber/state_graph.py",
        language="python",
        ast_summary="StateGraphTracker commit rollback",
        symbol_index="StateGraphTracker",
    )
    manager.db.add_memory(
        project_id=manager.project_id,
        kind="note",
        source="research",
        body="state graph nodes live in decisions_ledger " + ("note " * 80),
    )
    assembler = ContextAssembler(tmp_path, manager=manager, window=48)
    mutate = next(task for task in plan.tasks if task.tag == "mutate")
    packed = assembler.assemble(mutate.id)
    assert packed.tokens <= packed.budget
    assert "clipped" in packed.text
    assert "state_graph" in packed.text


def test_loop_pauses_mid_graph_then_resumes(tmp_path: Path) -> None:
    planner = AutonomousPlanner(tmp_path)
    plan = planner.plan("Refactor state graph and update tests")
    manager = planner.manager
    manager.db.put_codebase(
        project_id=manager.project_id,
        path="robber/state_graph.py",
        language="python",
        ast_summary="StateGraphTracker",
    )
    manager.db.add_memory(
        project_id=manager.project_id,
        kind="note",
        source="research",
        body="preserve DAG depends_on ordering",
    )
    seen: list[str] = []
    contexts: list[str] = []

    async def step(task: dict, ctx) -> StepResult:
        tag = str(merge_payload(task.get("payload_json")).get("tag") or "")
        seen.append(tag)
        contexts.append(ctx.text)
        if tag == "mutate" and seen.count("mutate") == 1:
            return StepResult(output="partial mutate", pause=True, tokens=12)
        return StepResult(output=f"done {tag}", tokens=8)

    loop = AutonomousAgentLoop(tmp_path, manager=manager, planner=planner, execute=step, sleep=_noop_sleep)
    first = asyncio.run(loop.run())
    assert first.status == PAUSED
    assert first.paused
    paused = manager.get_task(first.paused)
    assert paused is not None
    assert paused["status"] == PAUSED
    checkpoint = merge_payload(paused["payload_json"]).get("checkpoint") or {}
    assert checkpoint.get("reason") == "tactical-pause"
    assert "partial mutate" in str(checkpoint.get("output") or "")
    db_path = default_ssot_path(tmp_path)
    with sqlite3.connect(str(db_path)) as conn:
        paused_n = conn.execute("SELECT COUNT(*) FROM task_state WHERE status='paused'").fetchone()[0]
        agent_phase = conn.execute(
            "SELECT phase FROM agent_state WHERE worker_id='autonomous-loop'"
        ).fetchone()[0]
        payload = conn.execute(
            "SELECT payload_json FROM task_state WHERE id=?", (first.paused,)
        ).fetchone()[0]
    assert paused_n == 1
    assert agent_phase == PAUSED
    assert "tactical-pause" in payload
    ledger = HandoffLedger(tmp_path)
    assert any(block.kind == "loop_checkpoint" for block in ledger.blocks)
    graph = StateGraphTracker(tmp_path)
    assert any("CHECKPOINT" in node.payload for node in graph.nodes.values())
    assert any("StateGraphTracker" in text for text in contexts)

    second = asyncio.run(loop.resume())
    assert second.status == "completed"
    assert second.paused is None
    rows = {row["id"]: row for row in manager.list_tasks(limit=32)}
    for task in plan.tasks:
        assert rows[task.id]["status"] == "completed"
    assert seen.count("mutate") == 2
    assert "test" in seen and "verify" in seen
    assert manager.list_agents(worker_id="autonomous-loop")[0]["phase"] == "completed"


def test_non_recoverable_marks_failed_and_rolls_back(tmp_path: Path) -> None:
    planner = AutonomousPlanner(tmp_path)
    planner.plan("Refactor state graph and update tests")

    async def boom(task: dict, _ctx) -> StepResult:
        tag = str(merge_payload(task.get("payload_json")).get("tag") or "")
        if tag == "inspect":
            raise NonRecoverableError("non-recoverable inspect")
        return StepResult(output=f"ok {tag}", tokens=4)

    loop = AutonomousAgentLoop(
        tmp_path, manager=planner.manager, planner=planner, execute=boom, sleep=_noop_sleep
    )
    report = asyncio.run(loop.run())
    assert report.failed
    failed = planner.manager.get_task(report.failed[0])
    assert failed is not None
    assert failed["status"] == "failed"
    decisions = planner.manager.db.list_decisions(planner.manager.project_id, limit=32)
    assert any("conflict" in str(row.get("choice") or "") for row in decisions)
    ledger = HandoffLedger(tmp_path)
    assert any(block.kind == "loop_error" for block in ledger.blocks)


def test_pacing_engine_tracks_tokens_and_cooldown(tmp_path: Path) -> None:
    engine = PacingEngine(tmp_path)
    engine.token_budget = 20
    engine.record_request(12, heavy=True)
    engine.record_request(12, heavy=True)
    assert engine.request_count == 2
    assert engine.should_pause() is True
    assert engine.health == "budget"
    assert engine.cool_down_interval() >= 0.0
    snap = engine.health_snapshot()
    assert snap["tokens"] == 24
    engine.flush_caches()
    assert engine.health == "budget"


def test_execute_prompt_emits_state_ack_before_turn(tmp_path: Path) -> None:
    from robber.agent_loop import AgentLoopManager

    frames: list[dict] = []

    class Hub:
        async def broadcast(self, payload):
            frames.append(payload)

        async def send_to(self, _sender, payload):
            frames.append(payload)

    turn_at: list[int] = []

    async def turn(_prompt: str) -> None:
        turn_at.append(len(frames))

    async def dispatch(_sender, _raw: str) -> None:
        return None

    loop = AgentLoopManager(hub=Hub(), state={"workspace": str(tmp_path), "model": "GPT-X / Local"})
    loop.bind_runtime(run_turn=turn, dispatch=dispatch)
    asyncio.run(loop.execute_prompt("Test response"))
    assert turn_at
    ack = next((row for row in frames[: turn_at[0]] if str(row.get("event") or "") == "STATE_ACK"), None)
    assert ack is not None
    assert ack.get("stream_kind") == "ack"
    assert turn_at[0] >= 1


def test_handle_ws_execute_prompt_acks(tmp_path: Path) -> None:
    from robber.agent_loop import AgentLoopManager

    frames: list[dict] = []

    class Hub:
        async def broadcast(self, payload):
            frames.append(payload)

        async def send_to(self, _sender, payload):
            frames.append(payload)

    calls: list[str] = []

    async def turn(prompt: str) -> None:
        calls.append(prompt)

    async def dispatch(_sender, _raw: str) -> None:
        raise AssertionError("dispatch should not handle EXECUTE_PROMPT")

    loop = AgentLoopManager(hub=Hub(), state={"workspace": str(tmp_path)})
    loop.bind_runtime(run_turn=turn, dispatch=dispatch)
    raw = '{"type":"EXECUTE_PROMPT","text":"Test response","prompt":"Test response"}'
    asyncio.run(loop.handle_ws(None, raw))
    assert calls == ["Test response"]
    assert any(str(row.get("event") or "") == "STATE_ACK" for row in frames)
