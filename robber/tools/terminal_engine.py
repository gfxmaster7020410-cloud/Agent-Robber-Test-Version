"""Stateful interactive shell (Unix PTY / Windows ConPTY or winpty, pipe fallback)."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robber.ansi import strip_ansi
from robber.jail import workspace_root
from robber.tools.command_guard import CommandBlockedError, sanitize_command

DEFAULT_TIMEOUT = 60.0
RING_SIZE = 2000
OUTPUT_CAP = 120_000
CHUNK = 4096
PROMPT_MARK = "__RPS__"
PROMPT_TOKEN = f"{PROMPT_MARK}>" if os.name == "nt" else PROMPT_MARK
ANSI_EXTRA = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[PX^_][\s\S]*?\x1b\\"
    r"|\x1b[@-Z\\-_]"
    r"|\r"
)

OnChunk = Callable[[str, str], Awaitable[None] | None]

_ENGINE: TerminalEngine | None = None


@dataclass
class TerminalResult:
    command: str
    output: str
    raw: str
    exit_code: int
    timed_out: bool
    pid: int | None
    backend: str


@dataclass
class DaemonJob:
    pid: int
    argv: tuple[str, ...]
    started: float
    running: bool = True


class TerminalEngine:
    """One persistent shell per workspace. Does not spawn on import."""

    def __init__(self, workspace: Path | None = None, *, on_chunk: OnChunk | None = None) -> None:
        self.workspace = (workspace or workspace_root()).resolve()
        self.on_chunk = on_chunk
        self.ring: deque[str] = deque(maxlen=RING_SIZE)
        self.jobs: dict[int, DaemonJob] = {}
        self.backend = ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._master: int | None = None
        self._winpty: Any = None
        self._buf = ""
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._alive = False
        self._cond = threading.Condition(self._lock)

    @property
    def pid(self) -> int | None:
        if self._winpty is not None:
            return int(getattr(self._winpty, "pid", 0) or 0) or None
        if self._proc is not None:
            return self._proc.pid
        return None

    def strip_ansi_codes(self, raw_output: str) -> str:
        return strip_ansi_codes(raw_output)

    def write_stdin(self, input_str: str) -> None:
        data = input_str if input_str.endswith("\n") else f"{input_str}\n"
        self._write(data.encode("utf-8", errors="replace"))

    async def run(
        self,
        command: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        interactive: bool = False,
        workspace: Path | None = None,
        auto_approve: bool = False,
    ) -> TerminalResult:
        from robber.tools.command_policy import (
            TIER_2_USER_CONFIRMATION,
            TIER_3_BLOCKED,
            get_command_policy_engine,
        )

        ws = workspace or self.workspace
        policy = get_command_policy_engine(ws)
        evaluation = policy.evaluate(command, workspace=ws)

        if evaluation.tier == TIER_3_BLOCKED:
            raise CommandBlockedError(evaluation.reason or "Command blocked by security policy (Tier 3)")

        if evaluation.tier == TIER_2_USER_CONFIRMATION and not auto_approve:
            if not (os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("ROBBER_AUTO_APPROVE_ALL")):
                approved = await policy.request_confirmation(command, tier=2, reason=evaluation.reason)
                if not approved:
                    raise CommandBlockedError(f"Command confirmation denied or timed out: {command}")

        check = sanitize_command(command, workspace=ws)
        if not check.allowed:
            raise CommandBlockedError(check.reason or "command blocked")
        await asyncio.to_thread(self.ensure_shell)
        if interactive or _looks_interactive(check.command):
            return await asyncio.to_thread(self._run_interactive, check.command, timeout)
        return await asyncio.to_thread(self._run_marked, check.command, timeout)

    def ensure_shell(self) -> None:
        if self.alive():
            return
        self.workspace.mkdir(parents=True, exist_ok=True)
        env = _session_env()
        if os.name != "nt":
            self._start_unix(env)
            return
        if self._start_winpty(env):
            return
        if self._start_conpty(env):
            return
        self._start_pipes(env)

    def alive(self) -> bool:
        if self._winpty is not None:
            return bool(getattr(self._winpty, "isalive", lambda: False)())
        if self._proc is None:
            return False
        return self._proc.poll() is None

    def spawn_daemon(self, argv: list[str], *, cwd: Path | None = None) -> DaemonJob:
        from robber.tools.command_policy import (
            TIER_3_BLOCKED,
            get_command_policy_engine,
        )

        workdir = cwd or self.workspace
        cmd_str = " ".join(str(x) for x in argv)
        policy = get_command_policy_engine(workdir)
        evaluation = policy.evaluate(cmd_str, workspace=workdir)
        if evaluation.tier == TIER_3_BLOCKED:
            raise CommandBlockedError(evaluation.reason or "Command blocked by security policy (Tier 3)")

        check = sanitize_command(cmd_str, workspace=workdir)
        if not check.allowed:
            raise CommandBlockedError(check.reason or "command blocked")
        kwargs: dict[str, Any] = {
            "cwd": str(workdir),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": _session_env(),
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
        assert proc.pid is not None
        job = DaemonJob(pid=proc.pid, argv=tuple(str(x) for x in argv), started=time.time())
        self.jobs[proc.pid] = job
        return job

    def kill_process_group(self, pid: int | None = None) -> bool:
        target = int(pid or 0) or self.pid
        if not target or target == os.getpid() or target <= 1:
            return False
        ok = False
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(target)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                    check=False,
                )
                ok = True
            except (OSError, subprocess.SubprocessError):
                ok = False
        else:
            try:
                os.killpg(target, signal.SIGTERM)
                ok = True
            except OSError:
                try:
                    os.kill(target, signal.SIGTERM)
                    ok = True
                except OSError:
                    ok = False
        job = self.jobs.get(target)
        if job is not None:
            job.running = False
        return ok

    def close(self) -> None:
        self._alive = False
        pty_obj = self._winpty
        self._winpty = None
        if pty_obj is not None:
            try:
                pty_obj.terminate(force=True)
            except Exception:
                pass
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        master = self._master
        self._master = None
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        for pid, job in list(self.jobs.items()):
            if job.running:
                self.kill_process_group(pid)
                job.running = False

    def _start_unix(self, env: dict[str, str]) -> None:
        import pty as pty_mod

        master, slave = pty_mod.openpty()
        try:
            import fcntl
            import struct
            import termios

            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        except Exception:
            pass
        shell = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
        self._proc = subprocess.Popen(
            [shell],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=str(self.workspace),
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        try:
            os.close(slave)
        except OSError:
            pass
        self._master = master
        self.backend = "pty"
        self._begin_reader()
        self._prime_unix()

    def _start_winpty(self, env: dict[str, str]) -> bool:
        try:
            from winpty import PtyProcess  # type: ignore
        except Exception:
            return False
        argv = _windows_shell()
        try:
            self._winpty = PtyProcess.spawn(argv, cwd=str(self.workspace), env=env, dimensions=(24, 80))
        except Exception:
            self._winpty = None
            return False
        self.backend = "winpty"
        self._begin_reader()
        self._prime_windows()
        return True

    def _start_conpty(self, env: dict[str, str]) -> bool:
        # Optional: pywinpty-style ConPTY via ctypes is skipped when winpty is absent.
        # Pipe-backed cmd.exe still keeps cwd/env across commands on Windows.
        del env
        return False

    def _start_pipes(self, env: dict[str, str]) -> None:
        argv = _windows_shell()
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "cwd": str(self.workspace),
            "env": env,
            "bufsize": 0,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        self._proc = subprocess.Popen(argv, **kwargs)
        self.backend = "conpty-pipes" if os.name == "nt" else "pipes"
        self._begin_reader()
        self._prime_windows()

    def _begin_reader(self) -> None:
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, name="robber-pty", daemon=True)
        self._reader.start()

    def _prime_unix(self) -> None:
        self._write(f"PS1='{PROMPT_MARK}'; PROMPT_COMMAND=; export PS1\n".encode("ascii"))
        self._wait_prompt(8.0)

    def _prime_windows(self) -> None:
        self._write(f"prompt {PROMPT_MARK}$G\r\n".encode("ascii"))
        self._wait_prompt(8.0)

    def _run_marked(self, command: str, timeout: float) -> TerminalResult:
        self._drain_buf()
        nonce = secrets.token_hex(4)
        sentinel = f"__RD_{nonce}__"
        line_end = "\r\n" if os.name == "nt" else "\n"
        if os.name == "nt":
            payload = f"({command}) & echo {sentinel}{line_end}"
        else:
            payload = f"{command}{line_end}echo {sentinel}{line_end}"
        self._write(payload.encode("utf-8", errors="replace"))
        raw, found = self._wait_token(sentinel, timeout)
        cleaned = _trim_prompt(raw.replace(sentinel, ""))
        plain = strip_ansi_codes(cleaned)
        self._record(plain)
        return TerminalResult(
            command=command,
            output=plain[:OUTPUT_CAP] or "(no output)",
            raw=cleaned[:OUTPUT_CAP],
            exit_code=-1 if not found else 0,
            timed_out=not found,
            pid=self.pid,
            backend=self.backend,
        )

    def _run_interactive(self, command: str, timeout: float) -> TerminalResult:
        self._drain_buf()
        line_end = "\r\n" if os.name == "nt" else "\n"
        self._write((command + line_end).encode("utf-8", errors="replace"))
        raw = self._collect(timeout)
        plain = strip_ansi_codes(raw)
        self._record(plain)
        return TerminalResult(
            command=command,
            output=plain[:OUTPUT_CAP] or "(no output)",
            raw=raw[:OUTPUT_CAP],
            exit_code=0,
            timed_out=False,
            pid=self.pid,
            backend=self.backend,
        )

    def _write(self, data: bytes) -> None:
        if self._winpty is not None:
            self._winpty.write(data.decode("utf-8", errors="replace"))
            return
        if self._master is not None:
            os.write(self._master, data)
            return
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("terminal session is not running")
        proc.stdin.write(data)
        proc.stdin.flush()

    def _read_loop(self) -> None:
        try:
            while self._alive:
                chunk = self._read_some()
                if chunk is None:
                    break
                if not chunk:
                    time.sleep(0.01)
                    continue
                text = chunk.decode("utf-8", errors="replace")
                with self._cond:
                    self._buf += text
                    if len(self._buf) > OUTPUT_CAP * 2:
                        self._buf = self._buf[-OUTPUT_CAP:]
                    self._cond.notify_all()
                cb = self.on_chunk
                if cb is not None:
                    try:
                        maybe = cb(strip_ansi_codes(text), text)
                        if asyncio.iscoroutine(maybe):
                            pass
                    except Exception:
                        pass
        except Exception:
            pass

    def _read_some(self) -> bytes | None:
        if self._winpty is not None:
            try:
                data = self._winpty.read(CHUNK)
            except EOFError:
                return None
            except Exception:
                return None
            if isinstance(data, bytes):
                return data
            return str(data or "").encode("utf-8", errors="replace")
        if self._master is not None:
            try:
                return os.read(self._master, CHUNK)
            except OSError:
                return None
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        try:
            return os.read(proc.stdout.fileno(), CHUNK)
        except OSError:
            return None

    def _wait_prompt(self, timeout: float) -> tuple[str, bool]:
        deadline = time.time() + max(0.2, timeout)
        token = PROMPT_TOKEN
        while time.time() < deadline:
            with self._cond:
                if token in self._buf:
                    idx = self._buf.rfind(token)
                    collected = self._buf[:idx]
                    self._buf = self._buf[idx + len(token) :]
                    return collected, True
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=min(0.2, remaining))
        with self._cond:
            collected = self._buf
            self._buf = ""
        return collected, False

    def _wait_token(self, token: str, timeout: float) -> tuple[str, bool]:
        deadline = time.time() + max(0.2, timeout)
        while time.time() < deadline:
            with self._cond:
                if token in self._buf:
                    idx = self._buf.find(token)
                    collected = self._buf[:idx]
                    self._buf = self._buf[idx + len(token) :]
                    return collected, True
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=min(0.2, remaining))
        with self._cond:
            collected = self._buf
            self._buf = ""
        return collected, False

    def _collect(self, timeout: float) -> str:
        deadline = time.time() + max(0.2, timeout)
        while time.time() < deadline:
            with self._cond:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=min(0.15, remaining))
        with self._cond:
            collected = self._buf
            self._buf = ""
        return collected

    def _drain_buf(self) -> None:
        with self._cond:
            self._buf = ""

    def _record(self, text: str) -> None:
        for line in (text or "").splitlines():
            if line:
                self.ring.append(line[-500:])


def strip_ansi_codes(raw_output: str) -> str:
    text = strip_ansi(raw_output or "")
    return ANSI_EXTRA.sub("", text)


def get_terminal_engine(workspace: Path | None = None) -> TerminalEngine:
    global _ENGINE
    root = (workspace or workspace_root()).resolve()
    if _ENGINE is None or _ENGINE.workspace != root:
        if _ENGINE is not None:
            _ENGINE.close()
        _ENGINE = TerminalEngine(root)
    return _ENGINE


def bind_terminal_engine(engine: TerminalEngine | None) -> TerminalEngine:
    global _ENGINE
    if _ENGINE is not None and engine is not _ENGINE:
        _ENGINE.close()
    _ENGINE = engine if engine is not None else TerminalEngine()
    return _ENGINE


def peek_terminal_engine() -> TerminalEngine | None:
    return _ENGINE


def close_terminal_engine() -> None:
    global _ENGINE
    if _ENGINE is not None:
        _ENGINE.close()
        _ENGINE = None


def _session_env() -> dict[str, str]:
    env = os.environ.copy()
    env["TERM"] = env.get("TERM") or "xterm-256color"
    env["PROMPT"] = f"{PROMPT_MARK}$G" if os.name == "nt" else f"{PROMPT_MARK}"
    env["PS1"] = PROMPT_MARK
    return env


def _windows_shell() -> list[str]:
    comspec = os.environ.get("COMSPEC") or shutil.which("cmd.exe") or "cmd.exe"
    return [comspec, "/Q", "/K"]


def _looks_interactive(command: str) -> bool:
    return bool(re.search(r"\bpython(?:\.exe)?\s+-i\b|\bipython\b|\bpwsh\b\s*$", command, re.I))


def _trim_prompt(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        line_clean = line.replace(f"{PROMPT_MARK}>", "").replace(f"{PROMPT_MARK}$G", "").replace(PROMPT_MARK, "").strip()
        if line_clean:
            cleaned_lines.append(line_clean)
    return "\n".join(cleaned_lines).strip()
