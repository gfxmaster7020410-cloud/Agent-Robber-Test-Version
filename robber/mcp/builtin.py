"""In-process MCP adapters — jailed filesystem, SSOT search, read-only git."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Awaitable

from robber.jail import JailError, is_skipped_dir, resolve_inside, workspace_root
from robber.ssot import SECRET_NAMES, get_index
from robber.terminal import TerminalSupervisor
from robber.tools.workspace import list_workspace, read_workspace_file

MAX_SEARCH_HITS = 20
MAX_SEARCH_BYTES = 64_000

ToolFn = Callable[[dict[str, Any], TerminalSupervisor | None], Awaitable[str]]


def _as_path(arguments: dict[str, Any], key: str = "path") -> str | None:
    raw = arguments.get(key)
    if raw is None or raw == "":
        return None
    return str(raw)


async def _list_dir(arguments: dict[str, Any], _supervisor: TerminalSupervisor | None) -> str:
    root = workspace_root()
    relative = _as_path(arguments)
    target = resolve_inside(root, relative)
    if not target.is_dir():
        raise JailError(f"not a directory: {relative or '.'}")
    return list_workspace(target)


async def _read_file(arguments: dict[str, Any], _supervisor: TerminalSupervisor | None) -> str:
    relative = _as_path(arguments)
    if not relative:
        raise JailError("read_file requires path")
    return read_workspace_file(workspace_root(), relative)


async def _search_files(arguments: dict[str, Any], _supervisor: TerminalSupervisor | None) -> str:
    query = str(arguments.get("query") or arguments.get("q") or "").strip()
    if not query:
        raise JailError("search_files requires query")
    root = workspace_root()
    start = resolve_inside(root, _as_path(arguments))
    if start.is_file():
        start = start.parent
    needle = query.lower()
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = [name for name in dirnames if not is_skipped_dir(name)]
        for name in filenames:
            if name in SECRET_NAMES:
                continue
            path = Path(dirpath) / name
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            matched = needle in name.lower()
            if not matched:
                try:
                    if path.stat().st_size > MAX_SEARCH_BYTES:
                        continue
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                matched = needle in text.lower()
            if matched:
                hits.append(rel)
            if len(hits) >= MAX_SEARCH_HITS:
                break
        if len(hits) >= MAX_SEARCH_HITS:
            break
    header = f"search_files {query!r} ({len(hits)} hits, jailed)"
    body = "\n".join(hits) if hits else "(no matches)"
    return f"{header}\n{body}"


async def _ssot_search(arguments: dict[str, Any], _supervisor: TerminalSupervisor | None) -> str:
    query = str(arguments.get("query") or arguments.get("q") or "").strip()
    if not query:
        raise JailError("search requires query")
    index = get_index()
    return index.format_hits(index.search(query))


async def _git_status(_arguments: dict[str, Any], supervisor: TerminalSupervisor | None) -> str:
    if supervisor is None:
        raise JailError("git supervisor is not bound")
    result = await supervisor.run(["git", "status", "--short", "--branch"], timeout=12)
    return result.output


async def _git_diff(_arguments: dict[str, Any], supervisor: TerminalSupervisor | None) -> str:
    if supervisor is None:
        raise JailError("git supervisor is not bound")
    result = await supervisor.run(["git", "diff", "--stat"], timeout=12)
    return result.output


async def _git_log(_arguments: dict[str, Any], supervisor: TerminalSupervisor | None) -> str:
    if supervisor is None:
        raise JailError("git supervisor is not bound")
    result = await supervisor.run(["git", "log", "-8", "--oneline"], timeout=12)
    return result.output


BUILTIN_TOOLS: dict[str, dict[str, dict[str, Any]]] = {
    "jailed-fs": {
        "list_dir": {
            "description": "List a directory inside the jailed workspace.",
            "handler": _list_dir,
        },
        "read_file": {
            "description": "Read a text file inside the jailed workspace.",
            "handler": _read_file,
        },
        "search_files": {
            "description": "Substring-search filenames and file bodies inside the jail.",
            "handler": _search_files,
        },
    },
    "ssot-memory": {
        "search": {
            "description": "Search the local hash-json SSOT index.",
            "handler": _ssot_search,
        },
    },
    "git-readonly": {
        "status": {
            "description": "git status --short --branch (read-only).",
            "handler": _git_status,
        },
        "diff": {
            "description": "git diff --stat (read-only).",
            "handler": _git_diff,
        },
        "log": {
            "description": "git log -8 --oneline (read-only).",
            "handler": _git_log,
        },
    },
}


def list_builtin_tools(server_id: str) -> list[dict[str, str]]:
    tools = BUILTIN_TOOLS.get(server_id) or {}
    return [
        {"name": name, "description": str(meta["description"])}
        for name, meta in tools.items()
    ]


async def call_builtin(
    server_id: str,
    tool: str,
    arguments: dict[str, Any] | None,
    supervisor: TerminalSupervisor | None,
) -> str:
    tools = BUILTIN_TOOLS.get(server_id)
    if tools is None:
        raise JailError(f"unknown builtin MCP server '{server_id}'")
    meta = tools.get(tool)
    if meta is None:
        raise JailError(f"unknown tool '{tool}' on {server_id}")
    handler: ToolFn = meta["handler"]
    return await handler(arguments or {}, supervisor)
