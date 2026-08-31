"""Non-blocking subprocess runner with a ring buffer and PID tracker."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robber.jail import JailError, resolve_inside

SECRET_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "PRIVATE_KEY")
RING_SIZE = 5000
DEFAULT_TIMEOUT = 30.0

CREATE_NEW_PROCESS_GROUP = 0x00000200


def sanitized_env() -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(marker in upper for marker in SECRET_MARKERS):
            continue
        cleaned[key] = value
    return cleaned


@dataclass
class Job:
    pid: int
    argv: tuple[str, ...]
    started: float
    running: bool = True
    exit_code: int | None = None


@dataclass
class CommandResult:
    argv: tuple[str, ...]
    output: str
    exit_code: int
    timed_out: bool
    pid: int | None


@dataclass
class TerminalSupervisor:
    root: Path
    ring: deque[str] = field(default_factory=lambda: deque(maxlen=RING_SIZE))
    jobs: dict[int, Job] = field(default_factory=dict)

    def snapshot(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for job in self.jobs.values():
            rows.append(
                {
                    "pid": job.pid,
                    "cmd": " ".join(job.argv),
                    "running": job.running,
                    "exit_code": job.exit_code,
                }
            )
        return rows[-8:]

    def tail(self, lines: int = 40) -> str:
        if not self.ring:
            return "(terminal buffer empty)"
        return "\n".join(list(self.ring)[-lines:])

    def _record(self, line: str) -> None:
        text = line.rstrip("\n")
        if text:
            self.ring.append(text)

    async def kill(self, pid: int) -> bool:
        job = self.jobs.get(pid)
        if job is None:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        job.running = False
        return True

    async def start(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
    ) -> Job:
        """Launch a process without waiting — used by /run for dev servers."""
        if not argv:
            raise JailError("empty command")
        workdir = resolve_inside(self.root, cwd) if cwd else self.root
        kwargs: dict[str, Any] = {
            "cwd": str(workdir),
            "stdout": asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.STDOUT,
            "env": sanitized_env(),
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP
        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
        assert proc.pid is not None
        job = Job(pid=proc.pid, argv=tuple(argv), started=time.time())
        self.jobs[proc.pid] = job
        asyncio.create_task(self._reap(proc, job))
        return job

    async def _reap(self, proc: asyncio.subprocess.Process, job: Job) -> None:
        try:
            code = await proc.wait()
        except Exception:
            code = -1
        job.exit_code = code
        job.running = False

    async def kill_all(self) -> int:
        killed = 0
        for pid, job in list(self.jobs.items()):
            if job.running and await self.kill(pid):
                killed += 1
        return killed

    async def run(
        self,
        argv: list[str],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        cwd: str | None = None,
    ) -> CommandResult:
        if not argv:
            raise JailError("empty command")
        workdir = resolve_inside(self.root, cwd) if cwd else self.root
        kwargs: dict[str, Any] = {
            "cwd": str(workdir),
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
            "env": sanitized_env(),
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP
        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
        assert proc.pid is not None
        job = Job(pid=proc.pid, argv=tuple(argv), started=time.time())
        self.jobs[proc.pid] = job
        timed_out = False
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await self.kill(proc.pid)
            try:
                raw, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
            except (asyncio.TimeoutError, ProcessLookupError):
                raw = b""
            job.exit_code = -1
        else:
            job.exit_code = proc.returncode
        job.running = False
        text = raw.decode("utf-8", errors="replace") if raw else ""
        if timed_out:
            text = (text + "\n[watchdog] timed out, process killed").strip()
        for line in text.splitlines() or [text]:
            self._record(line)
        return CommandResult(
            argv=tuple(argv),
            output=text.strip() or "(no output)",
            exit_code=job.exit_code if job.exit_code is not None else -1,
            timed_out=timed_out,
            pid=proc.pid,
        )
