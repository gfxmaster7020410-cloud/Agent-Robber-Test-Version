#!/usr/bin/env python3
"""Polyglot end-to-end checks: TS client protocol, Go gateway, Python brain, Rust, SSOT."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Fail(Exception):
    pass


def _ok(name: str) -> None:
    print(f"PASS  {name}")


def _check(cond: bool, name: str, detail: str = "") -> None:
    if not cond:
        raise _Fail(f"{name}: {detail}" if detail else name)
    _ok(name)


def _free_port() -> int:
    for _ in range(8):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
        sock.close()
        if port != 8787:
            return port
    raise _Fail("could not allocate ephemeral port")


def step_ssot_integrity_and_migrate() -> Path:
    from robber.launcher import ProcessStack
    from robber.launcher import LaunchConfig
    from robber.ssot_db import SCHEMA_VERSION

    tmp = Path(tempfile.mkdtemp(prefix="robber-e2e-"))
    ssot = tmp / ".robber" / "ssot.db"
    stack = ProcessStack(LaunchConfig(root=tmp, ssot=ssot, build=False, headless=True))
    snap = stack.verify_ssot()
    _check(snap.get("version") == SCHEMA_VERSION, "ssot schema version")
    _check(stack.ssot is not None and stack.ssot.integrity_check().lower() == "ok", "ssot integrity")
    stack.stop()
    return tmp


def step_python_intent_planner_router(tmp: Path) -> dict[str, Any]:
    from robber.intent import classify_intent
    from robber.planner import AutonomousPlanner, CODE_GEN
    from robber.router.model_router import ModelRouter

    prompt = "create helper.py with a add() function"
    _check(classify_intent(prompt) == "action", "intent classifies action")
    plan = AutonomousPlanner(tmp).plan(prompt)
    _check(len(plan.tasks) >= 1, "planner DAG has tasks", str(len(plan.tasks)))
    kinds = {task.task_type for task in plan.tasks}
    _check(CODE_GEN in kinds or any("py" in (task.title + task.tag) for task in plan.tasks), "planner includes code work")
    decision = ModelRouter().choose(prompt=prompt, task_type=CODE_GEN)
    _check(bool(decision.primary.label), "router primary")
    _check(len(decision.chain) >= 1, "router fallback chain")
    return {"prompt": prompt, "tasks": len(plan.tasks), "route": decision.primary.label}


def _ensure_rust_cdylib() -> None:
    from robber.indexer_bridge import load_indexer
    from robber.launcher import find_cargo, rust_build_env

    if load_indexer() is not None:
        return
    cargo = find_cargo()
    if not cargo:
        raise _Fail("rust cdylib not built and cargo missing")
    env = rust_build_env(ROOT)
    built = subprocess.run(
        [cargo, "build", "--manifest-path", str(ROOT / "indexer" / "Cargo.toml"), "--lib"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if built.returncode != 0:
        raise _Fail(f"cargo build --lib failed: {(built.stderr or built.stdout)[-400:]}")
    if load_indexer() is None:
        raise _Fail("rust cdylib not built (indexer/target or CARGO_TARGET_DIR)")


def step_rust_index_sandbox_patch(tmp: Path) -> dict[str, Any]:
    from robber.indexer_bridge import apply_diff_atomic, index_workspace, load_indexer, run_tool_sandboxed
    from robber.ssot_db import SsotDB

    _ensure_rust_cdylib()
    src = tmp / "helper.py"
    src.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    ssot = tmp / ".robber" / "ssot.db"
    dll = load_indexer()
    out: dict[str, Any] = {"dll": bool(dll)}
    if dll is None:
        raise _Fail("rust cdylib not built (indexer/target or CARGO_TARGET_DIR)")
    report = index_workspace(tmp, ssot)
    _check(int(report.get("files_parsed") or 0) >= 1, "rust AST index", json.dumps(report))
    out["index"] = report
    program = "cmd" if sys.platform == "win32" else "sh"
    args = ["/C", "echo robber-e2e"] if sys.platform == "win32" else ["-c", "echo robber-e2e"]
    tool = run_tool_sandboxed(tmp, program, args, timeout_ms=4000, ssot_db=ssot)
    _check("robber-e2e" in str(tool.get("stdout") or ""), "rust sandbox echo", str(tool))
    diff = (
        "--- a/helper.py\n+++ b/helper.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
        "-    return a + b\n+    return a + b + 0\n"
    )
    patch = apply_diff_atomic(tmp, diff, ssot)
    _check(bool(patch.get("ok")), "rust atomic diff", json.dumps(patch))
    body = src.read_text(encoding="utf-8")
    _check("return a + b + 0" in body, "patched helper.py on disk")
    db = SsotDB(ssot).connect()
    arts = db.list_artifacts() if hasattr(db, "list_artifacts") else []
    db.wal_checkpoint("PASSIVE")
    db.close()
    out["artifacts"] = len(arts) if isinstance(arts, list) else arts
    out["patch"] = patch
    return out


def step_telemetry_reclaim(tmp: Path) -> None:
    from robber.config import COMBINED_RSS_LIMIT
    from robber.telemetry import MemoryGuard, reclaim_local, rss_bytes

    used = rss_bytes()
    _check(used > 0, "rss sample")
    _check(used < COMBINED_RSS_LIMIT, "rss under 1.5GB cap", str(used))
    info = reclaim_local(tmp / ".robber" / "ssot.db")
    _check(info.get("sqlite_shrink") is True, "sqlite shrink_memory")
    guard = MemoryGuard(lambda: [os.getpid()], ssot_path=tmp / ".robber" / "ssot.db")
    snap = guard.snapshot()
    _check(int(snap["combined"]) < COMBINED_RSS_LIMIT, "guard combined rss")
    _check(int(snap["limit"]) == COMBINED_RSS_LIMIT, "guard limit")


def step_gateway_and_js_client() -> None:
    from robber.launcher import find_go, gateway_binary

    binary = gateway_binary(ROOT)
    go = find_go()
    if go:
        built = subprocess.run(
            [go, "build", "-o", str(binary), "."],
            cwd=ROOT / "gateway",
            capture_output=True,
            text=True,
        )
        if built.returncode != 0:
            raise _Fail(f"go build failed: {(built.stderr or built.stdout)[-300:]}")
    elif not binary.is_file():
        print("SKIP  go gateway binary (toolchain missing)")
        return
    brain_port = _free_port()
    gw_port = _free_port()
    ipc_port = _free_port()
    stub = _BrainStub()
    stub.start(brain_port)
    tmp = Path(tempfile.mkdtemp(prefix="robber-e2e-gw-"))
    ssot = tmp / ".robber" / "ssot.db"
    ssot.parent.mkdir(parents=True, exist_ok=True)
    from robber.ssot_db import SsotDB

    SsotDB(ssot).connect().close()
    err_path = tmp / "gateway.err"
    err_file = open(err_path, "wb")
    proc = subprocess.Popen(
        [
            str(binary),
            "-addr",
            f"127.0.0.1:{gw_port}",
            "-root",
            str(tmp),
            "-brain",
            f"http://127.0.0.1:{brain_port}",
            "-ssot",
            str(ssot),
            "-ipc",
            f"127.0.0.1:{ipc_port}",
        ],
        cwd=str(ROOT / "gateway"),
        stdout=err_file,
        stderr=subprocess.STDOUT,
    )
    node: subprocess.Popen[str] | None = None
    try:
        try:
            _wait(f"http://127.0.0.1:{gw_port}/health", 12.0)
        except _Fail:
            err_file.flush()
            raise _Fail(f"gateway health: {err_path.read_text(encoding='utf-8', errors='replace')[-400:]}")
        node = _start_ws_probe(gw_port)
        time.sleep(1.2)
        req = urllib.request.Request(
            f"http://127.0.0.1:{gw_port}/api/v1/agent/task",
            data=json.dumps({"prompt": "e2e from tui", "title": "e2e"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        _check(bool(payload.get("id")), "gateway POST /api/v1/agent/task", str(payload))
        snap_url = f"http://127.0.0.1:{gw_port}/api/v1/ssot/snapshot"
        with urllib.request.urlopen(snap_url, timeout=4) as resp:
            shot = json.loads(resp.read().decode("utf-8"))
        _check(isinstance(shot.get("tasks"), list) and shot["tasks"], "gateway SSOT snapshot")
        if node is not None:
            out, err = node.communicate(timeout=8)
            _check(
                node.returncode == 0 and ("ssot" in out or "task_state" in out or payload["id"] in out),
                "ts/node websocket to /api/v1/stream?channel=ssot",
                (err or out)[-240:],
            )
    finally:
        if node is not None and node.poll() is None:
            node.kill()
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
        err_file.close()
        stub.stop()


def _start_ws_probe(port: int) -> subprocess.Popen[str] | None:
    ws_mod = ROOT / "ui" / "node_modules" / "ws"
    if not ws_mod.is_dir():
        print("SKIP  node ws client (ui/node_modules/ws missing)")
        return None
    script = (
        "import WebSocket from 'ws';"
        "const ws = new WebSocket(process.argv[1]);"
        "const timer = setTimeout(() => { console.log('timeout'); process.exit(2); }, 5000);"
        "ws.on('message', (buf) => {"
        "  clearTimeout(timer);"
        "  process.stdout.write(String(buf));"
        "  ws.close();"
        "  process.exit(0);"
        "});"
        "ws.on('error', (err) => { console.error(String(err)); process.exit(1); });"
    )
    return subprocess.Popen(
        ["node", "--input-type=module", "-e", script, f"ws://127.0.0.1:{port}/api/v1/stream?channel=ssot"],
        cwd=str(ROOT / "ui"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def step_shutdown_leaves_no_locks(tmp: Path) -> None:
    from robber.launcher import clear_stale_locks

    lock_dir = tmp / ".robber" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    leftover = lock_dir / "e2e.lock"
    leftover.write_text("stale", encoding="utf-8")
    n = clear_stale_locks(lock_dir)
    _check(n >= 1 and not leftover.exists(), "lock files removed on teardown")


def step_launcher_headless() -> None:
    """Boot the real orchestrator on ephemeral ports (never 8787)."""
    tmp = Path(tempfile.mkdtemp(prefix="robber-e2e-launch-"))
    ssot = tmp / ".robber" / "ssot.db"
    brain = _free_port()
    gw_port = _free_port()
    ipc_port = _free_port()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "robber.launcher",
            "--root",
            str(tmp),
            "--ssot",
            str(ssot),
            "--brain-port",
            str(brain),
            "--gateway-port",
            str(gw_port),
            "--ipc",
            f"127.0.0.1:{ipc_port}",
            "--skip-build",
            "--headless",
            "--timeout",
            "6",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=90,
    )
    log = (proc.stderr or "") + "\n" + (proc.stdout or "")
    _check(proc.returncode == 0, "launcher headless exit 0", log[-600:])
    _check("stack ready" in log, "launcher stack ready", log[-600:])
    leftover = list((tmp / ".robber" / "locks").glob("*.lock")) if (tmp / ".robber" / "locks").is_dir() else []
    _check(not leftover, "launcher teardown cleared locks", str(leftover))


class _BrainStub:
    def __init__(self) -> None:
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, port: int) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = b'{"ok":true,"stub":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(min(length, 1_000_000))
                body = b'{"ok":true}'
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self._httpd = httpd
        self._thread = thread

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()


def _wait(url: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1.2).read(2048)
            return
        except Exception as exc:
            last = str(exc)
            time.sleep(0.2)
    raise _Fail(f"timeout {url} ({last})")


def run_suite() -> int:
    failed = 0
    tmp: Path | None = None
    try:
        tmp = step_ssot_integrity_and_migrate()
        step_python_intent_planner_router(tmp)
        step_rust_index_sandbox_patch(tmp)
        step_telemetry_reclaim(tmp)
        step_gateway_and_js_client()
        step_launcher_headless()
        step_shutdown_leaves_no_locks(tmp)
    except _Fail as exc:
        print(f"FAIL  {exc}")
        failed = 1
    except Exception as exc:
        print(f"FAIL  {type(exc).__name__}: {exc}")
        failed = 1
    if failed == 0:
        print("e2e polyglot loop ok")
    return failed


def test_full_system() -> None:
    code = run_suite()
    assert code == 0


if __name__ == "__main__":
    raise SystemExit(run_suite())
