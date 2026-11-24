"""Phase 6 Part 3: local offload, AES-256 vault, packaging, offline path."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import robber.commands.handlers  # noqa: F401
from robber.commands.registry import ExecutionContext, get_registry
from robber.config import COMBINED_RSS_LIMIT, LOCAL_CONTEXT_WINDOW
from robber.local.ollama_adapter import OllamaAdapter, attach_local_fallbacks, bind_adapter
from robber.planner import AutonomousPlanner, CODE_GEN
from robber.router import APIKeyRotator, FailoverMatrix, ModelRouter, UnifiedProviderProxy
from robber.router.model_router import RouteDecision, RouteTarget
from robber.router.session import MODEL_META, MODEL_ROUTES
from robber.security.vault import CredentialVault, bind_vault
from robber.ssot_db import SCHEMA_VERSION, reset_ssot_cache
from robber.state_manager import StateManager
from robber.telemetry import rss_bytes

ROOT = Path(__file__).resolve().parents[1]


def _ctx(tmp_path: Path, args: list[str] | None = None, state: dict | None = None) -> ExecutionContext:
    host = MagicMock()
    host.broadcast = AsyncMock()
    host.inject = MagicMock()
    host.pack_digest = MagicMock(return_value="digest")
    payload = {"mode": "act", "last_output": ""}
    if state:
        payload.update(state)
    return ExecutionContext(
        args=list(args or []),
        session_id="test",
        workspace=tmp_path,
        state=payload,
        host=host,
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBBER_VAULT_KEY", os.urandom(32).hex())
    routes = {key: value for key, value in MODEL_ROUTES.items()}
    meta = {key: dict(value) for key, value in MODEL_META.items()}
    bind_adapter(None)
    bind_vault(None)
    yield
    MODEL_ROUTES.clear()
    MODEL_ROUTES.update(routes)
    MODEL_META.clear()
    MODEL_META.update(meta)
    bind_adapter(None)
    bind_vault(None)
    reset_ssot_cache()


def test_local_context_capped_at_4k() -> None:
    target = ModelRouter().target("GPT-X / Local")
    assert target is not None
    assert target.context_length <= LOCAL_CONTEXT_WINDOW
    assert target.cost_per_m == 0.0
    adapter = OllamaAdapter(probe=lambda _url: None)
    _text, tokens, clipped = adapter.throttle("token " * 20_000)
    assert clipped is True
    assert tokens <= LOCAL_CONTEXT_WINDOW


def test_adapter_registers_zero_cost_fallback_without_cloud() -> None:
    adapter = OllamaAdapter(
        hosts={"ollama": "http://127.0.0.1:11434"},
        probe=lambda url: {"models": [{"name": "qwen2.5-coder:3b"}, {"name": "tinyllama:1.1b"}]}
        if url.endswith("/api/tags")
        else None,
    )
    rows = adapter.scan()
    assert rows[0].ok
    assert adapter.preferred.startswith("qwen2.5-coder")
    targets = adapter.fallback_targets()
    assert targets
    assert all(item.cost_per_m == 0.0 for item in targets)
    assert all(item.context_length <= LOCAL_CONTEXT_WINDOW for item in targets)
    decision = RouteDecision(
        task_type=CODE_GEN,
        primary=RouteTarget("cloud", "openai", "gpt", cost_per_m=1.0),
        fallbacks=[],
        prompt="create a python utility",
        tokens=4,
    )
    attached = attach_local_fallbacks(decision, adapter)
    assert any(item.provider == "ollama" for item in attached.chain)
    assert attached.chain[-1].cost_per_m == 0.0


def test_aes256_vault_roundtrip_in_ssot(tmp_path: Path) -> None:
    vault = CredentialVault(tmp_path)
    bind_vault(vault)
    secret = "sk-test-never-log"
    public = vault.put("openai", secret)
    assert public["fingerprint"]
    assert secret not in json.dumps(public)
    assert vault.get("openai") == secret
    listed = vault.list_public()
    blob = json.dumps(listed)
    assert secret not in blob
    assert "sk-test" not in blob
    raw = vault.db.find_credential("openai")
    assert raw is not None
    assert raw["ciphertext"] != secret
    stored = sqlite3.connect(str(vault.db.path)).execute(
        "SELECT ciphertext FROM credential_store"
    ).fetchone()[0]
    assert secret not in stored
    snap = vault.db.snapshot()
    assert snap["version"] == SCHEMA_VERSION
    assert "credential_store" in snap["tables"]
    assert vault.db.integrity_check() == "ok"


def test_offline_task_indexes_tests_and_saves_state(tmp_path: Path) -> None:
    source = tmp_path / "util.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    compile(source.read_text(encoding="utf-8"), str(source), "exec")
    manager = StateManager(tmp_path)
    plan = AutonomousPlanner(tmp_path, manager=manager).plan(
        "Create a Python utility script, index AST, run tests, and save state"
    )
    assert plan.tasks
    manager.db.put_codebase(
        project_id=manager.project_id,
        path="util.py",
        language="python",
        ast_summary="def add",
        symbol_index="add",
        file_hash="util",
        size_bytes=source.stat().st_size,
    )
    (tmp_path / "test_util.py").write_text(
        "from util import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-c", "from util import add; assert add(2, 3) == 5"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    manager.add_artifact("pytest", body="ok add()", kind="test", exit_code=0)
    manager.record_decision("offline util", rationale="local ollama path", version_tag="offline")
    used = rss_bytes()
    assert 0 < used < COMBINED_RSS_LIMIT
    assert manager.db.integrity_check() == "ok"
    assert manager.db.snapshot()["version"] == SCHEMA_VERSION


def test_failover_uses_mock_ollama_not_cloud(tmp_path: Path) -> None:
    httpd = _start_ollama_stub()
    try:
        host = f"http://127.0.0.1:{httpd.server_port}"
        adapter = OllamaAdapter(hosts={"ollama": host}, timeout=2.0)
        adapter.scan()
        bind_adapter(adapter)
        assert adapter.endpoints and adapter.endpoints[0].ok
        matrix = FailoverMatrix(
            tmp_path,
            router=_LocalFirstRouter(host),
            proxy=UnifiedProviderProxy(timeout=5.0),
            rotator=APIKeyRotator(keys_fn=lambda _p: []),
            manager=StateManager(tmp_path),
            sleep=_sleep,
        )
        result = asyncio.run(matrix.execute("write util.py", task_type=CODE_GEN))
        assert result.response is not None
        assert "def add" in result.response.text
        assert result.response.provider in {"ollama", "local"}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_package_skip_native_has_no_secrets(tmp_path: Path) -> None:
    path = ROOT / "scripts" / "build" / "package_agent.py"
    spec = importlib.util.spec_from_file_location("package_agent", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    dest = tmp_path / "bundle"
    report = mod.package(dest, skip_native=True, skip_ui=True)
    assert Path(report["dest"]).is_dir()
    assert (dest / "robber" / "local" / "ollama_adapter.py").is_file()
    assert (dest / "robber" / "security" / "vault.py").is_file()
    assert (dest / "MANIFEST.json").is_file()
    assert (dest / "launch.cmd").is_file()
    assert (dest / ".env.example").is_file()
    assert "cryptography" in (dest / "requirements.txt").read_text(encoding="utf-8")
    text = json.dumps(report)
    assert "sk-" not in text
    names = [item.name for item in dest.rglob("*") if item.is_file()]
    assert ".env" not in names
    assert "accounts.db" not in names
    assert "credentials.json" not in names
    manifest = json.loads((dest / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["local_context_window"] == LOCAL_CONTEXT_WINDOW


def test_exit_slash_purges_without_orphans(tmp_path: Path) -> None:
    supervisor = MagicMock()
    supervisor.kill_all = AsyncMock(return_value=0)
    pool = MagicMock()
    pool.stop_all = AsyncMock()

    async def go() -> None:
        listed = await get_registry().dispatch("local", _ctx(tmp_path))
        assert listed.ok
        assert "local window" in listed.content
        result = await get_registry().dispatch(
            "exit",
            _ctx(tmp_path, state={"supervisor": supervisor, "worker_pool": pool}),
        )
        assert result.ok
        assert result.extra.get("quit") is True
        assert supervisor.kill_all.await_count == 1
        assert pool.stop_all.await_count == 1

    asyncio.run(go())
    assert rss_bytes() < COMBINED_RSS_LIMIT


async def _sleep(_seconds: float) -> None:
    return None


class _LocalFirstRouter:
    def __init__(self, host: str) -> None:
        self.host = host

    def choose(self, task=None, *, prompt: str = "", task_type: str | None = None, prefer: str | None = None) -> RouteDecision:
        return RouteDecision(
            task_type=task_type or CODE_GEN,
            primary=RouteTarget(
                "GPT-X / Local",
                "ollama",
                "qwen2.5-coder:3b",
                api_base=self.host,
                context_length=LOCAL_CONTEXT_WINDOW,
                cost_per_m=0.0,
                protocol="NATIVE",
            ),
            fallbacks=[],
            prompt=prompt or "write util.py",
            tokens=4,
        )


def _start_ollama_stub() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_GET(self) -> None:
            if self.path.endswith("/api/tags"):
                self._send(
                    {
                        "models": [
                            {"name": "qwen2.5-coder:3b"},
                            {"name": "deepseek-coder:1.5b"},
                        ]
                    }
                )
                return
            self._send({"error": "no"}, code=404)

        def do_POST(self) -> None:
            if self.path.endswith("/api/chat") or self.path.endswith("/api/generate"):
                self._send(
                    {
                        "model": "qwen2.5-coder:3b",
                        "message": {"content": "def add(a, b):\n    return a + b\n"},
                        "done_reason": "stop",
                        "prompt_eval_count": 8,
                        "eval_count": 16,
                    }
                )
                return
            self._send({"error": "no"}, code=404)

        def _send(self, payload: dict, code: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    if port == 8787:
        raise RuntimeError("refusing to bind 8787")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def test_kind_for_ollama_uses_native_chat_on_any_port() -> None:
    from robber.router.provider_proxy import KIND_OLLAMA, KIND_OPENAI, UnifiedProviderProxy

    assert UnifiedProviderProxy.kind_for("ollama", "http://127.0.0.1:59123", "NATIVE") == KIND_OLLAMA
    assert UnifiedProviderProxy.kind_for("ollama", "http://127.0.0.1:59123", "OPENAI_COMPATIBLE") == KIND_OLLAMA
    assert UnifiedProviderProxy.kind_for("ollama", "http://127.0.0.1:11434/v1", "OPENAI_COMPATIBLE") == KIND_OPENAI
    assert UnifiedProviderProxy.kind_for("llamacpp", "http://127.0.0.1:8080", "OPENAI_COMPATIBLE") == KIND_OPENAI


def test_adapter_scans_llamacpp_gguf() -> None:
    adapter = OllamaAdapter(
        hosts={"llamacpp": "http://127.0.0.1:8080"},
        probe=lambda url: {"data": [{"id": "qwen2.5-coder-1.5b-instruct.gguf"}]}
        if url.endswith("/v1/models")
        else None,
    )
    rows = adapter.scan()
    assert any(row.kind == "llamacpp" and row.ok for row in rows)
    targets = adapter.fallback_targets()
    assert targets
    assert all(item.cost_per_m == 0.0 for item in targets)
    assert all(item.protocol == "OPENAI_COMPATIBLE" for item in targets)
    assert all(item.context_length <= LOCAL_CONTEXT_WINDOW for item in targets)


def test_gguf_listing_is_filenames_only(tmp_path: Path) -> None:
    from robber.local.ollama_adapter import _gguf_names

    folder = tmp_path / "data" / "gguf"
    folder.mkdir(parents=True)
    (folder / "tiny.gguf").write_bytes(b"GGUF")
    assert _gguf_names(tmp_path) == ["tiny.gguf"]


def test_vault_proxy_token_encrypted_at_rest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if "OPENROUTER" in name.upper():
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("robber.vault.iter_keys", lambda _provider: [])
    vault = CredentialVault(tmp_path)
    secret = "proxy-token-never-plain"
    public = vault.put("openrouter", secret, kind="proxy")
    assert secret not in json.dumps(public)
    stored = sqlite3.connect(str(vault.db.path)).execute(
        "SELECT ciphertext, kind FROM credential_store"
    ).fetchone()
    assert stored[1] == "proxy"
    assert secret not in stored[0]
    assert vault.get("openrouter", kind="proxy") == secret
    bind_vault(vault)
    from robber.security.vault import iter_secured_keys

    assert iter_secured_keys("openrouter") == [secret]


def test_adapter_complete_hits_native_chat() -> None:
    httpd = _start_ollama_stub()
    try:
        host = f"http://127.0.0.1:{httpd.server_port}"
        adapter = OllamaAdapter(hosts={"ollama": host}, timeout=2.0)
        adapter.scan()
        bind_adapter(adapter)
        text = asyncio.run(adapter.complete("write add"))
        assert "def add" in text
        from robber.router.session import Engine, stream_completion

        engine = Engine("GPT-X / Local", "ollama/qwen2.5-coder:3b", "ollama", None, api_base=host)

        async def collect() -> str:
            parts: list[str] = []
            async for _role, chunk in stream_completion(engine, "write add", "act"):
                parts.append(chunk)
            return "".join(parts)

        blob = asyncio.run(collect())
        assert "def add" in blob
    finally:
        httpd.shutdown()
        httpd.server_close()
