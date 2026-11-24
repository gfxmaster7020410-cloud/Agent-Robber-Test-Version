from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from robber.intent_analyzer import CODE_MUTATION, IntentAnalyzer
from robber.planner import AutonomousPlanner, PlannedTask, PlannerError, parse_depends, topological_order
from robber.ssot_db import default_ssot_path, reset_ssot_cache


@pytest.fixture(autouse=True)
def _close_ssot() -> None:
    yield
    reset_ssot_cache()


def test_refactor_prompt_builds_dag_and_persists(tmp_path: Path) -> None:
    query = "Refactor state graph and update tests"
    analyzer = IntentAnalyzer(tmp_path)
    intent = analyzer.analyze(query)
    assert intent.category == CODE_MUTATION
    assert "robber/state_graph.py" in intent.files
    assert any("test" in item.lower() for item in intent.success_criteria)
    planner = AutonomousPlanner(tmp_path, manager=analyzer.manager, analyzer=analyzer)
    plan = planner.plan(intent)
    tags = [task.tag for task in plan.tasks]
    assert tags[0] == "goal"
    assert "inspect" in tags
    assert "mutate" in tags
    assert "test" in tags
    assert "verify" in tags
    by_tag = {task.tag: task for task in plan.tasks}
    assert by_tag["inspect"].id in by_tag["mutate"].depends_on
    assert by_tag["mutate"].id in by_tag["test"].depends_on
    assert by_tag["test"].id in by_tag["verify"].depends_on
    assert plan.order == topological_order(plan.tasks)
    assert plan.order.index(by_tag["inspect"].id) < plan.order.index(by_tag["mutate"].id)
    assert plan.order.index(by_tag["mutate"].id) < plan.order.index(by_tag["test"].id)
    rows = analyzer.manager.list_tasks(limit=32)
    assert len(rows) >= 5
    stored = {row["id"]: row for row in rows}
    mutate = stored[by_tag["mutate"].id]
    assert parse_depends(mutate["depends_on"]) == by_tag["mutate"].depends_on
    payload = json.loads(mutate["payload_json"])
    assert "robber/state_graph.py" in payload.get("outputs")
    assert payload.get("task_type") == "CODE_GEN"
    assert by_tag["inspect"].task_type == "REASONING"
    assert by_tag["verify"].task_type == "FAST_DRAFT"
    assert mutate["parent_id"] == by_tag["goal"].id
    decisions = analyzer.manager.db.list_decisions(analyzer.manager.project_id, graph_only=True)
    assert any(row.get("version_tag") == "plan" for row in decisions)
    assert any("CODE_MUTATION" in (row.get("choice") or "") for row in decisions)
    db_path = default_ssot_path(tmp_path)
    assert db_path.is_file()
    with sqlite3.connect(str(db_path)) as conn:
        task_n = conn.execute("SELECT COUNT(*) FROM task_state").fetchone()[0]
        decision_n = conn.execute("SELECT COUNT(*) FROM decisions_ledger").fetchone()[0]
        cols = {row[1] for row in conn.execute("PRAGMA table_info(task_state)")}
    assert task_n >= 5
    assert decision_n >= 2
    assert "depends_on" in cols


def test_parallel_mutations_share_inspect_dependency(tmp_path: Path) -> None:
    planner = AutonomousPlanner(tmp_path)
    plan = planner.plan("Fix bug in alpha.py and beta.py")
    mutates = [task for task in plan.tasks if task.tag == "fix"]
    assert len(mutates) == 2
    inspect = next(task for task in plan.tasks if task.tag == "inspect")
    assert all(inspect.id in task.depends_on for task in mutates)
    assert mutates[0].id not in mutates[1].depends_on
    assert mutates[1].id not in mutates[0].depends_on
    verify = next(task for task in plan.tasks if task.tag == "verify")
    assert mutates[0].id in verify.depends_on
    assert mutates[1].id in verify.depends_on


def test_cycle_is_rejected() -> None:
    one = PlannedTask(id="a", title="a", tag="x", depends_on=["b"])
    two = PlannedTask(id="b", title="b", tag="x", depends_on=["a"])
    with pytest.raises(PlannerError):
        topological_order([one, two])
