from __future__ import annotations



import asyncio

from pathlib import Path

from unittest.mock import AsyncMock, MagicMock



import robber.commands.handlers  # noqa: F401

from robber.commands.registry import ExecutionContext, get_registry

from robber.config import SNAPSHOT_MAX_BYTES

from robber.engine.recovery import SnapshotRecoveryEngine

from robber.ssot_db import SCHEMA_VERSION

from robber.state_manager import StateManager





def _ctx(tmp_path: Path, args: list[str] | None = None) -> ExecutionContext:

    host = MagicMock()

    host.broadcast = AsyncMock()

    host.emit = AsyncMock()

    host.inject = MagicMock()

    host.pack_digest = MagicMock(return_value="digest")

    return ExecutionContext(

        args=list(args or []),

        session_id="test",

        workspace=tmp_path,

        state={"mode": "act", "last_output": ""},

        host=host,

    )





def test_create_snapshot_is_lightweight_and_logged(tmp_path: Path) -> None:

    manager = StateManager(tmp_path)

    target = tmp_path / "module.py"

    target.write_text("stable = True\n", encoding="utf-8")

    engine = SnapshotRecoveryEngine(tmp_path, manager=manager)

    snap_id = engine.create_snapshot("pre-refactor")

    assert snap_id

    manifest = engine.snapshot(label="high-risk", files=["module.py"], reason="high-risk")

    assert float(manifest["elapsed_ms"]) < 100

    assert int(manifest["bytes"]) < SNAPSHOT_MAX_BYTES

    dest = tmp_path / ".robber" / "snapshots" / str(manifest["id"]) / "ssot.snapshot.db"

    assert dest.is_file()

    exports = list((tmp_path / "data" / "snapshots").glob("snap_*.db"))

    assert exports

    rows = manager.db.list_system_snapshots()

    assert any(item["id"] == manifest["id"] for item in rows)

    assert manager.db.snapshot()["version"] == SCHEMA_VERSION





def test_restore_snapshot_reverts_files_and_task_dag(tmp_path: Path) -> None:

    manager = StateManager(tmp_path)

    target = tmp_path / "module.py"

    target.write_text("stable = True\n", encoding="utf-8")

    seed = manager.put_task("seed", status="queued")

    engine = SnapshotRecoveryEngine(tmp_path, manager=manager)

    manifest = engine.snapshot(label="stable", files=["module.py"], reason="stable")

    snap_id = str(manifest["id"])

    target.write_text("stable = False\n", encoding="utf-8")

    extra = manager.put_task("after snapshot", status="queued")

    assert engine.restore_snapshot(snap_id) is True

    assert float(manifest["elapsed_ms"]) < 100

    assert target.read_text(encoding="utf-8") == "stable = True\n"

    assert manager.get_task(seed["id"]) is not None

    assert manager.get_task(extra["id"]) is None

    assert manager.db.integrity_check() == "ok"





def test_crash_prompt_after_ungraceful_exit(tmp_path: Path) -> None:

    manager = StateManager(tmp_path)

    manager.put_agent("loop", phase="running", loop_status="run")

    engine = SnapshotRecoveryEngine(tmp_path, manager=manager)

    engine.mark_dirty()

    engine.snapshot(reason="pre-crash", files=[])

    prompt = engine.crash_prompt()

    assert "[CRASH-RECOVERY]" in prompt

    assert "/restore" in prompt or "/snapshots" in prompt

    engine.mark_clean()

    manager.put_agent("loop", phase="idle", loop_status="idle")

    assert engine.crash_report() is None





def test_snapshots_slash_lists_and_restore_rolls_back(tmp_path: Path) -> None:

    manager = StateManager(tmp_path)

    target = tmp_path / "keep.py"

    target.write_text("ok = 1\n", encoding="utf-8")

    engine = SnapshotRecoveryEngine(tmp_path, manager=manager)

    manifest = engine.snapshot(label="keep", files=["keep.py"], reason="slash")

    target.write_text("ok = 0\n", encoding="utf-8")



    async def go() -> None:

        registry = get_registry()

        listed = await registry.dispatch("snapshots", _ctx(tmp_path))

        assert listed.ok

        assert str(manifest["id"]) in listed.content

        restored = await registry.dispatch("restore", _ctx(tmp_path, [str(manifest["id"])[:8]]))

        assert restored.ok



    asyncio.run(go())

    assert target.read_text(encoding="utf-8") == "ok = 1\n"

