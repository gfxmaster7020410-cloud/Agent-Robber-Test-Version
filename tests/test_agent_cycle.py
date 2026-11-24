from __future__ import annotations

import asyncio
from pathlib import Path

from robber.agent_loop import AgentLoopManager, format_telemetry, inject_error_delta
from robber.handoff_ledger import HandoffLedger
from robber.mutations import FileMutationManager, bind_mutations
from robber.state_graph import StateGraphTracker


def test_inject_error_delta_records_graph_and_ledger(tmp_path: Path) -> None:
    inject_error_delta(RuntimeError("SyntaxError: bad"), "fix mod.py", root=tmp_path)
    graph = StateGraphTracker(tmp_path)
    assert graph.nodes
    assert any("ERROR DELTA" in node.payload for node in graph.nodes.values())
    ledger = HandoffLedger(tmp_path)
    assert any(block.kind == "error_delta" for block in ledger.blocks)


def test_perception_cycle_on_failure(tmp_path: Path) -> None:
    bind_mutations(FileMutationManager(tmp_path))
    (tmp_path / "mod.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    loop = AgentLoopManager(
        hub=None,
        state={"amace_cycle": True, "workspace": str(tmp_path), "model": "GPT-X / Local"},
    )

    async def boom(_query: str) -> None:
        raise RuntimeError("SyntaxError: unexpected indent")

    async def dispatch(_sender, _raw: str) -> None:
        return None

    loop.bind_runtime(run_turn=boom, dispatch=dispatch)
    loop.healer = None

    async def go() -> None:
        try:
            await loop._run_agent_turn("implement foo in mod.py")
        except RuntimeError:
            return
        raise AssertionError("expected RuntimeError")

    asyncio.run(go())
    graph = StateGraphTracker(tmp_path)
    assert len(graph.nodes) >= 2
    assert any("error delta" in node.summary.lower() for node in graph.nodes.values())
    text = format_telemetry(tmp_path, loop)
    assert "LOOP telemetry" in text
    assert "tokens" in text
    assert loop.tree()["session"]["session_id"]


def test_tree_exposes_session_and_graph_keys() -> None:
    loop = AgentLoopManager(hub=None)
    tree = loop.tree()
    assert "session" in tree
    assert "turn_tokens" in tree
    assert "stale_locks" in tree
    assert "graph" in tree
    assert "horizon" in tree
    assert "swarm" in tree


def test_after_turn_decode_error_does_not_fail_turn(tmp_path: Path) -> None:
    bind_mutations(FileMutationManager(tmp_path))
    loop = AgentLoopManager(
        hub=None,
        state={"amace_cycle": True, "workspace": str(tmp_path), "model": "GPT-X / Local"},
    )
    ran = {"ok": False}

    async def ok(_query: str) -> None:
        ran["ok"] = True

    async def dispatch(_sender, _raw: str) -> None:
        return None

    async def boom(_query: str) -> None:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

    loop.bind_runtime(run_turn=ok, dispatch=dispatch)
    loop.healer = None
    loop.cycle.after_turn = boom  # type: ignore[method-assign]
    asyncio.run(loop._run_agent_turn("hi"))
    assert ran["ok"] is True
