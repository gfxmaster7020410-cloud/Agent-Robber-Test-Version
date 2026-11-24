"""Phase 6 Part 2: self-heal, context compression, and snapshot restore."""

from __future__ import annotations

import ast
import asyncio
import json
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import robber.commands.handlers  # noqa: F401
from robber.agent_loop import AutonomousAgentLoop, StepResult
from robber.commands.registry import ExecutionContext, get_registry
from robber.context_assembly import ContextAssembler
from robber.diagnostics.self_healing import SelfHealingEngine as DiagnosticHealer
from robber.engine.recovery import SnapshotRecoveryEngine
from robber.engine.self_healing import MAX_ATTEMPTS, SelfHealingEngine, bind_healer, get_healer
from robber.memory.context_compressor import ContextCompressor
from robber.mutations import FileMutationManager, bind_mutations
from robber.ssot_db import SCHEMA_VERSION, reset_ssot_cache
from robber.state_manager import StateManager


BROKEN = "def add(a, b)\n    return a + b\n"
FIXED_PREFIX = "def add(a, b):"


@pytest.fixture(autouse=True)
def _isolate() -> None:
    bind_healer(None)
    yield
    bind_healer(None)
    reset_ssot_cache()


def _ctx(tmp_path: Path, args: list[str] | None = None) -> ExecutionContext:
    host = MagicMock()
    host.broadcast = AsyncMock()
    host.inject = MagicMock()
    host.pack_digest = MagicMock(return_value="digest")
    return ExecutionContext(
        args=list(args or []),
        session_id="test",
        workspace=tmp_path,
        state={"mode": "act", "last_output": ""},
        host=host,
    )


async def _noop_sleep(_seconds: float) -> None:
    return None


def test_engine_shim_is_the_diagnostics_class() -> None:
    assert SelfHealingEngine is DiagnosticHealer
    assert MAX_ATTEMPTS == 3
    assert get_healer() is None


def test_self_healing_repairs_syntax_and_logs_decision(tmp_path: Path) -> None:
    bind_mutations(FileMutationManager(tmp_path))
    target = tmp_path / "generated.py"
    target.write_text(BROKEN, encoding="utf-8")
    with pytest.raises(SyntaxError):
        ast.parse(BROKEN, filename=str(target))

    healer = SelfHealingEngine(hub=None, root=tmp_path)
    assert healer.repair_source(target) is True

    source = target.read_text(encoding="utf-8")
    assert source.startswith(FIXED_PREFIX)
    ast.parse(source, filename=str(target))
    compile(source, str(target), "exec")

    manager = healer.manager
    decisions = manager.db.list_decisions(manager.project_id, branch="heal")
    assert decisions
    assert any(row.get("version_tag") == "heal" for row in decisions)
    assert any("self-heal" in str(row.get("choice") or "") for row in decisions)
    artifacts = [row for row in manager.list_artifacts(limit=32) if row.get("kind") == "heal"]
    assert artifacts
    snap = manager.db.snapshot()
    assert snap["version"] == SCHEMA_VERSION
    assert manager.db.integrity_check() == "ok"


def test_context_compressor_prunes_prompt_and_keeps_artifacts(tmp_path: Path) -> None:
    manager = StateManager(tmp_path)
    parent = manager.put_task("inspect generated", status="completed")
    fat = "def leftover():\n    pass\n" + ("tool-output-line\n" * 80) + "Error: done\n"
    manager.add_artifact("run", body=fat, kind="loop", task_id=parent["id"], exit_code=0)
    compressor = ContextCompressor(tmp_path, manager=manager)
    saved = compressor.summarize_task(parent["id"])
    assert saved is not None
    summary = compressor.summary_for(parent["id"])
    packed_summary = json.loads(summary)
    assert packed_summary.get("goal")
    assert "leftover" in packed_summary.get("key_decisions", [])
    assert packed_summary.get("test_status") in {"PASSED", "FAILED", "UNKNOWN"}
    raw = manager.list_artifacts(task_id=parent["id"])
    assert raw and len(str(raw[0].get("body") or "")) > 400

    child = manager.put_task(
        "mutate generated",
        depends_on=[parent["id"]],
        payload={"tag": "mutate", "outputs": ["generated.py"]},
    )
    packed = ContextAssembler(tmp_path, manager=manager).assemble(child)
    assert "summary:" in packed.text
    assert fat not in packed.text
    assert packed.text.count("tool-output-line") < 8


