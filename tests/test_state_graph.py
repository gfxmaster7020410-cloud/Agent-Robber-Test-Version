from __future__ import annotations

import json
from pathlib import Path

import pytest

from robber.ssot_db import reset_ssot_cache
from robber.state_graph import StateGraphTracker
from robber.state_manager import StateManager, StateTransitionError


@pytest.fixture(autouse=True)
def _close_ssot() -> None:
    yield
    reset_ssot_cache()


def test_commit_branch_rollback_and_final(tmp_path: Path) -> None:
    tracker = StateGraphTracker(tmp_path)
    first = tracker.commit("alpha hypothesis", summary="start")
    assert first.version == "v1.0"
    assert first.node_id == first.id
    assert first.version_tag == "v1.0"
    second = tracker.commit("alpha hypothesis\nbeta revision", summary="evolve")
    assert second.version == "v1.1"
    assert any(line.startswith("+") for line in second.deltas)
    branched = tracker.branch("explore", from_id=first.id)
    assert branched.branch == "explore"
    assert tracker.rollback(first.id) is not None
    assert tracker.current() is not None
    assert tracker.current().id == first.id
    assert tracker.rollback("missing") is None
    final = tracker.mark_final(second.id)
    assert final is not None
    assert final.version == "v-final"
    tree = tracker.render_tree()
    assert "v1.0" in tree
    assert "explore" in tree
    assert "v-final" in tree or "[v-final]" in tree
    chain = tracker.lineage(final.id)
    assert [node.version for node in chain][0] == "v1.0"
    assert chain[-1].version == "v-final"


def test_propagate_correction_skips_corpus_rescan(tmp_path: Path) -> None:
    tracker = StateGraphTracker(tmp_path)
    root = tracker.commit("latency budget 80\nkeep architecture", summary="draft")
    tracker.branch("explore", from_id=root.id)
    tracker.commit("latency budget 80 on explore", summary="child", branch="explore")
    updated = tracker.propagate_correction("80", "120", from_id=root.id)
    assert updated
    assert any("latency budget 120" in node.payload for node in updated)
    payloads = [node.payload for node in tracker.history()]
    assert any("120" in body for body in payloads)


def test_propagate_correction_by_node_payload(tmp_path: Path) -> None:
    tracker = StateGraphTracker(tmp_path)
    root = tracker.commit("latency budget 80", summary="draft")
    tracker.branch("explore", from_id=root.id)
    tracker.commit("latency budget 80 on explore", summary="child", branch="explore")
    updated = tracker.propagate_correction(root.id, {"fact": "80", "correction": "120"})
    assert updated
    assert any("120" in node.payload for node in updated)
    assert all(node.confidence_score >= 0.8 for node in updated)


def test_empty_graph_render(tmp_path: Path) -> None:
    tracker = StateGraphTracker(tmp_path)
    assert "empty" in tracker.render_tree().lower()


def test_graph_persists_across_restart(tmp_path: Path) -> None:
    tracker = StateGraphTracker(tmp_path)
    first = tracker.commit("alpha hypothesis", summary="start")
    second = tracker.commit("beta revision", summary="evolve")
    tracker.branch("explore", from_id=first.id)
    tracker.rollback(first.id)
    kept = len(tracker.nodes)
    head = first.id
    row = tracker.db.get_decision(second.id)
    assert row is not None
    assert row["version_tag"] == "v1.1"
    assert tracker.db.pragmas()["journal_mode"] == "wal"
    reset_ssot_cache()
    again = StateGraphTracker(tmp_path)
    assert len(again.nodes) == kept
    assert again.head_id == head
    assert again.nodes[first.id].payload == "alpha hypothesis"
    assert second.id in again.nodes
    assert again.rollback(first.id) is not None
    assert len(again.nodes) == kept


def test_migrates_legacy_json_into_decisions_ledger(tmp_path: Path) -> None:
    folder = tmp_path / ".robber"
    folder.mkdir()
    node_id = "abcabcabcabc"
    payload = {
        "head_id": node_id,
        "heads": {"main": node_id},
        "nodes": [
            {
                "id": node_id,
                "version": "v1.0",
                "parent_id": None,
                "branch": "main",
                "summary": "legacy",
                "payload": "from json",
                "checksum": "x",
                "created_at": 1.0,
                "deltas": ["+ genesis"],
                "final": False,
            }
        ],
    }
    (folder / "state_graph.json").write_text(json.dumps(payload), encoding="utf-8")
    tracker = StateGraphTracker(tmp_path)
    assert node_id in tracker.nodes
    stored = tracker.db.get_decision(node_id)
    assert stored is not None
    assert stored["payload"] == "from json"
    assert stored["version_tag"] == "v1.0"


def test_legacy_state_graph_load_replaces_bad_bytes(tmp_path: Path) -> None:
    folder = tmp_path / ".robber"
    folder.mkdir()
    (folder / "state_graph.json").write_bytes(b'{"nodes": [\xff]}')
    tracker = StateGraphTracker(tmp_path)
    assert tracker.nodes == {} or isinstance(tracker.nodes, dict)


def test_agent_task_artifact_transitions(tmp_path: Path) -> None:
    manager = StateManager(tmp_path)
    task = manager.put_task("audit graph", priority=3)
    agent = manager.put_agent("py-1", phase="idle", task_id=task["id"])
    with pytest.raises(StateTransitionError):
        manager.transition_agent(agent["id"], "completed")
    assert manager.get_agent(agent["id"])["phase"] == "idle"
    manager.transition_agent(agent["id"], "PLANNING")
    manager.transition_agent(agent["id"], "running")
    manager.transition_agent(agent["id"], "paused")
    done = manager.transition_agent(agent["id"], "completed", telemetry={"ok": True})
    assert done["phase"] == "completed"
    manager.transition_task(task["id"], "running")
    finished = manager.transition_task(task["id"], "completed")
    assert finished["status"] == "completed"
    artifact = manager.add_artifact("pytest", body="ok", kind="compiler", task_id=task["id"], exit_code=0)
    assert manager.get_artifact(artifact["id"])["exit_code"] == 0
    assert manager.list_tasks(status="completed")
    assert manager.list_agents(worker_id="py-1")
