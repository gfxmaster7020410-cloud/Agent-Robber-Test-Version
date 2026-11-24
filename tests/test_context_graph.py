from __future__ import annotations

import asyncio
from pathlib import Path

from robber.agents.sub_agents import AuditorWorker, CodeWorker
from robber.commands.registry import ExecutionContext, get_registry
from robber.memory.context_graph import ContextGraphEngine, extract_query_symbol
from robber.ssot_context import estimate_tokens
from robber.state_manager import StateManager


def _fixture(root: Path) -> None:
    (root / "rotator.py").write_text(
        'import os\nimport json\n\nclass APIKeyRotator:\n    """heavy unused docstring"""\n'
        "    def __init__(self):\n        self.keys = []\n"
        "    def next_key(self):\n        return self.keys[0] if self.keys else ''\n",
        encoding="utf-8",
    )
    (root / "user.py").write_text(
        "from rotator import APIKeyRotator\n\n"
        "def boot():\n    rot = APIKeyRotator()\n    return rot.next_key()\n",
        encoding="utf-8",
    )


def test_extracts_rotator_from_sentence() -> None:
    assert (
        extract_query_symbol("Where is APIKeyRotator instantiated and used across the codebase?")
        == "APIKeyRotator"
    )


def test_prompt_slice_under_1000_tokens(tmp_path: Path) -> None:
    _fixture(tmp_path)
    engine = ContextGraphEngine(tmp_path)
    packed = engine.prompt_slice(
        "Where is APIKeyRotator instantiated and used across the codebase?",
        depth=2,
    )
    assert packed.query == "APIKeyRotator"
    assert packed.tokens < 1000
    assert estimate_tokens(packed.text) < 1000
    payload = packed.payload
    defs = payload.get("definitions") or []
    sites = payload.get("callsites") or []
    callers = payload.get("callers") or []
    assert any(str(row.get("name")) == "APIKeyRotator" for row in defs)
    assert sites or callers
    limit_ms = 200.0 if packed.engine == "rust" else 250.0
    assert packed.elapsed_ms < limit_ms, packed.elapsed_ms
    assert "heavy unused docstring" not in packed.text


def test_python_fallback_without_dll(tmp_path: Path, monkeypatch) -> None:
    _fixture(tmp_path)

    def boom(*_a: object, **_k: object) -> str:
        raise FileNotFoundError("robber indexer dll is not built")

    monkeypatch.setattr("robber.indexer_bridge.get_symbol_context", boom)
    packed = ContextGraphEngine(tmp_path).prompt_slice("APIKeyRotator", 2)
    assert packed.engine == "python"
    assert packed.query == "APIKeyRotator"
    assert packed.tokens < 1000
    assert any(str(row.get("name")) == "APIKeyRotator" for row in (packed.payload.get("definitions") or []))


def test_prune_strips_docstring_and_unused_import() -> None:
    src = 'import os\nimport json\n\n"""module docs"""\n\ndef ping():\n    """fn docs"""\n    return json.dumps({})\n'
    cleaned = ContextGraphEngine().prune_boilerplate(src)
    assert "os" not in cleaned
    assert "json" in cleaned
    assert "module docs" not in cleaned
    assert "fn docs" not in cleaned


def test_audit_slash_symbol_query(tmp_path: Path) -> None:
    import robber.commands.handlers  # noqa: F401

    _fixture(tmp_path)
    from unittest.mock import AsyncMock, MagicMock

    host = MagicMock()
    host.broadcast = AsyncMock()
    host.inject = MagicMock()
    ctx = ExecutionContext(
        args=["Where", "is", "APIKeyRotator", "instantiated", "and", "used", "across", "the", "codebase?"],
        session_id="test",
        workspace=tmp_path,
        state={"mode": "act", "context_window": 200_000},
        host=host,
    )

    async def go() -> None:
        result = await get_registry().dispatch("audit", ctx)
        assert result.ok
        assert "AMACE symbol graph" in result.content
        assert "APIKeyRotator" in result.content
        assert int(result.extra.get("tokens") or 0) < 1000
        engine = str(result.extra.get("engine") or "")
        limit_ms = 200.0 if engine == "rust" else 250.0
        assert float(result.extra.get("elapsed_ms") or 99) < limit_ms

    asyncio.run(go())


def test_code_and_auditor_use_graph_not_raw_files(tmp_path: Path) -> None:
    huge = "x = 1\n" + ("# pad\n" * 4000)
    (tmp_path / "blob.py").write_text(
        'import os\n"""docs"""\n' + huge + "\nclass Widget:\n    def run(self):\n        return 1\n",
        encoding="utf-8",
    )
    manager = StateManager(tmp_path)
    hops: list[str] = []

    class Hub:
        async def broadcast(self, payload):
            hops.append(str(payload.get("content") or ""))

    coder = CodeWorker(tmp_path, manager=manager, hub=Hub())
    auditor = AuditorWorker(tmp_path, manager=manager, hub=Hub())
    task = {
        "id": "t1",
        "title": "Where is Widget used",
        "payload_json": {"query": "Widget", "files": ["blob.py"], "inputs": ["blob.py"]},
    }

    async def go() -> None:
        code = await coder.run(task, None)
        audit = await auditor.run(task, None)
        assert code.tokens < 1500
        assert audit.tokens < 1500
        assert "pad" not in (code.output or "")
        assert any("[CODER]" in item for item in hops)
        assert any("[AUDITOR]" in item for item in hops)

    asyncio.run(go())
