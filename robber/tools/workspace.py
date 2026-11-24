"""Jailed workspace tools: list, read, git, allowlisted command runner."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from robber.intent import is_inquiry
from robber.jail import JailError, is_skipped_dir, resolve_inside, workspace_root
from robber.patch import extract_diff
from robber.security.perimeter import SelfModificationProtectedError
from robber.ssot import get_index
from robber.terminal import CommandResult, TerminalSupervisor
from robber.tools.command_guard import CommandBlockedError, extract_shell_command, looks_like_shell_command
from robber.tools.patch_applier import JailedPatchApplicator

MAX_LIST = 24
MAX_READ_BYTES = 64_000
PASTE_READ_BYTES = 8_000_000
MAX_OUTPUT_CHARS = 2_400

FILE_EXT = r"py|ts|tsx|js|json|md|txt|css|log|toml|yml|yaml"
FILE_TOKEN = re.compile(
    rf"(?:[\w.-]+/)*[\w.-]+\.(?:{FILE_EXT})",
    re.IGNORECASE,
)
QUOTED_PATH = re.compile(
    rf'["\']([^"\']+\.(?:{FILE_EXT}))["\']',
    re.IGNORECASE,
)
ABS_WIN_FILE = re.compile(
    rf'[A-Za-z]:[\\/](?:[^\\/:*?"<>|\r\n]+[\\/])*[^\\/:*?"<>|\r\n]+\.(?:{FILE_EXT})',
    re.IGNORECASE,
)
ABS_WIN_ANY = re.compile(
    r'[A-Za-z]:[\\/][^\n"<>|*?]+',
)
ABS_POSIX_FILE = re.compile(
    rf"(?:/(?:[\w.-]+)+)+\.(?:{FILE_EXT})",
    re.IGNORECASE,
)
READ_INTENT = re.compile(r"\b(read|open|show|cat|contents?)\b", re.IGNORECASE)
LIST_INTENT = re.compile(r"\b(scan|director|folder|tree)\b", re.IGNORECASE)
WEB_INTENT = re.compile(
    r"\b(search the web|web search|google|look up online|search online|wikipedia)\b",
    re.IGNORECASE,
)
HOST_INTENT = re.compile(
    r"\b(whole (pc|machine|computer)|all (my )?files|my (desktop|documents|downloads)|"
    r"outside (the )?(repo|workspace|project)|host files|this (pc|computer))\b",
    re.IGNORECASE,
)
SECRET_NAMES = {".env", "credentials.json", "id_rsa", "id_ed25519"}
HOST_SKIP = SECRET_NAMES | {"appdata", "application data", ".ssh", ".aws", ".gnupg"}

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
    "whoami",
    "whoami.exe",
}

ALLOWED_GIT = {"status", "diff", "log", "branch", "rev-parse", "show"}
BLOCKED_GIT = {"push", "reset", "clean", "rebase", "commit", "checkout", "stash"}


@dataclass(frozen=True)
class ToolResult:
    name: str
    output: str
    ok: bool


CREATE_NAMED_FILE = re.compile(
    r"\b(?:create|write|make)\b(?:\s+a)?\s+([\w./\\-]+\.(?:py|js|ts|tsx|txt))\b",
    re.IGNORECASE,
)


def simple_new_file(query: str) -> tuple[str, str] | None:
    from robber.intent import is_simple_file_task

    if not is_simple_file_task(query):
        return None
    match = CREATE_NAMED_FILE.search(query or "")
    if not match:
        return None
    name = match.group(1).replace("\\", "/").lstrip("/")
    lowered = (query or "").lower()
    if "hello world" in lowered:
        if name.lower().endswith(".py"):
            return name, 'print("Hello, World!")\n'
        return name, "Hello, World!\n"
    return None


def clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n… (truncated)"


def is_conversational(query: str) -> bool:
    return is_inquiry(query)


def select_tool(query: str) -> str:
    lowered = query.lower()
    wants_read = bool(READ_INTENT.search(query))
    if "paste_buffer" in lowered or (wants_read and pick_read_target(query)):
        return "Read File"
    if re.search(r"(^|/|\b)mcp\b", lowered) or "mcp__" in lowered:
        return "MCP Host"
    if extract_diff(query) or re.search(
        r"\b(apply patch|apply this (?:diff|patch)|jailed patch)\b", lowered
    ):
        return "Jailed Patch Apply"
    if re.search(r"\b(replace (function|class|symbol)|rewrite (def|function|class))\b", lowered):
        return "AST Edit"
    if re.search(r"\b(analyze code|ast scan|list symbols|code analysis)\b", lowered):
        return "AST Analyze"
    if re.search(r"\b(scrape|fetch url|fetch page|download page)\b", lowered):
        return "Web Scrape"
    if WEB_INTENT.search(query):
        return "Web Search"
    if HOST_INTENT.search(query):
        return "Host Files"
    if is_inquiry(query):
        return "Conversation"
    if wants_read:
        return "Read File"
    if LIST_INTENT.search(query):
        return "Local Directory Scan"
    if re.search(r"\b(git|branch|commit)\b", lowered) or re.search(
        r"\bgit\s+diff\b", lowered
    ):
        return "Git Status Probe"
    if re.search(r"\b(memory|recall|remember|context)\b", lowered):
        return "SSOT Memory Query"
    if re.search(
        r"\b(run|build|terminal|shell|whoami|pytest)\b|\bnpm test\b|\brun tests\b",
        lowered,
    ):
        return "Command Runner"
    if looks_like_shell_command(query):
        return "Command Runner"
    if pick_read_target(query) and wants_read:
        return "Read File"
    return "Direct Execution"


def list_host_files() -> str:
    """Shallow listing of the user profile dirs — not a full-disk crawl."""
    home = Path.home()
    roots = [
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        home,
    ]
    seen: set[Path] = set()
    blocks: list[str] = [
        "Host listing is Desktop / Documents / Downloads / home (not the whole disk)."
    ]
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        names: list[str] = []
        try:
            children = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError as exc:
            blocks.append(f"{resolved}: {exc}")
            continue
        for child in children:
            if child.name.startswith(".") or child.name.lower() in HOST_SKIP:
                continue
            if child.is_dir() and is_skipped_dir(child.name):
                continue
            mark = "/" if child.is_dir() else ""
            names.append(f"{child.name}{mark}")
            if len(names) >= MAX_LIST:
                names.append("…")
                break
        blocks.append(f"{resolved} ({len(names)} shown)\n" + "\n".join(names))
    return "\n\n".join(blocks)


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
    header = f"listing of {root} ({len(entries)} shown)"
    return header + "\n" + "\n".join(entries) if entries else header + "\n(empty)"


def read_workspace_file(root: Path, relative: str, allow_external: bool = True) -> str:
    del allow_external
    target = resolve_inside(root, relative)
    if not target.is_file():
        raise JailError(f"not a file: {relative}")
    if target.name.lower() in SECRET_NAMES:
        raise JailError(f"refusing to read secret file: {target.name}")

    size = target.stat().st_size
    rel = str(target).replace("\\", "/")
    paste_file = "tmp_context/paste_buffer" in rel or target.name.startswith("paste_buffer")
    limit = PASTE_READ_BYTES if paste_file else MAX_READ_BYTES
    if size > limit:
        raise JailError(f"file too large to read ({size} bytes)")

    text = target.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    cap = len(lines) if paste_file else 80
    body = "\n".join(lines[:cap])
    return f"{target} ({min(len(lines), cap)} lines)\n{body}"


def pick_read_target(query: str) -> str | None:
    quoted = QUOTED_PATH.search(query)
    if quoted:
        return quoted.group(1).strip()
    win = ABS_WIN_FILE.search(query)
    if win:
        return win.group(0).strip()
    posix = ABS_POSIX_FILE.search(query)
    if posix:
        return posix.group(0).strip()
    match = FILE_TOKEN.search(query)
    return match.group(0) if match else None


def pick_dir_target(query: str) -> str | None:
    quoted = QUOTED_PATH.search(query)
    if quoted:
        raw = quoted.group(1).strip()
        if Path(raw).is_dir() or not Path(raw).suffix:
            return raw
    win = ABS_WIN_ANY.search(query)
    if win:
        return win.group(0).rstrip(".,;")
    posix = ABS_POSIX_FILE.search(query)
    if posix:
        parent = str(Path(posix.group(0)).parent)
        return parent if parent not in {".", ""} else posix.group(0)
    return None


def _is_absolute_user_path(relative: str) -> bool:
    text = relative.strip().strip('"')
    return bool(re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("/") or text.startswith("\\\\"))


async def git_status(supervisor: TerminalSupervisor) -> CommandResult:
    return await supervisor.run(["git", "status", "--short", "--branch"], timeout=12)


async def run_allowlisted(
    supervisor: TerminalSupervisor,
    argv: list[str],
    *,
    on_line: Callable[[str], Awaitable[None] | None] | None = None,
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
    return await supervisor.run(argv, timeout=20, on_line=on_line)


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
    if re.search(r"\bwhoami\b", lowered):
        return ["whoami"]
    if re.search(r"\bpytest\b", lowered):
        return ["python", "--version"]
    if re.search(r"\bnpm\b", lowered):
        return ["npm", "-v"]
    if re.search(r"\bnode\b", lowered):
        return ["node", "--version"]
    return ["python", "--version"]


async def execute_tool(
    query: str,
    supervisor: TerminalSupervisor,
    *,
    allow_writes: bool = True,
    allow_shell: bool = True,
    on_line: Callable[[str], Awaitable[None] | None] | None = None,
) -> ToolResult:
    root = workspace_root()
    supervisor.root = root
    name = select_tool(query)
    if not allow_writes and name == "Jailed Patch Apply":
        return ToolResult(name, "Plan mode is read-only — file writes are disabled.", True)
    if not allow_shell and name == "Command Runner":
        return ToolResult(name, "Shell execution is paused in this mode.", True)
    try:
        if name == "Conversation":
            return ToolResult(
                name,
                "Capability question — reply in 1-3 sentences. Do not write files, diffs, "
                "directory trees, or dummy projects.",
                True,
            )
        if name == "Direct Execution":
            created = simple_new_file(query) if allow_writes else None
            if created is not None:
                rel, body = created
                from robber.mutations import get_mutations

                get_mutations().write_file(rel, body, action="create")
                return ToolResult(name, f"Created {rel}", True)
            return ToolResult(
                name,
                "Implement only what the user asked. Do not invent extra files, NodeA.ts, "
                "or placeholder architecture.",
                True,
            )
        if name == "Web Search":
            from robber.tools.web_search import search_web

            return ToolResult(name, clip(search_web(query), 3_200), True)
        if name == "Web Scrape":
            from robber.tools.scrape import fetch_page

            return ToolResult(name, clip(fetch_page(query), 4_000), True)
        if name == "AST Analyze":
            from robber.tools.analyze import analyze_file

            return ToolResult(name, analyze_file(query, root=root), True)
        if name == "AST Edit":
            from robber.tools.ast_edit import replace_symbol

            return ToolResult(name, replace_symbol(query, root=root), True)
        if name == "Host Files":
            return ToolResult(name, clip(list_host_files(), 3_200), True)
        if name in {"Local Directory Scan", "Workspace Context Scan"}:
            listed = pick_dir_target(query)
            target = resolve_inside(root, listed) if listed else root
            if not target.is_dir():
                return ToolResult(name, f"not a directory: {target}", False)
            return ToolResult(name, clip(list_workspace(target)), True)
        if name == "Read File":
            relative = pick_read_target(query)
            if relative is None:
                return ToolResult(name, "No file path found in the prompt.", False)
            return ToolResult(
                name,
                clip(read_workspace_file(root, relative)),
                True,
            )
        if name == "Git Status Probe":
            result = await git_status(supervisor)
            return ToolResult(name, clip(result.output), result.exit_code == 0)
        if name == "Command Runner":
            raw = extract_shell_command(query)
            if raw:
                from robber.tools.terminal_engine import get_terminal_engine

                engine = get_terminal_engine(root)
                loop = asyncio.get_running_loop()

                def _bridge(plain: str, raw_chunk: str) -> None:
                    del plain
                    if on_line is None:
                        return
                    payload = raw_chunk if raw_chunk.endswith("\n") else f"{raw_chunk}\n"
                    maybe = on_line(payload)
                    if asyncio.iscoroutine(maybe):
                        asyncio.run_coroutine_threadsafe(maybe, loop)

                engine.on_chunk = _bridge
                try:
                    term = await engine.run(raw, timeout=60.0)
                finally:
                    engine.on_chunk = None
                return ToolResult(name, clip(term.output), (not term.timed_out) and term.exit_code == 0)
            argv = command_for_query(query)
            result = await run_allowlisted(supervisor, argv, on_line=on_line)
            if result.exit_code != 0 and not result.timed_out:
                from robber.engine.self_healing import get_healer

                healer = get_healer()
                if healer is not None:
                    repaired = await healer.heal_from_stderr(
                        result.output, task_name=" ".join(argv)
                    )
                    if repaired:
                        result = await run_allowlisted(supervisor, argv, on_line=on_line)
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
    except CommandBlockedError as exc:
        return ToolResult(name, f"command blocked: {exc}", False)
    except SelfModificationProtectedError as exc:
        return ToolResult(name, f"self-protection: {exc}", False)
    except FileNotFoundError as exc:
        return ToolResult(name, f"executable not found: {exc}", False)
    except OSError as exc:
        return ToolResult(name, f"tool failed: {exc}", False)
    return ToolResult(name, "no-op", True)
