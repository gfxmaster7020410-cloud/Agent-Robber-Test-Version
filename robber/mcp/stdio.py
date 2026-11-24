"""MCP stdio client — LSP-style Content-Length JSON-RPC, jailed spawn."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from robber.jail import workspace_root
from robber.mcp.catalog import ALLOWED_BINS, ALLOWED_MODULES, validate_stdio
from robber.terminal import CREATE_NEW_PROCESS_GROUP, sanitized_env

INIT_TIMEOUT = 15.0
CALL_TIMEOUT = 20.0
STDERR_KEEP = 2000


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def encode_frame(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        decoded = line.decode("ascii", errors="replace")
        key, _, value = decoded.partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length") or "0")
    if length <= 0:
        return None
    body = await reader.readexactly(length)
    payload = json.loads(body.decode("utf-8"))
    return payload if isinstance(payload, dict) else None


def resolve_argv(row: dict[str, Any]) -> list[str]:
    validate_stdio(row)
    binary = Path(str(row.get("command") or "")).name.lower()
    args = [str(item) for item in row.get("args") or []]
    if binary not in ALLOWED_BINS:
        raise ValueError(f"MCP command not allowlisted: {row.get('command')}")
    if binary in {"python", "python.exe", "py", "py.exe"}:
        executable = sys.executable
        if args[:2] == ["-m", "robber.mcp.echo_server"]:
            if args[1] not in ALLOWED_MODULES:
                raise ValueError("MCP module is not allowlisted")
            return [executable, "-u", "-m", "robber.mcp.echo_server"]
        script = args[0]
        from robber.jail import resolve_inside

        target = resolve_inside(workspace_root(), script)
        return [executable, "-u", str(target)]
    which = _which_node()
    script = args[0]
    from robber.jail import resolve_inside

    target = resolve_inside(workspace_root(), script)
    return [which, str(target)]


def _which_node() -> str:
    for name in ("node", "node.exe"):
        path = _look_path(name)
        if path:
            return path
    raise ValueError("node is not on PATH")


def _look_path(name: str) -> str | None:
    from shutil import which

    found = which(name)
    return found


def build_env(row: dict[str, Any]) -> dict[str, str]:
    env = sanitized_env()
    env["PYTHONUNBUFFERED"] = "1"
    args = [str(item) for item in row.get("args") or []]
    if args[:2] == ["-m", "robber.mcp.echo_server"]:
        root = str(package_root())
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = root if not existing else root + os.pathsep + existing
    for key in row.get("env_keys") or []:
        name = str(key).strip()
        if not name:
            continue
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def _text_from_result(result: dict[str, Any]) -> str:
    if "error" in result:
        err = result["error"]
        if isinstance(err, dict):
            return str(err.get("message") or err)
        return str(err)
    payload = result.get("result")
    if isinstance(payload, dict):
        chunks = payload.get("content")
        if isinstance(chunks, list):
            texts: list[str] = []
            for item in chunks:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(str(item.get("text") or ""))
            if texts:
                return "\n".join(texts)
        tools = payload.get("tools")
        if isinstance(tools, list):
            return json.dumps(payload)
        return json.dumps(payload)
    return json.dumps(result)


class StdioSession:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.proc: asyncio.subprocess.Process | None = None
        self.tools: list[dict[str, str]] = []
        self.status = "stopped"
        self.error = ""
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._stderr_tail = ""
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        argv = resolve_argv(self.row)
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": str(workspace_root()),
            "env": build_env(self.row),
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP
        self.proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
        self.status = "starting"
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr = asyncio.create_task(self._drain_stderr())
        try:
            await self.request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "agent-robber", "version": "1.0.0"},
                },
                timeout=INIT_TIMEOUT,
            )
            await self.notify("notifications/initialized")
            listed = await self.request("tools/list", {}, timeout=INIT_TIMEOUT)
            payload = listed.get("result") if isinstance(listed.get("result"), dict) else listed
            raw_tools = payload.get("tools") if isinstance(payload, dict) else []
            tools: list[dict[str, str]] = []
            if isinstance(raw_tools, list):
                for item in raw_tools:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip()
                    if not name:
                        continue
                    tools.append(
                        {
                            "name": name,
                            "description": str(item.get("description") or ""),
                        }
                    )
            self.tools = tools
            self.status = "ready"
            self.error = ""
        except Exception as exc:
            self.status = "error"
            self.error = str(exc)
            await self.stop()
            raise

    async def stop(self) -> None:
        proc = self.proc
        self.proc = None
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
        self._pending.clear()
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self._stderr is not None:
            self._stderr.cancel()
            self._stderr = None
        if proc is None:
            self.status = "stopped"
            return
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        self.status = "stopped"

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def ping(self, timeout: float = 5.0) -> bool:
        if not self.alive or self.status != "ready":
            return False
        try:
            await self.request("ping", {}, timeout=timeout)
            return True
        except Exception:
            return False

    async def call(self, tool: str, arguments: dict[str, Any] | None) -> str:
        result = await self.request(
            "tools/call",
            {"name": tool, "arguments": arguments or {}},
            timeout=CALL_TIMEOUT,
        )
        if "error" in result:
            raise RuntimeError(_text_from_result(result))
        payload = result.get("result") if isinstance(result.get("result"), dict) else {}
        if isinstance(payload, dict) and payload.get("isError"):
            raise RuntimeError(_text_from_result(result))
        return _text_from_result(result)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("stdio MCP server is not running")
        frame = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        proc.stdin.write(encode_frame(frame))
        await proc.stdin.drain()

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = CALL_TIMEOUT,
    ) -> dict[str, Any]:
        async with self._lock:
            proc = self.proc
            if proc is None or proc.stdin is None:
                raise RuntimeError("stdio MCP server is not running")
            self._next_id += 1
            msg_id = self._next_id
            loop = asyncio.get_running_loop()
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending[msg_id] = future
            frame: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
            if params is not None:
                frame["params"] = params
            proc.stdin.write(encode_frame(frame))
            await proc.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(msg_id, None)
            extra = f" ({self._stderr_tail})" if self._stderr_tail else ""
            raise TimeoutError(f"MCP {method} timed out{extra}") from exc

    async def _read_loop(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                message = await read_frame(proc.stdout)
                if message is None:
                    break
                msg_id = message.get("id")
                if isinstance(msg_id, int) and msg_id in self._pending:
                    future = self._pending.pop(msg_id)
                    if not future.done():
                        future.set_result(message)
        except (asyncio.CancelledError, asyncio.IncompleteReadError, json.JSONDecodeError):
            return
        finally:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError("stdio MCP server closed"))
            self._pending.clear()

    async def _drain_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace")
                self._stderr_tail = (self._stderr_tail + text)[-STDERR_KEEP:]
        except asyncio.CancelledError:
            return
