from __future__ import annotations



import asyncio

from pathlib import Path



from robber.agents.swarm_orchestrator import SwarmOrchestrator, wants_swarm

from robber.ssot_db import reset_ssot_cache

from robber.state_manager import StateManager



PROMPT = "Create a math utility with a test in tests/test_math.py that intentionally fails initially."





def setup_function() -> None:

    reset_ssot_cache()





def teardown_function() -> None:

    reset_ssot_cache()





def test_wants_swarm_on_failing_math_prompt() -> None:

    assert wants_swarm(PROMPT)





def test_swarm_repairs_failing_math_test(tmp_path: Path) -> None:

    frames: list[str] = []



    class Hub:

        async def broadcast(self, payload):

            frames.append(str(payload.get("content") or ""))



    orch = SwarmOrchestrator(tmp_path, manager=StateManager(tmp_path), hub=Hub())

    report = asyncio.run(orch.execute_task_dag(PROMPT))

    blob = "\n".join(frames)

    assert report.ok, blob[-2000:]

    assert report.retries >= 1

    assert report.hops[:4] == ["ARCHITECT", "CODER", "AUDITOR", "TESTER"]

    assert report.hops[4:7] == ["AUDITOR", "CODER", "TESTER"]

    source = (tmp_path / "tests" / "test_math.py").read_text(encoding="utf-8")

    assert "assert add(1, 1) == 2" in source

    assert "== 3" not in source

    kinds = {str(row.get("kind") or "") for row in orch.manager.list_artifacts(limit=64)}

    assert "test" in kinds

    assert "repair" in kinds

    assert "failed assertion" in blob

    assert "test_add" in blob

    assert "micro-diff" in blob

    from robber.indexer_bridge import load_indexer



    dll = load_indexer()

    if dll is not None and hasattr(dll, "robber_run_test_suite"):

        assert "via rust sandbox" in blob

