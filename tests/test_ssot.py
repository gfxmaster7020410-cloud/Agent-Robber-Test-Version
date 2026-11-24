from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from robber.ssot_db import TABLES, ConnectionPool, SsotDB, get_ssot_db, reset_ssot_cache


@pytest.fixture
def db(tmp_path: Path) -> SsotDB:
    store = SsotDB(tmp_path / "ssot.db", pool_size=4)
    store.connect()
    yield store
    store.close()


def test_creates_wal_schema_and_pragmas(db: SsotDB) -> None:
    info = db.pragmas()
    assert info["journal_mode"] == "wal"
    assert info["foreign_keys"] == 1
    assert info["busy_timeout"] >= 1000
    snap = db.snapshot()
    for name in TABLES:
        assert name in snap["tables"]
        assert snap["tables"][name] >= 0
    probe = db._open()
    try:
        names = {
            row[0]
            for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        probe.close()
    for name in (
        "agent_state",
        "task_state",
        "project_state",
        "codebase_state",
        "memory_store",
        "decisions_ledger",
        "execution_artifacts",
        "ssot_change_log",
        "credential_store",
        "system_snapshots",
    ):
        assert name in names


def test_inserts_across_worker_tables(db: SsotDB) -> None:
    project = db.put_project(
        name="robber",
        root_path="H:/proj",
        language="mixed",
        config={"ram_gb": 8},
        metadata={"workers": ["python", "rust", "go", "typescript"]},
    )
    task = db.put_task(project_id=project["id"], title="audit ssot", priority=10, payload={"step": 1})
    child = db.put_task(
        project_id=project["id"],
        title="subtask",
        parent_id=task["id"],
        status="running",
        priority=5,
    )
    agent = db.put_agent(
        worker_id="py-1",
        language="python",
        phase="analyze",
        loop_status="active",
        project_id=project["id"],
        task_id=task["id"],
        telemetry={"tokens": 12},
    )
    rust = db.put_agent(worker_id="rs-1", language="rust", phase="index", project_id=project["id"])
    file_row = db.put_codebase(
        project_id=project["id"],
        path="robber/ssot_db.py",
        file_hash="abc123",
        language="python",
        symbol_index="SsotDB,ConnectionPool",
        ast_summary="class SsotDB",
        size_bytes=1200,
    )
    again = db.put_codebase(
        project_id=project["id"],
        path="robber/ssot_db.py",
        file_hash="def456",
        ast_summary="updated",
        size_bytes=1300,
    )
    note = db.add_memory(
        project_id=project["id"],
        kind="note",
        source="horizon",
        body="long-term research note",
    )
    decision = db.add_decision(
        project_id=project["id"],
        task_id=task["id"],
        choice="use WAL sqlite",
        rationale="8GB local persistence",
        branch="v1.0",
    )
    artifact = db.add_artifact(
        project_id=project["id"],
        task_id=task["id"],
        kind="compiler",
        name="pytest",
        body="26 passed",
        exit_code=0,
    )
    queue = db.list_queue(project["id"])
    assert {row["id"] for row in queue} >= {task["id"], child["id"]}
    assert db.get_project(project["id"])["name"] == "robber"
    assert db.get_task(task["id"])["priority"] == 10
    assert db.get_agent(agent["id"])["phase"] == "analyze"
    assert rust["language"] == "rust"
    assert file_row["id"] == again["id"]
    assert again["file_hash"] == "def456"
    assert note["kind"] == "note"
    assert decision["choice"] == "use WAL sqlite"
    assert artifact["exit_code"] == 0


def test_foreign_key_rejects_orphan_task(db: SsotDB) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.put_task(project_id="missing", title="orphan")


def test_transaction_rollback(db: SsotDB) -> None:
    project = db.put_project(name="keep", root_path="/keep")
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.put_task(project_id=project["id"], title="will vanish")
            raise RuntimeError("boom")
    assert db.list_queue(project["id"]) == []
    assert db.get_project(project["id"]) is not None


def test_concurrent_reads_and_writes(db: SsotDB) -> None:
    project = db.put_project(name="parallel", root_path="/parallel", language="python")
    errors: list[BaseException] = []
    seen: list[str] = []

    def writer(index: int) -> None:
        try:
            row = db.add_memory(
                project_id=project["id"],
                kind="chunk",
                source=f"t{index}",
                body=f"chunk {index}",
            )
            seen.append(row["id"])
            db.put_agent(
                worker_id=f"w{index}",
                language="go" if index % 2 else "typescript",
                phase="running",
                project_id=project["id"],
            )
        except BaseException as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            db.snapshot()
            db.list_queue(project["id"])
            db.get_project(project["id"])
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    threads.extend(threading.Thread(target=reader) for _ in range(4))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(seen) == 8
    assert len(set(seen)) == 8
    snap = db.snapshot()
    assert snap["tables"]["memory_store"] == 8
    assert snap["tables"]["agent_state"] == 8
    assert snap["journal_mode"] == "wal"


def test_pool_exhaustion_times_out(tmp_path: Path) -> None:
    store = SsotDB(tmp_path / "pool.db", pool_size=1)
    store.connect()
    pool = store._require()
    assert isinstance(pool, ConnectionPool)
    held = pool.acquire()
    try:
        pool.timeout = 0.2
        with pytest.raises(TimeoutError):
            pool.acquire()
    finally:
        pool.release(held)
        store.close()


def test_get_ssot_db_singleton(tmp_path: Path) -> None:
    reset_ssot_cache()
    path = tmp_path / "ssot.db"
    first = get_ssot_db(path=path)
    second = get_ssot_db(path=path)
    assert first is second
    first.put_project(name="cached", root_path=str(tmp_path))
    assert second.snapshot()["tables"]["project_state"] == 1
    reset_ssot_cache()
    assert not first.connected


def test_change_log_records_task_mutations(db: SsotDB) -> None:
    project = db.put_project(name="log", root_path="H:/log")
    task = db.put_task(project_id=project["id"], title="watch me")
    db.put_task(
        project_id=project["id"],
        title="watch me",
        status="running",
        task_id=task["id"],
    )
    db.add_artifact(name="diff", body="ok", project_id=project["id"], task_id=task["id"])
    rows = db.list_changes(limit=32)
    tables = {row["table_name"] for row in rows}
    assert "task_state" in tables
    assert "execution_artifacts" in tables
    assert any(row["row_id"] == task["id"] for row in rows)
