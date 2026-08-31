"""Jailed workspace tools: list, read, git, allowlisted command runner."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from robber.jail import JailError, is_skipped_dir, resolve_inside, workspace_root
from robber.patch import extract_diff
from robber.ssot import get_index
from robber.terminal import CommandResult, TerminalSupervisor
from robber.tools.patch_applier import JailedPatchApplicator

MAX_LIST = 24
MAX_READ_BYTES = 64_000
MAX_OUTPUT_CHARS = 2_400

FILE_TOKEN = re.compile(
    r"(?:[\w.-]+/)*[\w.-]+\.(?:py|ts|tsx|js|json|md|txt|css)",
    re.IGNORECASE,
)

ALLOWED_BINS = {
    "git",
    "python",
    "python.exe",
    "py",
    "node",
    "node.exe",
    "npm",
    "npm.cmd",
    "npx",
    "pip",
    "pytest",
}

ALLOWED_GIT = {"status", "diff", "log", "branch", "rev-parse", "show"}
BLOCKED_GIT = {"push", "reset", "clean", "rebase", "commit", "checkout", "stash"}


@dataclass(frozen=True)
class ToolResult:
    name: str
    output: str
    ok: bool


def clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n… (truncated)"


def select_tool(query: str) -> str:
    lowered = query.lower()
    if re.search(r"(^|/|\b)mcp\b", lowered) or "mcp__" in lowered:
        return "MCP Host"
    if extract_diff(query) or re.search(
        r"\b(apply patch|apply this (?:diff|patch)|jailed patch)\b", lowered
    ):
        return "Jailed Patch Apply"
    if re.search(r"\b(file|scan|director|folder|tree)\b", lowered):
        return "Local Directory Scan"
    if re.search(r"\b(git|branch|commit)\b", lowered) or re.search(
        r"\bgit\s+diff\b", lowered
    ):
        return "Git Status Probe"
    if FILE_TOKEN.search(query) and re.search(r"\b(read|open|show|cat)\b", lowered):
        return "Read File"
    if re.search(r"\b(memory|recall|remember|context)\b", lowered):
        return "SSOT Memory Query"
    if re.search(r"\b(test|run|build)\b", lowered):
        return "Command Runner"
    if FILE_TOKEN.search(query):
        return "Read File"
    return "Workspace Context Scan"


def list_workspace(root: Path) -> str:
    entries: list[str] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as exc:
        return f"cannot read workspace: {exc}"
    for child in children:
        if child.is_dir() and is_skipped_dir(child.name):
            continue
        mark = "/" if child.is_dir() else ""
        entries.append(f"{child.name}{mark}")
        if len(entries) >= MAX_LIST:
            entries.append("…")
            break
    header = f"jailed listing of {root} ({len(entries)} shown)"
    return header + "\n" + "\n".join(entries) if entries else header + "\n(empty)"


def read_workspace_file(root: Path, relative: str, allow_external: bool = False) -> str:
    try:
        # Try jailed resolution first
        target = resolve_inside(root, relative)
    except JailError:
        # Fallback to external path if allowed
        if allow_external:
            target = Path(relative).resolve()
            if not target.is_absolute() or not target.exists():
                raise JailError(f"not a valid external file: {relative}")
        else:
            raise JailError(f"path escapes workspace jail: {relative}")

    if not target.is_file():
        raise JailError(f"not a file: {relative}")
    
    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise JailError(f"file too large to read ({size} bytes)")
    
    text = target.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    body = "\n".join(lines[:80])
    return f"{target} ({min(len(lines), 80)} lines)\n{body}"


def pick_read_target(query: str) -> str | None:
    match = FILE_TOKEN.search(query)
    return match.group(0) if match else None


async def git_status(supervisor: TerminalSupervisor) -> CommandResult:
    return await supervisor.run(["git", "status", "--short", "--branch"], timeout=12)


async def run_allowlisted(
    supervisor: TerminalSupervisor, argv: list[str]
) -> CommandResult:
    if not argv:
        raise JailError("empty command")
    binary = Path(argv[0]).name.lower()
    if binary not in ALLOWED_BINS:
        raise JailError(f"binary not allowlisted: {argv[0]}")
    if binary == "git":
        sub = argv[1].lower() if len(argv) > 1 else "status"
        if sub in BLOCKED_GIT or sub.startswith("-"):
            raise JailError(f"git subcommand blocked: {sub}")
        if sub not in ALLOWED_GIT:
            raise JailError(f"git subcommand not allowed: {sub}")
    return await supervisor.run(argv, timeout=20)


def command_for_query(query: str) -> list[str]:
    lowered = query.lower()
    if "git diff" in lowered:
        return ["git", "diff", "--stat"]
    if "git log" in lowered:
        return ["git", "log", "-5", "--oneline"]
    if "git branch" in lowered:
        return ["git", "branch", "-v"]
    if re.search(r"\bgit\b", lowered):
        return ["git", "status", "--short", "--branch"]
    if re.search(r"\bpytest\b", lowered):
        return ["python", "--version"]
    if re.search(r"\bnpm\b", lowered):
        return ["npm", "-v"]
    if re.search(r"\bnode\b", lowered):
        return ["node", "--version"]
    return ["python", "--version"]


async def execute_tool(query: str, supervisor: TerminalSupervisor) -> ToolResult:
    root = workspace_root()
    supervisor.root = root
    name = select_tool(query)
    try:
        if name in {"Local Directory Scan", "Workspace Context Scan"}:
            return ToolResult(name, clip(list_workspace(root)), True)
        if name == "Read File":
            relative = pick_read_target(query)
            if relative is None:
                return ToolResult(name, clip(list_workspace(root)), True)
            return ToolResult(name, clip(read_workspace_file(root, relative)), True)
        if name == "Git Status Probe":
            result = await git_status(supervisor)
            return ToolResult(name, clip(result.output), result.exit_code == 0)
        if name == "Command Runner":
            argv = command_for_query(query)
            result = await run_allowlisted(supervisor, argv)
            if result.exit_code != 0 and not result.timed_out:
                from robber.engine.self_healing import get_healer

                healer = get_healer()
                if healer is not None:
                    repaired = await healer.heal_from_stderr(
                        result.output, task_name=" ".join(argv)
                    )
                    if repaired:
                        result = await run_allowlisted(supervisor, argv)
            return ToolResult(name, clip(result.output), result.exit_code == 0 and not result.timed_out)
        if name == "SSOT Memory Query":
            index = get_index()
            return ToolResult(name, clip(index.format_hits(index.search(query))), True)
        if name == "MCP Host":
            from robber.mcp.host import get_mcp_host, parse_mcp_request

            host = get_mcp_host()
            request = parse_mcp_request(query)
            if request is None:
                return ToolResult(name, clip(host.format_catalog()), True)
            result = await host.call(request.server, request.tool, request.arguments)
            return ToolResult(name, clip(result.text), result.ok)
        if name == "Jailed Patch Apply":
            diff = extract_diff(query)
            if not diff:
                return ToolResult(
                    name,
                    "no unified diff in the prompt — wrap it in a ```diff fence",
                    False,
                )
            applied = await JailedPatchApplicator(root).apply(diff)
            if not applied.ok:
                detail = applied.traceback or applied.message
                return ToolResult(name, detail, False)
            await asyncio.to_thread(get_index().reindex)
            return ToolResult(name, applied.message, True)
    except JailError as exc:
        return ToolResult(name, f"jail blocked the call: {exc}", False)
    except FileNotFoundError as exc:
        return ToolResult(name, f"executable not found: {exc}", False)
    except OSError as exc:
        return ToolResult(name, f"tool failed: {exc}", False)
    return ToolResult(name, "no-op", True)