def test_snapshot_restore_reverts_files_and_ssot(tmp_path: Path) -> None:
    manager = StateManager(tmp_path)
    target = tmp_path / "module.py"
    target.write_text("stable = True\n", encoding="utf-8")
    seed = manager.put_task("seed", status="queued")
    engine = SnapshotRecoveryEngine(tmp_path, manager=manager)
    manifest = engine.snapshot(label="stable", files=["module.py"], reason="test")
    snap_id = str(manifest["id"])
    assert (tmp_path / ".robber" / "snapshots" / snap_id / "ssot.snapshot.db").is_file()

    target.write_text("stable = False\n", encoding="utf-8")
    extra = manager.put_task("after snapshot", status="queued")
    report = engine.restore(snap_id)
    assert report["ssot"] is True
    assert report["files_restored"] >= 1
    assert target.read_text(encoding="utf-8") == "stable = True\n"
    assert manager.get_task(seed["id"]) is not None
    assert manager.get_task(extra["id"]) is None
    assert manager.db.integrity_check() == "ok"
    assert manager.db.snapshot()["version"] == SCHEMA_VERSION


def test_restore_slash_lists_and_restores(tmp_path: Path) -> None:
    manager = StateManager(tmp_path)
    target = tmp_path / "keep.py"
    target.write_text("ok = 1\n", encoding="utf-8")
    engine = SnapshotRecoveryEngine(tmp_path, manager=manager)
    manifest = engine.snapshot(label="keep", files=["keep.py"], reason="slash")
    target.write_text("ok = 0\n", encoding="utf-8")

    async def go() -> None:
        registry = get_registry()
        listed = await registry.dispatch("restore", _ctx(tmp_path))
        assert listed.ok
        assert str(manifest["id"]) in listed.content
        restored = await registry.dispatch("restore", _ctx(tmp_path, [str(manifest["id"])[:8]]))
        assert restored.ok
        missing = await registry.dispatch("restore", _ctx(tmp_path, ["does-not-exist"]))
        assert not missing.ok

    asyncio.run(go())
    assert target.read_text(encoding="utf-8") == "ok = 1\n"


def test_loop_heals_compiler_failure_without_operator(tmp_path: Path) -> None:
    bind_mutations(FileMutationManager(tmp_path))
    manager = StateManager(tmp_path)
    target = tmp_path / "generated.py"
    target.write_text(BROKEN, encoding="utf-8")
    manager.put_task(
        "compile generated",
        payload={"tag": "mutate", "task_type": "CODE_GEN", "outputs": ["generated.py"]},
    )

    def execute(_task: dict, _ctx: object) -> StepResult:
        try:
            compile(target.read_text(encoding="utf-8"), str(target), "exec")
        except SyntaxError:
            return StepResult(output=traceback.format_exc(), ok=False, tokens=1)
        return StepResult(output="ok", ok=True, tokens=1)

    loop = AutonomousAgentLoop(tmp_path, manager=manager, execute=execute, sleep=_noop_sleep)
    report = asyncio.run(loop.run(max_steps=8))
    assert not report.failed
    source = target.read_text(encoding="utf-8")
    assert source.startswith(FIXED_PREFIX)
    compile(source, str(target), "exec")
    decisions = manager.db.list_decisions(manager.project_id, branch="heal")
    assert any(row.get("version_tag") == "heal" for row in decisions)
    assert manager.db.integrity_check() == "ok"


def test_run_with_heal_does_not_retry_unicode_decode(tmp_path: Path) -> None:
    hits = {"n": 0}

    async def factory() -> None:
        hits["n"] += 1
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

    healer = DiagnosticHealer(hub=None, root=tmp_path)

    async def go() -> None:
        with pytest.raises(UnicodeDecodeError):
            await healer.run_with_heal("agent_turn", factory)

    asyncio.run(go())
    assert hits["n"] == 1
