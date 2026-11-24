"""Spec-driven architecture, AST merge, security/LSP, and profiler."""



from __future__ import annotations



import asyncio

import json

import shutil

from pathlib import Path

from unittest.mock import AsyncMock, MagicMock



import pytest



import robber.commands.handlers  # noqa: F401

from robber.agents.swarm_orchestrator import SwarmOrchestrator

from robber.commands.registry import ExecutionContext, get_registry

from robber.config import COMBINED_RSS_LIMIT

from robber.diagnostics.lsp_security import AstSecurityAuditor, LspClient

from robber.diagnostics.profiler import scan_nested_loops

from robber.engine.git_engine import ConflictError, GitEngine, ast_merge, conventional_message, python_ast_merge

from robber.engine.sdd_spec import SPEC_META_KEY, SpecDrivenEngine

from robber.ssot_db import SCHEMA_VERSION, TABLES, reset_ssot_cache

from robber.state_manager import StateManager

from robber.telemetry import rss_bytes



GOAL = (

    "Design and implement an authenticated user session management system "

    "with REST endpoints and SQLite storage."

)





@pytest.fixture(autouse=True)

def _ssot() -> None:

    reset_ssot_cache()

    yield

    reset_ssot_cache()





def test_spec_lands_in_project_state_before_code(tmp_path: Path) -> None:

    manager = StateManager(tmp_path)

    spec = SpecDrivenEngine(tmp_path, manager=manager).plan(GOAL)

    assert "/api/sessions" in spec.openapi["paths"]

    assert "/api/sessions/current" in spec.openapi["paths"]

    assert "CREATE TABLE IF NOT EXISTS users" in spec.ddl

    assert "CREATE TABLE IF NOT EXISTS sessions" in spec.ddl

    assert any(item.get("port") == "SessionStore" for item in spec.dip)

    row = manager.db.get_project(manager.project_id)

    assert row is not None

    meta = json.loads(row["metadata_json"])

    assert meta.get("dal") == "state_manager"

    stored = meta[SPEC_META_KEY]

    assert stored["id"] == spec.spec_id

    assert "sessions" in stored["ddl"]

    notes = manager.list_memory(kind="spec", limit=8)

    assert notes

    artifacts = manager.list_artifacts(limit=16)

    assert any(str(item.get("kind") or "") == "spec" for item in artifacts)

    py_files = list(tmp_path.rglob("*.py"))

    assert py_files == []

    snap = manager.db.snapshot()

    assert snap["version"] == SCHEMA_VERSION

    assert set(TABLES).issubset(set(snap["tables"]))

    assert rss_bytes() < COMBINED_RSS_LIMIT





def test_ast_merge_imports_and_functions() -> None:

    base = "def foo():\n    return 1\n"

    ours = "import os\n\ndef foo():\n    return 1\n\ndef bar():\n    return 2\n"

    theirs = "import sys\n\ndef foo():\n    return 1\n\ndef baz():\n    return 3\n"

    merged = python_ast_merge(base, ours, theirs)

    assert "import os" in merged

    assert "import sys" in merged

    assert "def bar()" in merged

    assert "def baz()" in merged

    assert "<<<<<<<" not in merged

    assert conventional_message(base, ours).startswith("feat:")

    with pytest.raises(ConflictError):

        python_ast_merge(

            "def foo():\n    return 1\n",

            "def foo():\n    return 2\n",

            "def foo():\n    return 3\n",

        )

    assert ast_merge(base, ours, theirs).count("def ") >= 3





def test_security_and_lsp_session_route(tmp_path: Path) -> None:

    src = (

        "def login(user_id):\n"

        '    cur.execute("SELECT * FROM users WHERE id=" + user_id)\n'

        '    password = "hardcoded-secret"\n'

        "    return user_id\n"

    )

    findings = AstSecurityAuditor().scan_text(src, "routes.py")

    rules = {item.rule for item in findings}

    assert "sqli" in rules

    assert "secret" in rules

    clean = "def add(a, b):\n    return a + b\n"

    diags = LspClient().diagnose("util.py", clean)

    assert not any(item.severity == "error" for item in diags)

    (tmp_path / "ok.py").write_text(clean, encoding="utf-8")

    injected = LspClient(

        transport=lambda _payload: {"diagnostics": []}

    ).diagnose("ok.py", clean)

    assert injected == []





def test_profiler_flags_nested_loops() -> None:

    src = (

        "def scan(outer, inner):\n"

        "    for a in outer:\n"

        "        for b in inner:\n"

        "            if a == b:\n"

        "                return a\n"

        "    return None\n"

    )

    hits = scan_nested_loops(src, "slow.py")

    assert hits

    assert hits[0].kind == "nested_loop"

    assert "set/dict" in hits[0].suggestion





def test_git_engine_commits_merged_module(tmp_path: Path) -> None:

    if not shutil.which("git"):

        pytest.skip("git not installed")

    base = "def foo():\n    return 1\n"

    ours = "import os\n\ndef foo():\n    return 1\n\ndef bar():\n    return 2\n"

    theirs = "import sys\n\ndef foo():\n    return 1\n\ndef baz():\n    return 3\n"

    merged = ast_merge(base, ours, theirs)

    path = tmp_path / "session_lib.py"

    path.write_text(merged, encoding="utf-8")

    engine = GitEngine(tmp_path)

    message = engine.commit_message(base, merged)

    assert message.split(":", 1)[0] in {"feat", "refactor", "fix", "test"}

    report = engine.commit_paths(["session_lib.py"], message=message, branch="feat/sdd-session")

    assert report["ok"] is True, report.get("output")





def test_swarm_still_four_hops_and_stores_spec(tmp_path: Path) -> None:

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

    frames: list[str] = []



    class Hub:

        async def broadcast(self, payload):

            frames.append(str(payload.get("content") or ""))



    orch = SwarmOrchestrator(tmp_path, manager=StateManager(tmp_path), hub=Hub())

    report = asyncio.run(

        orch.execute_task_dag("Refactor key_rotator.py to use async locks and add unit tests.")

    )

    assert report.hops == ["ARCHITECT", "CODER", "AUDITOR", "TESTER"]

    blob = "\n".join(frames)

    assert "SDD spec stored" in blob

    loaded = SpecDrivenEngine(tmp_path, manager=orch.manager).load()

    assert loaded is not None

    assert loaded.openapi.get("paths")

    assert rss_bytes() < COMBINED_RSS_LIMIT





def test_spec_slash_command(tmp_path: Path) -> None:

    host = MagicMock()

    host.broadcast = AsyncMock()

    ctx = ExecutionContext(

        args=GOAL.split(),

        session_id="test",

        workspace=tmp_path,

        state={"mode": "plan"},

        host=host,

    )



    async def go() -> None:

        result = await get_registry().dispatch("spec", ctx)

        assert result.ok

        assert "SpecDrivenEngine" in result.content

        assert "/api/sessions" in json.dumps(result.extra.get("spec") or {})



    asyncio.run(go())

    assert rss_bytes() < COMBINED_RSS_LIMIT

