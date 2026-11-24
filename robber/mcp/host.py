"""MCP host — loads the local catalog, starts enabled servers, aggregates tools."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from robber.config import MCP_PING_TIMEOUT, MCP_WATCHDOG_SECONDS
from robber.jail import JailError
from robber.mcp.builtin import call_builtin, list_builtin_tools
from robber.mcp.catalog import (
    add_server,
    get_server,
    load_catalog,
    remove_server,
    set_enabled,
)
from robber.mcp.stdio import StdioSession
from robber.terminal import TerminalSupervisor

logger = logging.getLogger(__name__)

QUALIFIED_RE = re.compile(r"^mcp__([a-z0-9][a-z0-9._-]*)__([A-Za-z0-9._-]+)$")
CALL_RE = re.compile(
    r"^/?mcp\s+call\s+(\S+)\s+(\S+)\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
LIST_RE = re.compile(r"^/?mcp(?:\s+list)?\s*$", re.IGNORECASE)

@dataclass(frozen=True)
class McpRequest:
    server: str
    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class McpCallResult:
    ok: bool
    text: str
    server: str
    tool: str


class McpHost:
    def __init__(self) -> None:
        self._supervisor: TerminalSupervisor | None = None
        self._stdio: dict[str, StdioSession] = {}
        self._started = False
        self._watchdog: asyncio.Task[None] | None = None
        self._watchdog_restarts = 0

    @property
    def started(self) -> bool:
        return self._started

    def bind(self, supervisor: TerminalSupervisor) -> None:
        self._supervisor = supervisor

    async def start(self, supervisor: TerminalSupervisor | None = None) -> None:
        if supervisor is not None:
            self._supervisor = supervisor
        await self.stop()
        self._started = True
        for row in load_catalog():
            if not row.get("enabled"):
                continue
            try:
                await self._ensure_server(row["id"])
            except Exception as exc:
                session = self._stdio.get(row["id"])
                if session is not None:
                    session.status = "error"
                    session.error = str(exc)
        self._watchdog = asyncio.create_task(self._watchdog_loop())

    async def stop(self) -> None:
        self._started = False
        task = self._watchdog
        self._watchdog = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        sessions = list(self._stdio.values())
        self._stdio.clear()
        for session in sessions:
            await session.stop()

    async def enable(self, server_id: str) -> dict[str, Any]:
        row = set_enabled(server_id, True)
        await self._ensure_server(row["id"])
        return self.snapshot()

    async def disable(self, server_id: str) -> dict[str, Any]:
        row = set_enabled(server_id, False)
        await self._stop_server(row["id"])
        return self.snapshot()

    async def add(self, **kwargs: Any) -> dict[str, Any]:
        row = add_server(**kwargs)
        if row.get("enabled"):
            await self._ensure_server(row["id"])
        return self.snapshot()

    async def remove(self, server_id: str) -> dict[str, Any]:
        found = remove_server(server_id)
        await self._stop_server(found["id"])
        return self.snapshot()

    async def call(
        self,
        server_id: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
    ) -> McpCallResult:
        server = (server_id or "").strip().lower()
        name = (tool or "").strip()
        if not server or not name:
            return McpCallResult(False, "server and tool are required", server, name)
        row = get_server(server)
        if row is None:
            return McpCallResult(False, f"unknown MCP server '{server}'", server, name)
        if not row.get("enabled"):
            return McpCallResult(False, f"MCP server '{server}' is disabled", server, name)
        try:
            await self._ensure_server(server)
            if row.get("kind") == "builtin":
                text = await call_builtin(server, name, arguments or {}, self._supervisor)
                return McpCallResult(True, text, server, name)
            session = self._stdio.get(server)
            if session is None or session.status != "ready":
                detail = session.error if session else "not running"
                return McpCallResult(False, f"stdio MCP '{server}' is not ready: {detail}", server, name)
            text = await session.call(name, arguments or {})
            return McpCallResult(True, text, server, name)
        except (JailError, ValueError, RuntimeError, TimeoutError, FileNotFoundError, OSError) as exc:
            return McpCallResult(False, str(exc), server, name)

    def snapshot(self) -> dict[str, Any]:
        servers_out: list[dict[str, Any]] = []
        tools_out: list[dict[str, Any]] = []
        enabled = 0
        for row in load_catalog():
            server_id = row["id"]
            kind = row["kind"]
            status = "stopped"
            error = ""
            tools = list_builtin_tools(server_id) if kind == "builtin" else []
            session = self._stdio.get(server_id)
            if kind == "builtin":
                status = "ready" if row.get("enabled") else "stopped"
            elif session is not None:
                status = session.status
                error = session.error
                tools = session.tools
            if row.get("enabled"):
                enabled += 1
            servers_out.append(
                {
                    "id": server_id,
                    "label": row["label"],
                    "kind": kind,
                    "description": row.get("description") or "",
                    "enabled": bool(row.get("enabled")),
                    "builtin": bool(row.get("builtin")),
                    "command": row.get("command") or "",
                    "args": list(row.get("args") or []),
                    "env_keys": list(row.get("env_keys") or []),
                    "status": status,
                    "error": error,
                    "tool_count": len(tools),
                }
            )
            if row.get("enabled"):
                for tool in tools:
                    tools_out.append(
                        {
                            "server": server_id,
                            "name": tool["name"],
                            "qualified": f"mcp__{server_id}__{tool['name']}",
                            "description": tool.get("description") or "",
                        }
                    )
        return {
            "servers": servers_out,
            "tools": tools_out,
            "enabled": enabled,
            "started": self._started,
            "watchdog": {
                "running": self._watchdog is not None and not self._watchdog.done(),
                "interval": MCP_WATCHDOG_SECONDS,
                "restarts": self._watchdog_restarts,
            },
        }

    def prompt_catalog(self) -> str:
        snap = self.snapshot()
        tools = snap.get("tools") or []
        if not tools:
            return "MCP: no enabled tools. Use /mcp to list the local marketplace."
        names = ", ".join(str(item["qualified"]) for item in tools[:24])
        extra = "" if len(tools) <= 24 else f" (+{len(tools) - 24} more)"
        return f"MCP tools ({len(tools)}): {names}{extra}"

    def format_catalog(self) -> str:
        snap = self.snapshot()
        lines = [
            f"MCP marketplace · {snap['enabled']} enabled · {len(snap['tools'])} tools",
            "Local catalog only — no unsigned downloads. Secrets stay in .env.",
        ]
        for row in snap["servers"]:
            mark = "on" if row["enabled"] else "off"
            lines.append(
                f"  [{mark}] {row['id']}  {row['label']}  {row['kind']}  "
                f"{row['status']}  tools={row['tool_count']}"
            )
            if row.get("error"):
                lines.append(f"      error: {row['error']}")
        if snap["tools"]:
            lines.append("qualified names:")
            for tool in snap["tools"]:
                lines.append(f"  {tool['qualified']}  {tool['description']}")
        return "\n".join(lines)

    async def _ensure_server(self, server_id: str) -> None:
        row = get_server(server_id)
        if row is None:
            raise ValueError(f"unknown MCP server '{server_id}'")
        if row.get("kind") == "builtin":
            return
        session = self._stdio.get(server_id)
        if session is not None and session.status == "ready" and session.alive:
            return
        if session is not None:
            await session.stop()
        session = StdioSession(row)
        self._stdio[server_id] = session
        await session.start()

    async def _stop_server(self, server_id: str) -> None:
        session = self._stdio.pop(server_id, None)
        if session is not None:
            await session.stop()

    async def _watchdog_loop(self) -> None:
        try:
            while self._started:
                await asyncio.sleep(MCP_WATCHDOG_SECONDS)
                await self.watchdog_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("mcp watchdog loop stopped", exc_info=True)

    async def watchdog_tick(self) -> int:
        """Ping enabled stdio servers and restart the ones that are down. Returns restart count."""
        restarted = 0
        if not self._started:
            return 0
        for row in load_catalog():
            if not row.get("enabled") or row.get("kind") == "builtin":
                continue
            server_id = str(row.get("id") or "")
            if not server_id:
                continue
            session = self._stdio.get(server_id)
            healthy = (
                session is not None
                and session.status == "ready"
                and session.alive
                and await session.ping(MCP_PING_TIMEOUT)
            )
            if healthy:
                continue
            try:
                await self._ensure_server(server_id)
                restarted += 1
                self._watchdog_restarts += 1
                logger.info("mcp watchdog restarted stdio server %s", server_id)
            except Exception as exc:
                logger.warning("mcp watchdog failed to restart %s (%s)", server_id, type(exc).__name__)
        return restarted


_HOST: McpHost | None = None


def get_mcp_host() -> McpHost:
    global _HOST
    if _HOST is None:
        _HOST = McpHost()
    return _HOST


def bind_mcp(host: McpHost) -> McpHost:
    global _HOST
    _HOST = host
    return host


def parse_mcp_request(query: str) -> McpRequest | None:
    text = query.strip()
    if not text:
        return None
    if LIST_RE.match(text) or text.lower() in {"mcp", "list mcp", "show mcp"}:
        return None
    match = CALL_RE.match(text)
    if match:
        return McpRequest(match.group(1).lower(), match.group(2), _parse_args(match.group(3)))
    token = QUALIFIED_RE.search(text)
    if token:
        rest = text[token.end() :].strip()
        return McpRequest(token.group(1), token.group(2), _parse_args(rest))
    return None


def _parse_args(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text, "query": text, "path": text}
    return value if isinstance(value, dict) else {"value": value}
