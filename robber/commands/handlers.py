"""Slash command handlers. Each function is registered on import."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from robber.commands.registry import CommandResult, ExecutionContext, command, get_registry
from robber.core.ast_parser import ASTCodeParser
from robber.jail import LOCK_FILE, JailError, dir_is_locked, is_skipped_dir, resolve_inside
from robber.notes import add_note, load_notes
from robber.prefs import load_sound, save_sound
from robber.ssot import get_index

HARNESS_MODES = {
    "build": ("act", None, "Full filesystem writes and refactors."),
    "code": ("act", None, "Precision code edits only."),
    "terminal": ("act", None, "Prefer shell / test loops."),
    "plan": ("plan", None, "Read-only architectural planning."),
    "act": ("act", None, "Standard execution turn."),
    "review": ("review", None, "Review-only pass."),
    "autonomous": (None, "autonomous", "Unrestricted autonomous harness."),
    "manual": (None, "manual", "Manual control — confirm heals."),
}

CRASH_GLOBS = ("*.log", "*.stackdump", "npm-debug.log", "yarn-error.log")


def _ok(text: str, *, ephemeral: bool = False, **extra: Any) -> CommandResult:
    return CommandResult(True, text, ephemeral=ephemeral, extra=extra)


def _fail(text: str) -> CommandResult:
    return CommandResult(False, text)


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pick_port(preferred: int = 0) -> int:
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _hash_secret(secret: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{secret}".encode("utf-8")).hexdigest()


async def _notify_os(title: str, body: str) -> str:
    if sys.platform == "win32":
        try:
            import winsound

            winsound.MessageBeep()
        except Exception:
            pass
        escaped = body.replace("'", "''")[:180]
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; "
            "$n.Visible = $true; "
            f"$n.ShowBalloonTip(2500, '{title}', '{escaped}', "
            "[System.Windows.Forms.ToolTipIcon]::Info)"
        )
        try:
            proc = await __import__("asyncio").create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                ps,
                stdout=__import__("asyncio").subprocess.DEVNULL,
                stderr=__import__("asyncio").subprocess.DEVNULL,
            )
            await proc.wait()
            return "desktop balloon shown"
        except Exception:
            return "audio ping (balloon unavailable)"
    sys.stdout.write("\a")
    return "terminal bell"


@command(
    name="help",
    aliases=("h", "?"),
    description="List slash commands by category",
    category="System",
    usage="/help [command]",
)
async def cmd_help(ctx: ExecutionContext) -> CommandResult:
    registry = get_registry()
    if ctx.args:
        item = registry.get(ctx.args[0])
        if item is None:
            return _fail(f"Unknown command: /{ctx.args[0]}")
        return _ok(f"/{item.name} — {item.description}\nUsage: {item.usage}")
    return _ok(registry.help_text())


@command(
    name="status",
    description="Show daemon link, mode, model, SSOT window",
    category="System",
    usage="/status",
)
async def cmd_status(ctx: ExecutionContext) -> CommandResult:
    state = ctx.state
    index = get_index()
    lines = [
        f"session  {ctx.session_id}",
        f"mode     {state.get('mode')} · harness {state.get('harness') or '—'}",
        f"control  {state.get('control_mode') or '—'}",
        f"model    {state.get('model')}",
        f"engine   {state.get('engine')}",
        f"batch    {state.get('batch')} · sound {'on' if load_sound() else 'off'}",
        f"ssot     {index.stats.get('files')} files / {index.stats.get('chunks')} chunks",
        f"window   {state.get('context_window')}",
        f"workspace {ctx.workspace}",
    ]
    return _ok("\n".join(lines))


@command(
    name="keys",
    description="BYOK provider status (no secret values)",
    category="System",
    usage="/keys",
)
async def cmd_keys(ctx: ExecutionContext) -> CommandResult:
    from robber.vault import providers_status

    rows = providers_status()
    lines = ["BYOK vault (values never printed):"]
    for name, ok in rows.items():
        lines.append(f"  {name:<10} {'ready' if ok else 'missing'}")
    return _ok("\n".join(lines))


@command(
    name="purge",
    description="Kill agent-spawned subprocesses",
    category="System",
    usage="/purge [pid]",
)
async def cmd_purge(ctx: ExecutionContext) -> CommandResult:
    supervisor = ctx.state["supervisor"]
    if ctx.args:
        try:
            pid = int(ctx.args[0])
        except ValueError:
            return _fail("Usage: /purge  or  /purge <pid>")
        killed = await supervisor.kill(pid)
        return _ok(f"purge pid {pid}: {'killed' if killed else 'not found'}")
    count = await supervisor.kill_all()
    return _ok(f"purged {count} agent-spawned process(es)")


@command(
    name="theme",
    description="Switch dark | light (synced TUI + GUI)",
    category="System",
    usage="/theme [dark|light|toggle]",
)
async def cmd_theme(ctx: ExecutionContext) -> CommandResult:
    from robber.prefs import load_theme, save_theme

    current = str(ctx.state.get("theme") or load_theme())
    arg = (ctx.args[0] if ctx.args else "toggle").lower()
    if arg == "toggle":
        next_theme = "light" if current == "dark" else "dark"
    elif arg in {"dark", "light"}:
        next_theme = arg
    else:
        return _fail("Usage: /theme  or  /theme dark|light")
    ctx.state["theme"] = save_theme(next_theme)
    await ctx.host.broadcast()
    return _ok(f"Theme set to {next_theme}")


@command(
    name="notify",
    description="Desktop notification now, or after the next long turn",
    category="System",
    usage="/notify [on|off|message]",
)
async def cmd_notify(ctx: ExecutionContext) -> CommandResult:
    arg = ctx.argline.lower()
    if arg in {"", "on"}:
        ctx.state["notify_on_complete"] = True
        return _ok("Will notify when the next long-running turn finishes.")
    if arg == "off":
        ctx.state["notify_on_complete"] = False
        return _ok("Completion notifications off.")
    note = await _notify_os("Agent Robber", ctx.argline)
    return _ok(f"Notify: {ctx.argline} ({note})")


@command(
    name="sound-switch",
    aliases=("sound",),
    description="Toggle audio cues for build / success / error",
    category="System",
    usage="/sound-switch [on|off]",
)
async def cmd_sound(ctx: ExecutionContext) -> CommandResult:
    arg = (ctx.args[0] if ctx.args else "").lower()
    if arg == "on":
        enabled = True
    elif arg == "off":
        enabled = False
    else:
        enabled = not load_sound()
    save_sound(enabled)
    if enabled:
        await _notify_os("Agent Robber", "Sound on")
    return _ok(f"Sound cues {'on' if enabled else 'off'}")


@command(
    name="run",
    description="Start workspace dev server with auto-port detection",
    category="System",
    usage="/run [port|script]",
)
async def cmd_run(ctx: ExecutionContext) -> CommandResult:
    root = ctx.workspace
    port = None
    script = ""
    if ctx.args:
        if ctx.args[0].isdigit():
            port = int(ctx.args[0])
        else:
            script = ctx.args[0]
    pkg = root / "package.json"
    argv: list[str] | None = None
    if script:
        argv = ["npm", "run", script]
    elif pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else {}
        if isinstance(scripts, dict):
            for name in ("dev", "start", "preview"):
                if name in scripts:
                    argv = ["npm", "run", name]
                    break
    if argv is None:
        port = _pick_port(port or 4173)
        argv = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"]
        hint = f"http://127.0.0.1:{port}"
    else:
        port = port or _pick_port(5173)
        os.environ.setdefault("PORT", str(port))
        hint = f"script {' '.join(argv)} · port {port}"
    pid = await ctx.host.start_argv(argv)
    return _ok(f"Started pid {pid} · {hint}\nUse /purge {pid} to stop.")


@command(
    name="fix-dependencies",
    aliases=("dep", "deps"),
    description="Install missing packages from package.json / requirements.txt",
    category="System",
    usage="/fix-dependencies",
)
async def cmd_fix_deps(ctx: ExecutionContext) -> CommandResult:
    root = ctx.workspace
    reports: list[str] = []
    req = root / "requirements.txt"
    if req.is_file():
        code, out = await ctx.host.run_argv(
            [sys.executable, "-m", "pip", "install", "-r", str(req), "-q"],
            timeout=120,
        )
        reports.append(f"pip: exit {code}\n{out[:800]}")
    pkg = root / "package.json"
    if pkg.is_file():
        code, out = await ctx.host.run_argv(["npm", "install"], timeout=120)
        reports.append(f"npm: exit {code}\n{out[:800]}")
    if not reports:
        return _fail("No package.json or requirements.txt in the workspace.")
    return _ok("\n\n".join(reports))


@command(
    name="structure",
    description="Clean logs, ensure exports/plans, lock/unlock sector dirs",
    category="Workspace",
    usage="/structure [status|clean|organize|lock <dir> [pass]|unlock <dir> [pass]]",
)
async def cmd_structure(ctx: ExecutionContext) -> CommandResult:
    root = ctx.workspace
    action = (ctx.args[0] if ctx.args else "status").lower()
    if action == "status":
        locked = [
            str(path.relative_to(root))
            for path in root.rglob(LOCK_FILE)
            if path.is_file()
        ]
        return _ok(
            "folders: exports, plans, research (created by organize)\n"
            f"locked sectors: {', '.join(locked) or 'none'}"
        )
    if action == "organize":
        for name in ("exports", "plans", "research"):
            _ensure_dir(root / name)
        return _ok("Ensured ./exports, ./plans, ./research")
    if action == "clean":
        removed = 0
        for pattern in CRASH_GLOBS:
            for path in root.glob(pattern):
                if path.is_file() and path.name not in {"accounts.db"}:
                    path.unlink()
                    removed += 1
        return _ok(f"Removed {removed} crash log file(s)")
    if action in {"lock", "unlock"}:
        if len(ctx.args) < 2:
            return _fail(f"Usage: /structure {action} <dir> [password]")
        rel = ctx.args[1]
        try:
            target = resolve_inside(root, rel)
        except JailError as exc:
            return _fail(str(exc))
        if target == root:
            return _fail("Cannot lock the workspace root")
        if action == "lock":
            if not target.is_dir():
                return _fail("Path is not a directory")
            secret = ctx.args[2] if len(ctx.args) > 2 else ""
            salt = secrets.token_hex(8)
            payload = {
                "salt": salt,
                "hash": _hash_secret(secret, salt) if secret else "",
                "created": _now_stamp(),
            }
            (target / LOCK_FILE).write_text(json.dumps(payload), encoding="utf-8")
            return _ok(f"Locked sector {rel} (SSOT will skip it). Unlock with /structure unlock {rel}")
        lock_path = target / LOCK_FILE
        if not lock_path.is_file():
            return _fail("Directory is not locked")
        try:
            stored = json.loads(lock_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            stored = {}
        expected = str(stored.get("hash") or "")
        if expected:
            secret = ctx.args[2] if len(ctx.args) > 2 else ""
            if _hash_secret(secret, str(stored.get("salt") or "")) != expected:
                return _fail("Password mismatch")
        lock_path.unlink()
        return _ok(f"Unlocked {rel}")
    return _fail("Usage: /structure [status|clean|organize|lock|unlock]")


@command(
    name="compact",
    description="Compress session memory while keeping SSOT state",
    category="Context",
    usage="/compact",
)
async def cmd_compact(ctx: ExecutionContext) -> CommandResult:
    digest = ctx.host.pack_digest("session compact", 4000)
    notes = load_notes()
    preview = f"{len(notes)} notes · last output {len(str(ctx.state.get('last_output') or ''))} chars"
    add_note("compact_snapshot", digest[:4000], category="session", tags=["compact"])
    path = _ensure_dir(ctx.workspace / ".robber") / "compact.json"
    path.write_text(
        json.dumps({"at": _now_stamp(), "digest": digest[:8000], "notes": len(notes)}, indent=2),
        encoding="utf-8",
    )
    return _ok(f"Compacted session memory ({preview}). SSOT index left intact.")


@command(
    name="save-session",
    aliases=("save",),
    description="Export memory notes and last output to disk",
    category="Context",
    usage="/save-session [label]",
)
async def cmd_save_session(ctx: ExecutionContext) -> CommandResult:
    folder = _ensure_dir(ctx.workspace / ".robber" / "sessions")
    label = ctx.argline.replace(" ", "_") or "session"
    path = folder / f"{label}_{_now_stamp()}.json"
    payload = {
        "mode": ctx.state.get("mode"),
        "harness": ctx.state.get("harness"),
        "model": ctx.state.get("model"),
        "notes": load_notes(),
        "last_output": ctx.state.get("last_output") or "",
        "last_tool": ctx.state.get("last_tool") or "",
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return _ok(f"Session saved to {path.relative_to(ctx.workspace)}")


@command(
    name="save-output",
    description="Write last AI response to ./exports",
    category="Context",
    usage="/save-output [filename]",
)
async def cmd_save_output(ctx: ExecutionContext) -> CommandResult:
    text = str(ctx.state.get("last_output") or "").strip()
    if not text:
        return _fail("No assistant output stored yet.")
    folder = _ensure_dir(ctx.workspace / "exports")
    name = ctx.argline or f"output_{_now_stamp()}.md"
    if not Path(name).suffix:
        name += ".md"
    path = resolve_inside(ctx.workspace, f"exports/{Path(name).name}")
    path.write_text(text, encoding="utf-8")
    return _ok(f"Wrote {path.relative_to(ctx.workspace)}")


@command(
    name="read-research",
    aliases=("research",),
    description="Inject local research docs (.md/.pdf/.json) into the next turn",
    category="Context",
    usage="/read-research [path]",
)
async def cmd_read_research(ctx: ExecutionContext) -> CommandResult:
    root = ctx.workspace
    targets: list[Path] = []
    if ctx.args:
        try:
            targets = [resolve_inside(root, ctx.argline)]
        except JailError as exc:
            return _fail(str(exc))
    else:
        for folder in (root / "research", root / "docs", root):
            if folder.is_dir():
                for suffix in (".md", ".json", ".pdf"):
                    targets.extend(sorted(folder.glob(f"*{suffix}"))[:12])
    blobs: list[str] = []
    used = 0
    for path in targets:
        if not path.is_file() or used >= 8:
            continue
        suffix = path.suffix.lower()
        try:
            if suffix == ".pdf":
                text = _read_pdf(path)
            else:
                text = path.read_text(encoding="utf-8", errors="replace")[:6000]
        except OSError:
            continue
        if not text.strip():
            continue
        used += 1
        rel = path.relative_to(root).as_posix()
        blobs.append(f"### {rel}\n{text[:4000]}")
    if not blobs:
        return _fail("No research docs found. Put .md/.json/.pdf in ./research or pass a path.")
    ctx.host.inject("Research injection:\n" + "\n\n".join(blobs))
    return _ok(f"Injected {used} research file(s) into the next turn.")


@command(
    name="btw",
    description="Side question that does not pollute main history",
    category="Context",
    usage="/btw <question>",
)
async def cmd_btw(ctx: ExecutionContext) -> CommandResult:
    if not ctx.argline:
        return _fail("Usage: /btw <quick question>")
    ctx.host.inject(f"Side question (do not treat as the main task): {ctx.argline}")
    return _ok(f"Queued aside: {ctx.argline}", ephemeral=True)


@command(
    name="look-deep",
    aliases=("deep",),
    description="Multi-pass AST scan of the workspace",
    category="Context",
    usage="/look-deep [path]",
)
async def cmd_look_deep(ctx: ExecutionContext) -> CommandResult:
    root = ctx.workspace
    start = resolve_inside(root, ctx.argline) if ctx.argline else root
    parser = ASTCodeParser()
    files = 0
    symbols = 0
    samples: list[str] = []
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = [
            name
            for name in dirnames
            if not is_skipped_dir(name) and not dir_is_locked(Path(dirpath) / name)
        ]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() not in {".py", ".ts", ".tsx", ".js"}:
                continue
            files += 1
            if files > 80:
                break
            found = parser.extract_symbols(str(path))
            symbols += len(found)
            if found and len(samples) < 12:
                rel = path.relative_to(root).as_posix()
                samples.append(f"{rel}: " + ", ".join(str(item.get("name")) for item in found[:6]))
        if files > 80:
            break
    digest = f"AST scan {files} files · {symbols} symbols\n" + "\n".join(samples)
    out = _ensure_dir(ctx.workspace / ".robber") / "ast-scan.json"
    path_rel = out.relative_to(root).as_posix()
    out.write_text(json.dumps({"files": files, "symbols": symbols, "samples": samples}, indent=2), encoding="utf-8")
    ctx.host.inject(f"Deep AST scan ({path_rel}):\n" + "\n".join(samples[:20]))
    return _ok(digest + f"\nWrote {path_rel}")


@command(
    name="memory",
    description="Query or reindex the SSOT index",
    category="Context",
    usage="/memory [query|reindex|watch]",
)
async def cmd_memory(ctx: ExecutionContext) -> CommandResult:
    index = get_index()
    arg = ctx.argline
    if arg.lower() == "reindex":
        stats = index.reindex()
        return _ok(f"SSOT reindexed: {stats}")
    hits = index.search(arg or "workspace", k=8)
    return _ok(index.format_hits(hits))


@command(
    name="window",
    aliases=("context",),
    description="Show or set the SSOT context window",
    category="Context",
    usage="/window [1t|2m|...]",
)
async def cmd_window(ctx: ExecutionContext) -> CommandResult:
    if not ctx.argline:
        return _ok(f"SSOT window {ctx.state.get('context_window')} tokens")
    value = await ctx.host.persist_window(ctx.argline)
    await ctx.host.broadcast()
    return _ok(f"SSOT context window set to {value} tokens")


@command(
    name="mode",
    aliases=("m",),
    description="Set harness (build|code|plan) or routing tier (eco|balanced|best)",
    category="Execution",
    usage="/mode <build|code|terminal|plan|autonomous|manual|act|review|eco|balanced|best>",
)
async def cmd_mode(ctx: ExecutionContext) -> CommandResult:
    if not ctx.args:
        return _ok(
            f"Turn {ctx.state.get('mode')} · harness {ctx.state.get('harness') or '—'} · "
            f"control {ctx.state.get('control_mode') or '—'} · tier {ctx.state.get('batch')}\n"
            "Usage: /mode build | code | plan | eco | balanced | best"
        )
    key = ctx.args[0].lower()
    
    if key in {"eco", "balanced", "best"}:
        tier = await ctx.host.set_route(key.upper())
        await ctx.host.broadcast()
        return _ok(f"Routing tier set to {tier}")

    spec = HARNESS_MODES.get(key)
    if spec is None:
        return _fail("Usage: /mode build | code | terminal | plan | autonomous | manual | act | review")
    turn, control, blurb = spec
    ctx.state["harness"] = key
    lines = [f"Harness {key}: {blurb}"]
    if turn:
        await ctx.host.set_turn_mode(turn)
        lines.append(f"Turn mode → {turn}")
    if control:
        await ctx.host.set_control_mode(control)
        lines.append(f"Control suite → {control}")
    ctx.host.inject(f"Operator set harness /mode {key}: {blurb}")
    await ctx.host.broadcast()
    return _ok("\n".join(lines))


@command(
    name="model",
    description="Route the next prompt to a specific configured model",
    category="Execution",
    usage="/model <label>",
)
async def cmd_model(ctx: ExecutionContext) -> CommandResult:
    names = ctx.host.list_models()
    if not ctx.argline:
        listing = ", ".join(names[:24]) or "(none)"
        return _ok(f"Active: {ctx.state.get('model')}\nAvailable: {listing}")
    wanted = ctx.argline.lower()
    match = next((name for name in names if name.lower() == wanted), None)
    if match is None:
        match = next((name for name in names if wanted in name.lower()), None)
    if match is None:
        return _fail(f"Unknown model '{ctx.argline}'. Try /model for the list.")
    label = await ctx.host.set_model(match)
    return _ok(f"Active model set to {label}")


@command(
    name="mcps",
    aliases=("mcp",),
    description="MCP server manager — list / enable / disable",
    category="Execution",
    usage="/mcps [list|enable <id>|disable <id>]",
)
async def cmd_mcps(ctx: ExecutionContext) -> CommandResult:
    if not ctx.args or ctx.args[0].lower() == "list":
        return _ok(ctx.host.mcp_catalog())
    action = ctx.args[0].lower()
    if action in {"enable", "disable"}:
        if len(ctx.args) < 2:
            return _fail(f"Usage: /mcps {action} <id>")
        server_id = ctx.args[1]
        if action == "enable":
            await ctx.host.mcp_enable(server_id)
        else:
            await ctx.host.mcp_disable(server_id)
        await ctx.host.broadcast()
        return _ok(f"MCP {action}d: {server_id}")
    return _fail("Usage: /mcps [list|enable <id>|disable <id>]")


@command(
    name="logic-check",
    aliases=("logic",),
    description="Verification pass for algorithm correctness",
    category="Execution",
    usage="/logic-check [focus]",
)
async def cmd_logic(ctx: ExecutionContext) -> CommandResult:
    focus = ctx.argline or "the last change"
    ctx.host.inject(
        "Logic-check harness: validate algorithms, edge cases, and invariants for "
        f"{focus}. Do not write files until the analysis is complete."
    )
    await ctx.host.set_turn_mode("review")
    return _ok("Queued a logic-check pass on the next prompt (review mode).")


@command(
    name="reasoning",
    aliases=("think", "cot"),
    description="Force high-reasoning routing (Best tier / CoT)",
    category="Execution",
    usage="/reasoning [on|off]",
)
async def cmd_reasoning(ctx: ExecutionContext) -> CommandResult:
    arg = (ctx.args[0] if ctx.args else "on").lower()
    if arg == "off":
        ctx.state["force_kind"] = None
        return _ok("Reasoning override cleared.")
    ctx.state["force_kind"] = "reasoning"
    await ctx.host.set_route("BEST")
    ctx.host.inject("Use explicit chain-of-thought. Prefer architecture and trade-off analysis.")
    return _ok("Next turn uses Best-tier / reasoning routing.")


@command(
    name="design-plan",
    aliases=("design",),
    description="Generate architecture and interface specs",
    category="Planning",
    usage="/design-plan [topic]",
)
async def cmd_design(ctx: ExecutionContext) -> CommandResult:
    topic = ctx.argline or "the current workspace"
    ctx.host.inject(
        f"Produce a structural system architecture plan and interface spec for: {topic}. "
        "Include components, data flow, and risks. Do not implement yet."
    )
    await ctx.host.set_turn_mode("plan")
    return _ok("Queued /design-plan. Send the next prompt (or just '.') to generate the canvas.")


@command(
    name="prototype",
    description="Fast-track a lightweight prototype from the active plan",
    category="Planning",
    usage="/prototype [note]",
)
async def cmd_prototype(ctx: ExecutionContext) -> CommandResult:
    ctx.host.inject(
        "Fast-track a minimal prototype from the active design plan. Keep scope small. "
        + (ctx.argline or "")
    )
    await ctx.host.set_turn_mode("act")
    ctx.state["harness"] = "build"
    return _ok("Harness set to build · prototype directive injected.")


@command(
    name="save-plan",
    description="Export the last plan/reasoning canvas to ./plans",
    category="Planning",
    usage="/save-plan",
)
async def cmd_save_plan(ctx: ExecutionContext) -> CommandResult:
    body = str(ctx.state.get("last_reasoning") or ctx.state.get("last_output") or "").strip()
    if not body:
        return _fail("No plan canvas stored yet.")
    folder = _ensure_dir(ctx.workspace / "plans")
    latest = folder / "PLAN_latest.md"
    stamped = folder / f"PLAN_{_now_stamp()}.md"
    latest.write_text(body, encoding="utf-8")
    stamped.write_text(body, encoding="utf-8")
    return _ok(f"Wrote {latest.relative_to(ctx.workspace)}")


@command(
    name="output-analyze-plan",
    aliases=("analyze-plan",),
    description="Turn last tool output into an implementation plan",
    category="Planning",
    usage="/output-analyze-plan",
)
async def cmd_analyze_plan(ctx: ExecutionContext) -> CommandResult:
    tool = str(ctx.state.get("last_tool") or "").strip()
    if not tool:
        return _fail("No tool output stored yet.")
    ctx.host.inject(
        "Analyze this tool output and produce a step-by-step implementation plan:\n" + tool[:8000]
    )
    await ctx.host.set_turn_mode("plan")
    return _ok("Queued plan analysis of the last tool result.")


@command(
    name="ask-questions",
    aliases=("ask",),
    description="Clarify ambiguous requirements before coding",
    category="Planning",
    usage="/ask-questions [topic]",
)
async def cmd_ask(ctx: ExecutionContext) -> CommandResult:
    ctx.host.inject(
        "Ask clarifying questions before writing code. Wait for answers. Topic: "
        + (ctx.argline or "the current request")
    )
    await ctx.host.set_turn_mode("plan")
    return _ok("Q&A mode: next turn will ask clarifying questions first.")


@command(
    name="prepare-next-input",
    aliases=("next",),
    description="Draft an optimized follow-up prompt",
    category="Planning",
    usage="/prepare-next-input",
)
async def cmd_prepare(ctx: ExecutionContext) -> CommandResult:
    ctx.host.inject(
        "Formulate the single best follow-up prompt for the next pipeline stage. "
        "Return only that prompt."
    )
    return _ok("Queued follow-up prompt synthesis. Send '.' or a hint to generate it.")


@command(
    name="test",
    description="Discover and run pytest / npm test / vitest",
    category="Planning",
    usage="/test [pytest|npm|vitest]",
)
async def cmd_test(ctx: ExecutionContext) -> CommandResult:
    root = ctx.workspace
    choice = (ctx.args[0] if ctx.args else "").lower()
    runs: list[tuple[str, list[str]]] = []
    if choice in {"", "pytest"} and (
        (root / "pytest.ini").is_file()
        or (root / "pyproject.toml").is_file()
        or (root / "tests").is_dir()
        or any(root.glob("test_*.py"))
    ):
        runs.append(("pytest", [sys.executable, "-m", "pytest", "-q"]))
    pkg = root / "package.json"
    if pkg.is_file() and choice in {"", "npm", "vitest"}:
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}
        except json.JSONDecodeError:
            scripts = {}
        if isinstance(scripts, dict) and "test" in scripts:
            if choice == "vitest" or "vitest" in str(scripts.get("test")):
                runs.append(("vitest", ["npm", "test", "--", "--run"]))
            else:
                runs.append(("npm test", ["npm", "test"]))
    if not runs:
        return _fail("No pytest / npm test / vitest suite found.")
    chunks: list[str] = []
    for label, argv in runs:
        code, out = await ctx.host.run_argv(argv, timeout=90)
        chunks.append(f"{label} exit {code}\n{out[:1200]}")
        ctx.state["last_tool"] = out
    return _ok("\n\n".join(reports) if (reports := chunks) else "No tests ran.")


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        parts = []
        for page in reader.pages[:8]:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception:
        return ""


def register_default_commands() -> None:
    """Importing this module registers handlers via @command. This is a no-op hook."""
    get_registry()
