from __future__ import annotations

import asyncio
from pathlib import Path

from robber.agents.swarm_orchestrator import SwarmOrchestrator, wants_swarm
from robber.ssot_db import reset_ssot_cache
from robber.state_manager import StateManager


def setup_function() -> None:
    reset_ssot_cache()


def teardown_function() -> None:
    reset_ssot_cache()


def test_wants_swarm_on_refactor_not_chat() -> None:
    assert wants_swarm("Refactor key_rotator.py to use async locks and add unit tests.")
    assert wants_swarm("implement foo.py")
    assert not wants_swarm("hello")
    assert not wants_swarm("Test response")


def test_execute_task_dag_hops_four_roles(tmp_path: Path) -> None:
    (tmp_path / "robber").mkdir()
    (tmp_path / "robber" / "key_rotator.py").write_text(
        "import threading\n\nLOCK = threading.Lock()\n\ndef get():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_key_rotator.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    hops: list[str] = []

    class Hub:
        async def broadcast(self, payload):
            text = str(payload.get("content") or "")
            if text.startswith("[") and "]" in text:
                hops.append(text.split("]", 1)[0].strip("[]"))

    orch = SwarmOrchestrator(tmp_path, manager=StateManager(tmp_path), hub=Hub())
    report = asyncio.run(
        orch.execute_task_dag("Refactor key_rotator.py to use async locks and add unit tests.")
    )
    assert report.hops == ["ARCHITECT", "CODER", "AUDITOR", "TESTER"]
    for label in report.hops:
        assert label in hops
    assert len(report.task_ids) == 4
    notes = orch.manager.list_memory(kind="swarm", limit=16)
    assert notes
    agents = orch.manager.list_agents(limit=16)
    names = {str(row.get("worker_id") or "") for row in agents}
    assert "swarm-architect" in names
    assert "swarm-coder" in names
    assert "swarm-auditor" in names
    assert "swarm-tester" in names


def test_agent_loop_broadcasts_swarm_before_turn(tmp_path: Path) -> None:
    from robber.agent_loop import AgentLoopManager

    (tmp_path / "robber").mkdir()
    (tmp_path / "robber" / "key_rotator.py").write_text("X = 1\n", encoding="utf-8")
    frames: list[str] = []

    class Hub:
        async def broadcast(self, payload):
            frames.append(str(payload.get("content") or payload.get("event") or ""))

        async def send_to(self, _sender, payload):
            frames.append(str(payload.get("content") or ""))

    turn_at: list[int] = []

    async def turn(_prompt: str) -> None:
        turn_at.append(len(frames))

    async def dispatch(_sender, _raw: str) -> None:
        return None

    loop = AgentLoopManager(hub=Hub(), state={"workspace": str(tmp_path)})
    loop.bind_runtime(run_turn=turn, dispatch=dispatch)
    asyncio.run(loop.execute_prompt("Refactor key_rotator.py to use async locks and add unit tests."))
    blob = "\n".join(frames)
    assert "[ARCHITECT]" in blob
    assert "[CODER]" in blob
    assert "[AUDITOR]" in blob
    assert "[TESTER]" in blob
    assert turn_at
    assert loop.tree()["swarm"]["hops"] == ["ARCHITECT", "CODER", "AUDITOR", "TESTER"]
