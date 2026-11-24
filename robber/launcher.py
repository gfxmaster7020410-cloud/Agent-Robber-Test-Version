"""Unified process orchestrator for the four-layer Agent Robber stack.

Startup order: SSOT migrate -> (optional builds) -> Go gateway -> Python brain
-> telemetry -> TypeScript TUI (foreground). SIGINT/SIGTERM checkpoints WAL
and kills only children spawned by this process.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robber.ssot_db import SCHEMA_VERSION, SsotDB, default_ssot_path
from robber.telemetry import MemoryGuard, rss_bytes


_active_stack: ProcessStack | None = None


def _log(message: str) -> None:
    stream = sys.stderr
    line = f"[launcher] {message}\n"
    try:
        stream.write(line)
        stream.flush()
    except UnicodeEncodeError:
        stream.buffer.write(line.encode("ascii", "replace"))
        stream.flush()


@dataclass
class LaunchConfig:
    root: Path
    ssot: Path
    install: Path = field(default_factory=lambda: ROOT)
    brain_host: str = "127.0.0.1"
    brain_port: int = 8787
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8790
    ipc: str = "127.0.0.1:8791"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 3000
    build: bool = True
    headless: bool = False
    timeout: float = 0.0
    python: str = field(default_factory=lambda: sys.executable)


class ProcessStack:
    def __init__(self, config: LaunchConfig) -> None:
        self.config = config
        self.procs: list[subprocess.Popen[Any]] = []
        self.guard: MemoryGuard | None = None
        self.ssot: SsotDB | None = None
        self._stop = threading.Event()
        self._pg: int = 0
        self._logs: list[Any] = []
        self._dashboard_proc: subprocess.Popen[Any] | None = None
        if sys.platform == "win32":
            self._pg = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    @property
    def install(self) -> Path:
        return self.config.install

    @property
    def brain_url(self) -> str:
        return f"http://{self.config.brain_host}:{self.config.brain_port}"

    @property
    def gateway_url(self) -> str:
        return f"http://{self.config.gateway_host}:{self.config.gateway_port}"

    def pids(self) -> list[int]:
        out = [os.getpid()]
        if self._dashboard_proc is not None and self._dashboard_proc.poll() is None and self._dashboard_proc.pid:
            out.append(int(self._dashboard_proc.pid))
        for proc in self.procs:
            if proc.poll() is None and proc.pid:
                out.append(int(proc.pid))
        return out

    def _launch_dashboard(self) -> bool:
        """Launch the web dashboard server in a background process (Phase 2)."""
        dashboard_py = self.install / "robber" / "ui" / "web_dashboard.py"
        if not dashboard_py.is_file():
            _log("web dashboard module missing; skipping")
            return False
        env = os.environ.copy()
        env["ROBBER_WORKSPACE"] = str(self.config.root)
        env["ROBBER_SSOT"] = str(self.config.ssot)
        env["DASHBOARD_HOST"] = self.config.dashboard_host
        env["DASHBOARD_PORT"] = str(self.config.dashboard_port)
        pythonpath = str(self.install)
        prev = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = pythonpath if not prev else pythonpath + os.pathsep + prev
        logs = self.config.root / ".robber" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / "dashboard.stderr.log"
        log_handle = open(log_path, "ab", buffering=0)
        self._logs.append(log_handle)
        try:
            self._dashboard_proc = subprocess.Popen(
                [self.config.python, str(dashboard_py)],
                cwd=str(self.config.root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                creationflags=self._pg,
            )
        except Exception as exc:
            _log(f"dashboard spawn failed: {exc}")
            return False
        _log(f"dashboard pid={self._dashboard_proc.pid}")
        deadline = time.time() + 8.0
        url = f"http://{self.config.dashboard_host}:{self.config.dashboard_port}/"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1.0) as resp:
                    if resp.status < 500:
                        return True
            except Exception:
                pass
            time.sleep(0.25)
        _log("dashboard port did not respond within 8s")
        return False

    def verify_ssot(self) -> dict[str, Any]:
        db = SsotDB(self.config.ssot).connect()
        check = db.integrity_check()
        if check.lower() != "ok":
            db.close()
            raise RuntimeError(f"ssot integrity_check={check}")
        pragmas = db.pragmas()
        if pragmas.get("journal_mode") != "wal":
            db.close()
            raise RuntimeError("ssot journal_mode is not wal")
        snap = db.snapshot()
        if str(snap.get("version") or "") != SCHEMA_VERSION:
            _log(f"ssot schema version {snap.get('version')} (expected {SCHEMA_VERSION})")
        self.ssot = db
        try:
            from robber.engine.recovery import SnapshotRecoveryEngine

            prompt = SnapshotRecoveryEngine(self.config.root).crash_prompt()
            if prompt:
                _log(prompt)
        except Exception:
            pass
        return snap

    def maybe_build(self) -> None:
        if not self.config.build:
            return
        gateway_bin = gateway_binary(self.install)
        if not gateway_bin.is_file():
            go = find_go()
            if go:
                _log("building go gateway")
                dest = gateway_bin
                dest.parent.mkdir(parents=True, exist_ok=True)
                run_ok(
                    [go, "build", "-o", str(dest), "."],
                    cwd=self.install / "gateway",
                )
            else:
                _log("go toolchain missing; gateway build skipped")
        dll = rust_cdylib(self.install)
        if dll is None:
            cargo = find_cargo()
            if cargo:
                _log("building rust indexer cdylib")
                run_ok(
                    [
                        cargo,
                        "build",
                        "--manifest-path",
                        str(self.install / "indexer" / "Cargo.toml"),
                        "--lib",
                    ],
                    cwd=self.install,
                    env=rust_build_env(self.install),
                )
            else:
                _log("cargo missing; rust build skipped")

    def start(self) -> None:
        self.verify_ssot()
        self.maybe_build()

        # Phase 2: Launch web dashboard BEFORE model routing or agent loop
        dashboard_ok = self._launch_dashboard()
        if dashboard_ok:
            _log(f"web dashboard ready http://{self.config.dashboard_host}:{self.config.dashboard_port}")
        else:
            _log("web dashboard failed to bind; continuing without dashboard")

        env = os.environ.copy()
        env["ROBBER_WORKSPACE"] = str(self.config.root)
        env["ROBBER_SSOT"] = str(self.config.ssot)
        env["ROBBER_PORT"] = str(self.config.brain_port)
        env["ROBBER_BRAIN"] = self.brain_url
        env["GATEWAY_ADDR"] = f"{self.config.gateway_host}:{self.config.gateway_port}"
        env["GATEWAY_IPC"] = self.config.ipc
        env["ROBBER_ROOT"] = str(self.config.root)
        env["DASHBOARD_HOST"] = self.config.dashboard_host
        env["DASHBOARD_PORT"] = str(self.config.dashboard_port)
        pythonpath = str(self.install)
        prev = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = pythonpath if not prev else pythonpath + os.pathsep + prev
        logs = self.config.root / ".robber" / "logs"
        logs.mkdir(parents=True, exist_ok=True)

        gw = gateway_binary(self.install)
        if gw.is_file():
            self.procs.append(
                spawn(
                    [
                        str(gw),
                        "-addr",
                        f"{self.config.gateway_host}:{self.config.gateway_port}",
                        "-root",
                        str(self.config.root),
                        "-brain",
                        self.brain_url,
                        "-ssot",
                        str(self.config.ssot),
                        "-ipc",
                        self.config.ipc,
                    ],
                    cwd=self.install / "gateway",
                    env=env,
                    flags=self._pg,
                    log_path=logs / "gateway.stderr.log",
                    log_handles=self._logs,
                )
            )
            _log(f"gateway pid={self.procs[-1].pid} {self.gateway_url}")
        else:
            _log("gateway binary missing")

        self.procs.append(
            spawn(
                [self.config.python, str(self.install / "server.py")],
                cwd=self.config.root,
                env=env,
                flags=self._pg,
                log_path=logs / "brain.stderr.log",
                log_handles=self._logs,
            )
        )
        _log(f"brain pid={self.procs[-1].pid} {self.brain_url}")

        self.guard = MemoryGuard(
            self.pids,
            ssot_path=self.config.ssot,
            brain_url=self.brain_url,
        )
        self.guard.start()
        try:
            wait_http(f"{self.brain_url}/api/system/health", timeout=40.0)
        except TimeoutError:
            _log(tail_log(logs / "brain.stderr.log"))
            raise
        if gw.is_file():
            try:
                wait_http(f"{self.gateway_url}/health", timeout=15.0)
            except TimeoutError:
                _log(tail_log(logs / "gateway.stderr.log"))
                raise
        _log(
            "stack ready rss={rss} combined_cap={cap}".format(
                rss=sum(rss_bytes(pid) for pid in self.pids()),
                cap=self.guard.limit,
            )
        )

    def run_tui(self) -> int:
        ui_entry = self.install / "ui" / "src" / "index.tsx"
        tsx = self.install / "ui" / "node_modules" / ".bin" / ("tsx.cmd" if sys.platform == "win32" else "tsx")
        if not ui_entry.is_file():
            _log("ui/src/index.tsx missing")
            return 1
        cmd: list[str]
        if tsx.is_file():
            cmd = [str(tsx), str(ui_entry), "--gateway", self.gateway_url, "--root", str(self.config.root)]
        else:
            cmd = [
                "npx",
                "--yes",
                "tsx",
                str(ui_entry),
                "--gateway",
                self.gateway_url,
                "--root",
                str(self.config.root),
            ]
        env = os.environ.copy()
        env["ROBBER_WORKSPACE"] = str(self.config.root)
        proc = spawn(
            cmd,
            cwd=self.install / "ui",
            env=env,
            flags=0,
            inherit_io=True,
        )
        self.procs.append(proc)
        _log(f"tui pid={proc.pid}")
        return int(proc.wait() or 0)

    def wait(self, timeout: float = 0.0) -> None:
        deadline = time.time() + timeout if timeout > 0 else 0.0
        while not self._stop.is_set():
            if timeout > 0 and time.time() >= deadline:
                return
            time.sleep(0.2)

    def stop(self) -> None:
        self._stop.set()
        if self.guard is not None:
            self.guard.stop()
            self.guard = None
        for proc in reversed(self.procs):
            terminate(proc)
        self.procs.clear()
        if self._dashboard_proc is not None:
            terminate(self._dashboard_proc)
            self._dashboard_proc = None
        for handle in self._logs:
            try:
                handle.close()
            except Exception:
                pass
        self._logs.clear()
        db = self.ssot
        if db is not None:
            try:
                from robber.engine.recovery import SnapshotRecoveryEngine

                SnapshotRecoveryEngine(self.config.root).mark_clean(db)
            except Exception:
                pass
            try:
                db.wal_checkpoint("TRUNCATE")
            except Exception:
                pass
            db.close()
            self.ssot = None
        clear_stale_locks(self.config.root / ".robber" / "locks")
        _log("stack stopped")


def gateway_binary(root: Path) -> Path:
    name = "gateway.exe" if sys.platform == "win32" else "gateway"
    return root / "gateway" / name


def rust_cdylib(root: Path) -> Path | None:
    from robber.indexer_bridge import cdylib_candidates

    for path in cdylib_candidates(root):
        if path.is_file():
            return path
    return None


def rust_build_env(install: Path) -> dict[str, str]:
    """MinGW cc1 for rusqlite/tree-sitter; keep the existing cargo target dir."""
    env = os.environ.copy()
    w64 = install / "indexer" / ".tools" / "w64devkit" / "bin"
    gcc = w64 / ("gcc.exe" if sys.platform == "win32" else "gcc")
    if gcc.is_file():
        env["CC"] = str(gcc)
        gxx = w64 / ("g++.exe" if sys.platform == "win32" else "g++")
        if gxx.is_file():
            env["CXX"] = str(gxx)
        env["PATH"] = str(w64) + os.pathsep + env.get("PATH", "")
    return env


def find_go() -> str | None:
    found = shutil.which("go")
    if found:
        return found
    extra = Path(r"D:\Program Files\Go\bin\go.exe")
    return str(extra) if extra.is_file() else None


def find_cargo() -> str | None:
    found = shutil.which("cargo")
    if found:
        return found
    home = Path.home() / ".cargo" / "bin" / ("cargo.exe" if sys.platform == "win32" else "cargo")
    return str(home) if home.is_file() else None


def run_ok(argv: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(err[-1] if err else f"command failed: {argv[0]}")


def spawn(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    flags: int,
    inherit_io: bool = False,
    log_path: Path | None = None,
    log_handles: list[Any] | None = None,
) -> subprocess.Popen[Any]:
    stdin: Any = None if inherit_io else subprocess.DEVNULL
    stdout: Any = None
    stderr: Any = None
    if inherit_io:
        stdout = None
        stderr = None
    elif log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "ab", buffering=0)
        if log_handles is not None:
            log_handles.append(handle)
        stdout = handle
        stderr = handle
        stdin = subprocess.DEVNULL
    else:
        stdout = subprocess.DEVNULL
        stderr = subprocess.DEVNULL
        stdin = subprocess.DEVNULL
    return subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        creationflags=flags,
    )


def terminate(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=4)
        return
    except Exception:
        pass
    if sys.platform == "win32" and proc.pid:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        return
    try:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        pass


def tail_log(path: Path, lines: int = 20) -> str:
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    body = text.strip().splitlines()
    if not body:
        return ""
    return "\n".join(body[-lines:])


def wait_http(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if 200 <= int(resp.status) < 500:
                    return
                last = f"status {resp.status}"
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return
            last = str(exc)
        except Exception as exc:
            last = str(exc)
        time.sleep(0.25)
    raise TimeoutError(f"timeout waiting for {url} ({last})")


def clear_stale_locks(lock_dir: Path) -> int:
    if not lock_dir.is_dir():
        return 0
    n = 0
    for path in lock_dir.glob("*.lock"):
        try:
            path.unlink()
            n += 1
        except OSError:
            continue
    return n


def parse_args(argv: list[str] | None = None) -> LaunchConfig:
    parser = argparse.ArgumentParser(description="Launch Agent Robber polyglot stack")
    parser.add_argument("--root", default="", help="workspace root")
    parser.add_argument("--ssot", default="", help="ssot.db path")
    parser.add_argument("--brain-port", type=int, default=int(os.environ.get("ROBBER_PORT", "8787")))
    parser.add_argument("--gateway-port", type=int, default=8790)
    parser.add_argument("--ipc", default=os.environ.get("GATEWAY_IPC", "127.0.0.1:8791"))
    parser.add_argument("--dashboard-port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "3000")))
    parser.add_argument("--dashboard-host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--headless", action="store_true", help="do not start the TS TUI")
    parser.add_argument("--timeout", type=float, default=0.0, help="headless idle seconds then stop")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else ROOT
    ssot = Path(args.ssot).resolve() if args.ssot else default_ssot_path(root)
    return LaunchConfig(
        root=root,
        ssot=ssot,
        install=ROOT,
        brain_port=int(args.brain_port),
        gateway_port=int(args.gateway_port),
        ipc=str(args.ipc),
        dashboard_host=str(args.dashboard_host),
        dashboard_port=int(args.dashboard_port),
        build=not args.skip_build,
        headless=bool(args.headless),
        timeout=float(args.timeout),
    )


def main(argv: list[str] | None = None) -> int:
    from robber.vault import load_dotenv

    load_dotenv()
    config = parse_args(argv)
    stack = ProcessStack(config)

    def _on_stop(_signum: int, _frame: Any) -> None:
        stack._stop.set()

    signal.signal(signal.SIGINT, _on_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_stop)

    try:
        global _active_stack
        _active_stack = stack
        stack.start()
        if config.headless:
            stack.wait(config.timeout)
            return 0
        return stack.run_tui()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        _log(str(exc))
        return 1
    finally:
        _active_stack = None
        stack.stop()


if __name__ == "__main__":
    raise SystemExit(main())
