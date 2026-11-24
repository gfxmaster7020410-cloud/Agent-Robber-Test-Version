"""Slash command handlers. Each function is registered on import."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from robber.commands.registry import CommandResult, ExecutionContext, command, get_registry
from robber.core.ast_parser import ASTCodeParser
from robber.jail import LOCK_FILE, JailError, dir_is_locked, is_skipped_dir, resolve_inside
from robber.notes import add_note, load_notes
from robber.prefs import load_sound, save_sound
from robber.ssot import get_index
from robber.ssot_context import format_window

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


def _fail(text: str, **extra: Any) -> CommandResult:
    return CommandResult(False, text, extra=extra)


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
        f"window   {format_window(int(state.get('context_window') or 0))} ({state.get('context_window')} tokens)",
        f"workspace {ctx.workspace}",
    ]
    try:
        from robber.agent_loop import format_telemetry, get_loop

        lines.append("")
        lines.append(format_telemetry(Path(ctx.workspace), get_loop()))
    except Exception:
        pass
    return _ok("\n".join(lines))


@command(
    name="plugin",
    aliases=("plugins",),
    description="List, reload, enable, or disable third-party tools",
    category="System",
    usage="/plugin [list|reload|enable <name>|disable <name>|run <name>]",
)
async def cmd_plugin(ctx: ExecutionContext) -> CommandResult:
    from robber.plugins.plugin_loader import get_loader

    loader = get_loader(ctx.workspace)
    action = (ctx.args[0].lower() if ctx.args else "list")
    if action in {"list", "ls", "status"}:
        loader.scan()
        rows = loader.catalog()
        if not rows:
            extra = ""
            if loader.errors:
                extra = "\n" + "\n".join(f"  skip {item}" for item in loader.errors[:8])
            return _ok("No plugins loaded. Drop .py or .dll files in .robber/plugins/." + extra)
        lines = ["Plugins:"]
        for row in rows:
            flag = "on" if row.get("enabled") else "off"
            signed = "signed" if row.get("signed") else "unsigned"
            lines.append(f"  {row['name']:<24} {flag:<3} {signed} {row.get('kind')}")
        for item in loader.errors[:8]:
            lines.append(f"  skip {item}")
        return _ok("\n".join(lines), plugins=rows)
    if action == "reload":
        rows = loader.reload()
        skipped = f" skipped={len(loader.errors)}" if loader.errors else ""
        return _ok(f"Reloaded {len(rows)} plugin(s){skipped}.", plugins=loader.catalog())
    if action in {"enable", "disable"}:
        if len(ctx.args) < 2:
            return _fail("Usage: /plugin enable <name>")
        try:
            loader.set_enabled(ctx.args[1], action == "enable")
        except Exception as exc:
            return _fail(str(exc))
        return _ok(f"Plugin {ctx.args[1]} {action}d.")
    if action == "run":
        if len(ctx.args) < 2:
            return _fail("Usage: /plugin run <name>")
        payload: dict[str, Any] = {}
        if len(ctx.args) > 2:
            blob = " ".join(ctx.args[2:])
            try:
                loaded = json.loads(blob)
            except json.JSONDecodeError:
                return _fail("plugin args must be json")
            if not isinstance(loaded, dict):
                return _fail("plugin args must be a json object")
            payload = loaded
        try:
            result = loader.execute(ctx.args[1], payload)
        except Exception as exc:
            return _fail(str(exc))
        return _ok(result.output or "ok", plugin=ctx.args[1], exit_code=result.exit_code)
    return _fail("Usage: /plugin [list|reload|enable <name>|disable <name>|run <name>]")


@command(
    name="keys",
    description="BYOK provider status (no secret values)",
    category="System",
    usage="/keys",
)
async def cmd_keys(ctx: ExecutionContext) -> CommandResult:
    from robber.vault import providers_status
    from robber.config import VAULT_KEY_ENV
    from robber.security.vault import _VAULT, get_vault

    rows = providers_status()
    lines = ["BYOK vault (values never printed):"]
    for name, ok in rows.items():
        lines.append(f"  {name:<10} {'ready' if ok else 'missing'}")
    try:
        if _VAULT is not None or (os.environ.get(VAULT_KEY_ENV) or "").strip():
            stored = get_vault(ctx.workspace).list_public()
            if stored:
                lines.append("SSOT AES-256 slots:")
                for item in stored:
                    lines.append(
                        f"  {str(item.get('provider') or ''):<10} fp {item.get('fingerprint')}"
                    )
    except Exception:
        pass
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
        try:
            from robber.tools.terminal_engine import peek_terminal_engine

            extra = peek_terminal_engine()
            if extra is not None:
                extra.kill_process_group(pid)
        except Exception:
            pass
        return _ok(f"purge pid {pid}: {'killed' if killed else 'not found'}")
    count = await supervisor.kill_all()
    try:
        from robber.tools.terminal_engine import close_terminal_engine

        close_terminal_engine()
    except Exception:
        pass
    return _ok(f"purged {count} agent-spawned process(es)")


@command(
    name="exit",
    aliases=("quit",),
    description="Stop agent subprocesses and signal the TUI to quit",
    category="System",
    usage="/exit",
)
async def cmd_exit(ctx: ExecutionContext) -> CommandResult:
    count = 0
    supervisor = ctx.state.get("supervisor")
    if supervisor is not None and hasattr(supervisor, "kill_all"):
        try:
            count = int(await supervisor.kill_all() or 0)
        except Exception:
            count = 0
    pool = ctx.state.get("worker_pool")
    if pool is not None and hasattr(pool, "stop_all"):
        try:
            await pool.stop_all()
        except Exception:
            pass
    try:
        from robber.memory.ram_guard import get_ram_guard

        get_ram_guard().stop()
    except Exception:
        pass
    return _ok(f"exit: purged {count} agent-spawned process(es)", quit=True)


@command(
    name="local",
    aliases=("ollama", "offline"),
    description="Scan localhost Ollama / LM Studio / vLLM / llama.cpp (GGUF) and register zero-cost fallbacks",
    category="Execution",
    usage="/local [status|scan]",
)
async def cmd_local(ctx: ExecutionContext) -> CommandResult:
    from robber.local.ollama_adapter import get_adapter

    adapter = get_adapter()
    action = (ctx.args[0] if ctx.args else "status").strip().lower()
    if action in {"scan", "refresh", "detect"}:
        adapter.scan()
    snap = adapter.public()
    lines = [
        f"local window {snap.get('window')} tokens",
        f"preferred {snap.get('preferred') or '-'}",
    ]
    endpoints = snap.get("endpoints") or []
    if not endpoints:
        lines.append("no localhost servers cached (run /local scan)")
    for row in endpoints:
        models = ", ".join((row.get("models") or [])[:4]) or "-"
        flag = "up" if row.get("ok") else "down"
        lines.append(f"  {row.get('kind')} {row.get('host')} {flag} [{models}]")
    return _ok("\n".join(lines), **snap)


@command(
    name="spec",
    aliases=("sdd",),
    description="Store OpenAPI, SQL DDL, and DIP contracts in project_state before code",
    category="Planning",
    usage="/spec [goal]",
)
async def cmd_spec(ctx: ExecutionContext) -> CommandResult:
    from robber.engine.sdd_spec import SpecDrivenEngine
    from robber.state_manager import StateManager

    goal = (ctx.argline or "").strip() or str(ctx.state.get("last_prompt") or "workspace service")
    engine = SpecDrivenEngine(ctx.workspace, manager=StateManager(ctx.workspace))
    spec = engine.plan(goal)
    routes = list((spec.openapi.get("paths") or {}).keys())
    lines = [
        f"SpecDrivenEngine {spec.spec_id}",
        f"routes {', '.join(routes[:8]) or '-'}",
        f"contracts {len(spec.contracts)} dip {len(spec.dip)}",
    ]
    if spec.ddl:
        lines.append("ddl sqlite stored in project_state")
    return _ok("\n".join(lines), spec=spec.public())


@command(
    name="profile",
    aliases=("profiler",),
    description="Profile sandboxed tests and flag nested-loop bottlenecks",
    category="Execution",
    usage="/profile [path]",
)
async def cmd_profile(ctx: ExecutionContext) -> CommandResult:
    from robber.diagnostics.profiler import ExecutionProfiler

    profiler = ExecutionProfiler(ctx.workspace)
    rels = [item for item in ctx.args if item]
    report = profiler.record_suite({"ok": True, "elapsed_ms": 0}, rels or None)
    if not rels:
        report.bottlenecks = profiler.scan_tree()
    lines = [
        f"rss {report.rss_bytes} cap {report.rss_cap}",
        f"bottlenecks {len(report.bottlenecks)}",
    ]
    for item in report.bottlenecks[:6]:
        lines.append(f"  {item.path}:{item.line} {item.kind}")
    return _ok("\n".join(lines), **report.public())


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
    name="term",
    aliases=("sh", "shell"),
    description="Run a command in the persistent PTY/ConPTY shell",
    category="Execution",
    usage="/term <command>",
)
async def cmd_term(ctx: ExecutionContext) -> CommandResult:
    command = " ".join(ctx.args).strip()
    if not command:
        return _fail("Usage: /term <command>")
    from robber.tools.command_guard import CommandBlockedError
    from robber.tools.terminal_engine import get_terminal_engine

    engine = get_terminal_engine(ctx.workspace)
    try:
        result = await engine.run(command, timeout=60.0)
    except CommandBlockedError as exc:
        return _fail(str(exc))
    await ctx.host.emit("tool", result.output)
    return _ok(result.output)


@command(
    name="stdin",
    description="Write a line to the persistent terminal (prompts, python -i, wizards)",
    category="Execution",
    usage="/stdin <text>",
)
async def cmd_stdin(ctx: ExecutionContext) -> CommandResult:
    text = " ".join(ctx.args)
    if text == "":
        return _fail("Usage: /stdin <text>")
    from robber.tools.terminal_engine import get_terminal_engine

    engine = get_terminal_engine(ctx.workspace)
    await asyncio.to_thread(engine.ensure_shell)
    engine.write_stdin(text)
    return _ok("stdin written")


@command(
    name="policy",
    description="Command auto-approval policy engine status and dynamic rules",
    category="System",
    usage="/policy [status|trust <prefix>|reset]",
)
async def cmd_policy(ctx: ExecutionContext) -> CommandResult:
    from robber.tools.command_policy import get_command_policy_engine

    engine = get_command_policy_engine(ctx.workspace)
    args = ctx.args
    sub = args[0].lower() if args else "status"

    if sub == "trust" and len(args) > 1:
        prefix = " ".join(args[1:]).strip()
        engine.trust_prefix(prefix)
        return _ok(f"Added trusted prefix: `{prefix}` (auto-approved for session)")
    if sub == "reset":
        engine.reset_trust()
        return _ok("Reset dynamic trusted prefixes to defaults.")

    pending = engine.list_pending()
    lines = [
        "Command Policy Engine (3-Tier Security Matrix)",
        f"  Tier 1 Rules: {len(engine.tier1_rules)} auto-approved patterns",
        f"  Tier 2 Rules: {len(engine.tier2_rules)} mutating/confirmation patterns",
        f"  Tier 3 Rules: {len(engine.tier3_rules)} high-risk/blocked patterns",
        f"  Trusted Overrides: {', '.join(engine.trust_prefixes) if engine.trust_prefixes else '(none)'}",
        f"  Pending Approvals: {len(pending)}",
    ]
    for p in pending:
        lines.append(f"    - [{p['id']}] `{p['command']}` (Tier {p['tier']}: {p['reason']})")
    return _ok("\n".join(lines))


@command(
    name="webtest",
    aliases=("web", "browser"),
    description="Launch web app, test UI, capture screenshots, and auto-fix frontend crashes",
    category="Execution",
    usage="/webtest [url|dev_command]",
)
async def cmd_webtest(ctx: ExecutionContext) -> CommandResult:
    """Visual UI testing and self-healing loop with Playwright browser automation."""
    import re
    import time
    from robber.tools.terminal_engine import get_terminal_engine
    from robber.tools.browser_engine import PlaywrightBrowserEngine
    from robber.diagnostics.browser_inspector import BrowserInspector

    argline = ctx.argline.strip()
    url = "http://127.0.0.1:3000"  # Default local dev URL
    dev_command = None
    port = 3000

    # Parse arguments: either a URL or a dev server command
    if not argline:
        # No args, just test default URL
        pass
    elif argline.startswith("http://") or argline.startswith("https://"):
        url = argline
    else:
        # Assume it's a dev server command (e.g., "npm run dev" or "python app.py")
        dev_command = argline
        # Try to extract port from common patterns
        port_match = re.search(r'(?:port[=\s]+|:)(\d{4,5})', argline, re.I)
        if port_match:
            port = int(port_match.group(1))
        url = f"http://127.0.0.1:{port}"

    await ctx.host.emit("system", f"[WEBTEST] Initializing browser automation for {url}")

    # If dev_command is provided, start the dev server
    pid = None
    if dev_command:
        await ctx.host.emit("system", f"[WEBTEST] Starting dev server: {dev_command}")
        engine = get_terminal_engine(ctx.workspace)
        
        # Parse command into argv list
        import shlex
        try:
            argv = shlex.split(dev_command)
        except Exception:
            argv = dev_command.split()
        
        # Run command in background (non-blocking)
        try:
            job = await asyncio.to_thread(engine.spawn_daemon, argv, cwd=ctx.workspace)
            pid = job.pid
            await ctx.host.emit("system", f"[WEBTEST] Dev server launched (PID {pid})")
        except Exception as exc:
            return _fail(f"Failed to start dev server: {exc}")

        # Wait for port to become available
        await ctx.host.emit("system", f"[WEBTEST] Waiting for port {port} to bind...")
        port_ready = await _wait_for_port(port, timeout=30.0, emit=ctx.host.emit)
        if not port_ready:
            return _fail(f"Timeout waiting for dev server on port {port}")
        
        await ctx.host.emit("system", f"[WEBTEST] Port {port} is ready")
        # Give the server a moment to stabilize
        await asyncio.sleep(1.5)

    # Perform browser test
    browser = PlaywrightBrowserEngine(ctx.workspace)
    inspector = BrowserInspector(ctx.workspace)

    try:
        await ctx.host.emit("system", f"[WEBTEST] Navigating to {url}")
        snapshot = await browser.navigate_and_capture(url)
        
        # Broadcast snapshot metadata via IPC
        await ctx.host.emit("system", f"[WEBTEST] Captured screenshot: {snapshot.screenshot_path}")
        await ctx.host.emit("system", f"[WEBTEST] Status: {snapshot.status_code}, DOM nodes: {len(snapshot.dom_html)}")

        # Inspect for errors
        report = inspector.inspect_snapshot(snapshot)
        
        issues = []
        if not snapshot.ok:
            issues.append(f"HTTP {snapshot.status_code}")
        issues.extend([f"Page Error: {e.get('message', '')}" for e in report.page_errors])
        issues.extend([f"Console {e.get('type', '')}: {e.get('text', '')}" for e in report.console_errors])
        issues.extend([f"Failed {r.get('method', '')} {r.get('url', '')}" for r in report.failed_requests])

        # Stream results to TUI
        if not issues:
            await ctx.host.emit("system", "[WEBTEST] ✓ Clean render — no console errors or network failures")
            result_summary = json.dumps({
                "url": url,
                "status": "PASS",
                "screenshot": snapshot.screenshot_path,
                "issues": 0,
            }, indent=2)
            return _ok(result_summary)
        else:
            await ctx.host.emit("system", f"[WEBTEST] ✗ Found {len(issues)} issue(s)")
            for i, issue in enumerate(issues[:5], 1):
                await ctx.host.emit("system", f"  {i}. {issue}")
            
            # Trigger vision auditor for self-healing
            await ctx.host.emit("system", "[WEBTEST] Triggering VisionAuditorWorker for self-healing...")
            
            # Create a task for the vision auditor
            from robber.agents.vision_auditor import VisionAuditorWorker
            from robber.state_manager import StateManager
            
            manager = StateManager(ctx.workspace)
            worker = VisionAuditorWorker(ctx.workspace, manager=manager, hub=None)
            
            task = {
                "id": f"webtest-{int(time.time())}",
                "payload_json": {
                    "url": url,
                    "auto_heal": True,
                },
            }
            
            result = await worker.run(task, None)
            
            if result.ok:
                await ctx.host.emit("system", "[WEBTEST] ✓ Self-healing succeeded")
            else:
                await ctx.host.emit("system", "[WEBTEST] ✗ Self-healing inconclusive")
            
            result_summary = json.dumps({
                "url": url,
                "status": "HEALED" if result.ok else "FAILED",
                "screenshot": snapshot.screenshot_path,
                "issues": len(issues),
                "healing_result": result.output,
            }, indent=2)
            
            return _ok(result_summary) if result.ok else _fail(result_summary)

    except Exception as exc:
        await ctx.host.emit("system", f"[WEBTEST] Error: {exc}")
        return _fail(f"Browser test failed: {exc}")
    finally:
        await browser.close()


async def _wait_for_port(port: int, timeout: float = 30.0, emit=None) -> bool:
    """Poll until the given port is bound and accepting connections."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(("127.0.0.1", port))
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            await asyncio.sleep(0.5)
    return False


@command(
    name="approve",
    description="Approve a pending Tier-2 shell command confirmation request",
    category="System",
    usage="/approve [request_id]",
)
async def cmd_approve(ctx: ExecutionContext) -> CommandResult:
    from robber.tools.command_policy import get_command_policy_engine

    engine = get_command_policy_engine(ctx.workspace)
    req_id = ctx.args[0] if ctx.args else None
    if engine.approve(req_id):
        return _ok(f"Approved command request {req_id or '(latest)'}.")
    return _fail(f"No matching pending request {req_id or ''} to approve.")


@command(
    name="deny",
    description="Deny a pending Tier-2 shell command confirmation request",
    category="System",
    usage="/deny [request_id]",
)
async def cmd_deny(ctx: ExecutionContext) -> CommandResult:
    from robber.tools.command_policy import get_command_policy_engine

    engine = get_command_policy_engine(ctx.workspace)
    req_id = ctx.args[0] if ctx.args else None
    if engine.deny(req_id):
        return _ok(f"Denied command request {req_id or '(latest)'}.")
    return _fail(f"No matching pending request {req_id or ''} to deny.")


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
    extra = ""
    try:
        from robber.memory.context_compressor import ContextCompressor

        report = ContextCompressor(ctx.workspace).compact_session()
        extra = f" memory_store summaries={report.get('summaries')} tokens={report.get('tokens')}"
    except Exception:
        extra = ""
    return _ok(f"Compacted session memory ({preview}). SSOT index left intact.{extra}")


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


_AMACE_CHECKS = (
    ("robber.ssot", ("atomic_section_lock", "DeadlockRecoveryWatchdog", "MemoryMappedChunkStreamer", "QuantumSafeChecksum")),
    ("robber.ssot_context", (
        "bidirectional_gap_scan",
        "adaptive_relevance_threshold",
        "MemoryMappedChunkStreamer",
        "atomic_section_lock",
        "DeadlockRecoveryWatchdog",
        "stream_document",
    )),
    ("robber.state_graph", ("StateGraphTracker", "StateNode")),
    ("robber.state_manager", ("StateManager", "StateTransitionError")),
    ("robber.handoff_ledger", ("HandoffLedger", "HandoffPayload")),
    ("robber.conflict_resolver", ("ConflictResolutionMatrix",)),
    ("robber.engine.distributed_brain", ("BrainSynthesizer", "DistributedAgentBrain")),
    ("robber.agent_loop", ("PerceptionActionCycle", "AgentLoopManager", "AutonomousAgentLoop")),
    ("robber.context_assembly", ("ContextAssembler", "AssembledContext")),
    ("robber.router", ("SessionFailover", "assign_engine", "classify_strength", "ModelRouter", "UnifiedProviderProxy", "APIKeyRotator", "FailoverMatrix")),
    ("robber.tools.dispatcher", ("FrontierToolDispatcher",)),
    ("robber.engine.pacing_engine", ("PacingEngine", "CoolDownScheduler")),
    ("robber.ssot_db", ("SsotDB", "ConnectionPool")),
    ("robber.intent_analyzer", ("IntentAnalyzer", "IntentPayload")),
    ("robber.planner", ("AutonomousPlanner", "ExecutionPlan")),
)


@command(
    name="audit",
    description="Live AMACE scan, or a precision AST symbol graph when given a query",
    category="System",
    usage="/audit [symbol query]",
)
async def cmd_audit(ctx: ExecutionContext) -> CommandResult:
    import importlib
    import time

    from robber.handoff_ledger import HandoffLedger
    from robber.ssot import DeadlockRecoveryWatchdog, FileMicroLock, SsotIndex
    from robber.state_graph import StateGraphTracker

    query = ctx.argline
    if query:
        from robber.config import GRAPH_PROMPT_TOKEN_CAP
        from robber.memory.context_graph import ContextGraphEngine, extract_query_symbol
        from robber.ssot_context import estimate_tokens

        engine = ContextGraphEngine(ctx.workspace, prompt_budget=GRAPH_PROMPT_TOKEN_CAP)
        packed = engine.prompt_slice(query, depth=2)
        symbol = packed.query or extract_query_symbol(query)
        header = [
            "AMACE symbol graph",
            f"query: {symbol}",
            f"engine: {packed.engine}  elapsed_ms: {packed.elapsed_ms:.3f}",
        ]
        body = engine.allocate("\n".join(header + [packed.text]), GRAPH_PROMPT_TOKEN_CAP)
        tokens = estimate_tokens(body)
        extra = {
            "query": symbol,
            "engine": packed.engine,
            "elapsed_ms": packed.elapsed_ms,
            "tokens": tokens,
            "graph": packed.payload,
        }
        return _ok(body, **extra)

    lines = ["AMACE audit"]
    missing: list[str] = []
    for mod_name, attrs in _AMACE_CHECKS:
        try:
            module = importlib.import_module(mod_name)
        except Exception as exc:
            missing.append(f"{mod_name} ({type(exc).__name__})")
            lines.append(f"[FAIL] {mod_name} — {type(exc).__name__}: {exc}")
            continue
        present = [name for name in attrs if hasattr(module, name)]
        absent = [name for name in attrs if not hasattr(module, name)]
        flag = "ok" if not absent else "WARN"
        lines.append(f"[{flag}] {mod_name}  {', '.join(present) or '—'}")
        if absent:
            missing.extend(f"{mod_name}.{name}" for name in absent)
            lines.append(f"         missing: {', '.join(absent)}")

    root = Path(ctx.workspace)
    lock_dir = root / ".robber" / "locks"
    stale = 0
    if lock_dir.is_dir():
        clock = time.time()
        watchdog = DeadlockRecoveryWatchdog(lock_dir)
        for path in lock_dir.glob("*.lock"):
            if FileMicroLock(path).stale(now=clock, timeout=watchdog.stale_after):
                stale += 1
    index = SsotIndex(root)
    index.load()
    stats = index.stats
    ledger = HandoffLedger(root)
    graph = StateGraphTracker(root)
    pending = ledger.dump_for()
    bottlenecks: list[str] = []
    if int(stats.get("files") or 0) == 0:
        bottlenecks.append("SSOT index is empty — run /sync")
    skipped = stats.get("skipped_by") if isinstance(stats.get("skipped_by"), dict) else {}
    if int(skipped.get("size") or 0) > 0:
        bottlenecks.append(f"{skipped['size']} files skipped for size (mmap cap)")
    if stale:
        bottlenecks.append(f"{stale} stale lock(s) — run /heal")
    if pending:
        bottlenecks.append(f"{len(pending)} unconsumed handoff payload(s)")
    if not graph.nodes:
        bottlenecks.append("state graph has no commits")

    lines.append(f"ssot     {stats.get('files')} files / {stats.get('chunks')} chunks")
    lines.append(f"locks    stale: {stale}")
    lines.append(f"handoff  {len(pending)} pending / graph {len(graph.nodes)} nodes")
    if bottlenecks:
        lines.append("bottlenecks:")
        lines.extend(f"  - {item}" for item in bottlenecks)
    else:
        lines.append("bottlenecks: none")
    extra = {"missing": missing, "bottlenecks": bottlenecks}
    if not missing:
        return _ok("\n".join(lines), **extra)
    return _fail("\n".join(lines), **extra)


@command(
    name="heal",
    description="Release stale file locks and recover execution deadlocks",
    category="System",
    usage="/heal",
)
async def cmd_heal(ctx: ExecutionContext) -> CommandResult:
    from robber.handoff_ledger import HandoffLedger
    from robber.ssot import DeadlockRecoveryWatchdog

    lock_dir = Path(ctx.workspace) / ".robber" / "locks"
    recovered = DeadlockRecoveryWatchdog(lock_dir).recover()
    pruned = HandoffLedger(ctx.workspace).prune()
    return _ok(
        f"Heal complete — released {recovered} stale lock(s), pruned {pruned} expired handoff payload(s)."
    )


@command(
    name="snapshots",
    aliases=("snapshot",),
    description="List point-in-time SSOT and workspace checkpoints",
    category="System",
    usage="/snapshots",
)
async def cmd_snapshots(ctx: ExecutionContext) -> CommandResult:
    from robber.engine.recovery import SnapshotRecoveryEngine, format_snapshot_list

    engine = SnapshotRecoveryEngine(ctx.workspace)
    return _ok(format_snapshot_list(engine.list_snapshots()))


@command(
    name="restore",
    description="Restore SSOT and snapshotted files from a long-horizon checkpoint",
    category="System",
    usage="/restore [snapshot_id]",
)
async def cmd_restore(ctx: ExecutionContext) -> CommandResult:
    from robber.engine.recovery import SnapshotRecoveryEngine, format_snapshot_list

    engine = SnapshotRecoveryEngine(ctx.workspace, hub=ctx.host)
    if not ctx.args:
        return _ok(format_snapshot_list(engine.list_snapshots()))
    try:
        report = engine.restore(str(ctx.args[0]))
    except FileNotFoundError as exc:
        return _fail(str(exc))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _fail(f"restore failed: {exc}")
    try:
        await ctx.host.emit(
            "system",
            f"Restored snapshot {report.get('id')} "
            f"({report.get('files_restored')} file(s), SSOT rolled back).",
        )
        await ctx.host.broadcast()
    except Exception:
        pass
    return _ok(
        f"Restored snapshot {report.get('id')} ({report.get('files_restored')} file(s), SSOT rolled back)."
    )


@command(
    name="sync",
    description="Atomic SSOT re-index using memory-mapped chunk streaming",
    category="Context",
    usage="/sync",
)
async def cmd_sync(ctx: ExecutionContext) -> CommandResult:
    from robber.ssot import SsotIndex, get_index

    index = get_index()
    target = Path(ctx.workspace).resolve()
    if index.root.resolve() != target:
        index = SsotIndex(target)
    stats = index.reindex()
    await ctx.host.broadcast()
    return _ok(
        f"SSOT synced (mmap + atomic lock): {stats.get('files')} files / {stats.get('chunks')} chunks"
    )


@command(
    name="handoff",
    description="Dump unconsumed inter-model handoff payloads for a specialist",
    category="Context",
    usage="/handoff [target_model]",
)
async def cmd_handoff(ctx: ExecutionContext) -> CommandResult:
    from robber.handoff_ledger import HandoffLedger

    target = ctx.args[0] if ctx.args else None
    return _ok(HandoffLedger(ctx.workspace).format_dump(target))


@command(
    name="graph",
    description="Visualize hypothesis evolution tree and active revision branches",
    category="Planning",
    usage="/graph",
)
async def cmd_graph(ctx: ExecutionContext) -> CommandResult:
    from robber.state_graph import StateGraphTracker

    return _ok(StateGraphTracker(ctx.workspace).render_tree())


@command(
    name="loop",
    aliases=("telemetry",),
    description="Live execution-loop telemetry: tokens, micro-locks, state-graph lineage",
    category="System",
    usage="/loop",
)
async def cmd_loop(ctx: ExecutionContext) -> CommandResult:
    from robber.agent_loop import format_telemetry, get_loop

    return _ok(format_telemetry(Path(ctx.workspace), get_loop()))


@command(
    name="horizon",
    aliases=("pace", "long-horizon"),
    description="Long-horizon research: micro-batch, checkpoint, pause, resume",
    category="Execution",
    usage="/horizon [path|status|pause|resume]",
)
async def cmd_horizon(ctx: ExecutionContext) -> CommandResult:
    from robber.agent_loop import get_loop
    from robber.engine.pacing_engine import PacingEngine

    action = (ctx.args[0].lower() if ctx.args else "status")
    loop = get_loop()
    if action == "pause":
        if loop is None:
            return _fail("Horizon pause needs a running daemon loop.")
        job = await loop.pause_horizon()
        if job is None:
            return _fail("No horizon job to pause.")
        return _ok(f"Horizon pause requested at {job.cursor}/{job.total}.")
    if action == "resume":
        if loop is None:
            return _fail("Horizon resume needs a running daemon loop.")
        job = await loop.resume_horizon()
        if job is None:
            return _fail("No paused horizon job to resume.")
        return _ok(f"Horizon {job.id} resumed at {job.cursor}/{job.total}.")
    if action == "status" or not ctx.args:
        engine = loop.pacing if loop is not None else PacingEngine(ctx.workspace)
        job = engine.job or engine.load_job()
        if job is None:
            return _ok("No long-horizon job. Start with /horizon <path-or-prompt>.")
        gaps = engine.pending_gaps(job)
        extra = f"\ngaps: {'; '.join(gaps[:6])}" if gaps else ""
        return _ok(
            f"Horizon {job.id} {job.status} {job.cursor}/{job.total} "
            f"pauses {job.pauses} timeouts {len(job.timeouts)}{extra}"
        )
    query = ctx.argline
    if loop is not None:
        job = await loop.start_horizon(query)
        return _ok(f"Horizon {job.id} started - {job.total} micro-batches from {job.path or 'prompt'}.")
    engine = PacingEngine(ctx.workspace)
    job = await engine.run(query)
    return _ok(job.synthesis or f"Horizon {job.id} {job.status} {job.cursor}/{job.total}")


@command(
    name="resolve",
    description="Run ACRM on recent parallel specialist outputs",
    category="Execution",
    usage="/resolve",
)
async def cmd_resolve(ctx: ExecutionContext) -> CommandResult:
    from robber.conflict_resolver import ConflictResolutionMatrix
    from robber.handoff_ledger import HandoffLedger

    ledger = HandoffLedger(ctx.workspace)
    outputs: dict[str, str] = {}
    for block in reversed(ledger.blocks):
        source = (block.source or "specialist").strip() or "specialist"
        if source in outputs:
            continue
        if (block.body or "").strip():
            outputs[source] = block.body
        if len(outputs) >= 6:
            break
    if len(outputs) < 2:
        return _fail("Need at least two specialist payloads on the handoff ledger. Run a distributed turn first.")
    matrix = ConflictResolutionMatrix()
    report = matrix.report(outputs)
    reconciled = matrix.reconcile(outputs)
    lines = [
        f"ACRM  {report['count']} conflict(s)  kinds={', '.join(report['kinds']) or 'none'}",
        f"specialists: {', '.join(sorted(outputs))}",
    ]
    for item in report["conflicts"]:
        lines.append(
            f"- [{item['kind']}] {item['left_node']} vs {item['right_node']} "
            f"-> {item.get('winner') or '?'} ({item['confidence']:.2f}) {item['detail']}"
        )
    if not report["conflicts"]:
        lines.append("No contradictions detected.")
    winners = {key: (value[:80] + "..." if len(value) > 80 else value) for key, value in reconciled.items()}
    lines.append("arbitrated keys: " + ", ".join(winners))
    return _ok("\n".join(lines), **report)


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
        current = int(ctx.state.get("context_window") or 0)
        return _ok(f"SSOT window {format_window(current)} ({current} tokens)")
    value = await ctx.host.persist_window(ctx.argline)
    await ctx.host.broadcast()
    return _ok(f"SSOT context window set to {format_window(value)} ({value} tokens)")


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


@command(
    name="discover",
    aliases=("models-scan",),
    description="Auto-discover models from provider /v1/models endpoints and refresh the failover pool",
    category="Execution",
    usage="/discover [refresh|disable <id>|enable <id>|pin <id>|unpin <id>]",
)
async def cmd_discover(ctx: ExecutionContext) -> CommandResult:
    from robber.engine.discovery import get_discovery
    from robber.engine.failover_pool import get_failover_pool

    verb = (ctx.args[0].strip().lower() if ctx.args else "")
    svc = get_discovery()
    if verb in {"disable", "enable", "pin", "unpin"}:
        model_id = " ".join(ctx.args[1:]).strip()
        if not model_id:
            return _fail("Usage: /discover disable <model-id>")
        if verb == "disable":
            svc.set_override(model_id, disabled=True)
        elif verb == "enable":
            svc.set_override(model_id, disabled=False)
        elif verb == "pin":
            svc.set_override(model_id, priority_high=True)
        else:
            svc.set_override(model_id, priority_high=False)
        get_failover_pool().hydrate_from_cache()
        await ctx.host.broadcast()
        return _ok(f"Updated discovery override for {model_id}")
    force = verb in {"refresh", "force"}
    fn = getattr(ctx.host, "discover_models", None)
    if callable(fn):
        text = await fn(force)
        return _ok(str(text))
    summary = svc.public_summary()
    pool = get_failover_pool().public()
    return _ok(
        f"Cached endpoints: {len(summary['endpoints'])}, models: {summary['models']}, "
        f"failover pool: {pool['size']}. Use /discover refresh while the daemon is running."
    )


@command(
    name="undo",
    description="Undo the last jailed file mutation",
    category="Workspace",
    usage="/undo",
)
async def cmd_undo(ctx: ExecutionContext) -> CommandResult:
    return await _run_mutation(ctx, "undo_step")


@command(
    name="redo",
    description="Redo the last undone file mutation",
    category="Workspace",
    usage="/redo",
)
async def cmd_redo(ctx: ExecutionContext) -> CommandResult:
    return await _run_mutation(ctx, "redo_step")


@command(
    name="accept",
    aliases=("accept-all",),
    description="Accept all pending file mutations",
    category="Workspace",
    usage="/accept",
)
async def cmd_accept(ctx: ExecutionContext) -> CommandResult:
    return await _run_mutation(ctx, "accept_all")


@command(
    name="revert",
    aliases=("revert-all",),
    description="Revert all session file mutations to their originals",
    category="Workspace",
    usage="/revert",
)
async def cmd_revert(ctx: ExecutionContext) -> CommandResult:
    return await _run_mutation(ctx, "revert_all")


async def _run_mutation(ctx: ExecutionContext, method: str) -> CommandResult:
    fn = getattr(ctx.host, method, None)
    if fn is None:
        return _fail("File mutation manager is not bound")
    text = await fn()
    if str(text).startswith("Nothing"):
        return _fail(str(text))
    return _ok(str(text))


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


@command(
    name="index-repo",
    aliases=("index",),
    description="Index an external repository for cross-repo context and search",
    category="Context",
    usage="/index-repo <path_or_alias>",
)
async def cmd_index_repo(ctx: ExecutionContext) -> CommandResult:
    """Index an external repository or library directory for multi-repo search."""
    argline = ctx.argline.strip()
    if not argline:
        return _fail("Usage: /index-repo <path_or_alias>")

    from robber.memory.multi_repo_context import get_multi_repo_context_engine
    from robber.memory.vector_store import get_vector_store
    
    # Parse path/alias
    target_path = Path(argline)
    if not target_path.exists():
        # Try relative to workspace
        target_path = ctx.workspace / argline
        if not target_path.exists():
            return _fail(f"Path not found: {argline}")
    
    target_path = target_path.resolve()
    repo_name = target_path.name
    
    await ctx.host.emit("system", f"[INDEX-REPO] Indexing repository: {repo_name} at {target_path}")
    
    # Register repo with multi-repo context engine
    engine = get_multi_repo_context_engine(ctx.workspace)
    engine.register_repo(repo_name, target_path)
    
    await ctx.host.emit("system", f"[INDEX-REPO] Scanning files in {repo_name}...")
    
    # Collect files to index
    files_to_index = []
    extensions = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs"}
    
    try:
        for path in target_path.rglob("*"):
            if path.is_file() and path.suffix in extensions:
                # Skip hidden and build directories
                if any(part.startswith(".") or part in {"node_modules", "__pycache__", "target", "dist", "build"} 
                       for part in path.parts):
                    continue
                files_to_index.append(path)
    except Exception as exc:
        return _fail(f"Error scanning directory: {exc}")
    
    await ctx.host.emit("system", f"[INDEX-REPO] Found {len(files_to_index)} files")
    
    # Index files (with memory limit)
    vector_store = get_vector_store(ctx.workspace)
    vector_store.initialize()
    
    indexed_count = 0
    max_files = 500  # Limit to avoid memory issues
    
    for i, file_path in enumerate(files_to_index[:max_files]):
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            
            # Truncate very large files
            if len(content) > 10000:
                content = content[:10000]
            
            # Generate document ID
            doc_id = f"{repo_name}:{file_path.relative_to(target_path)}"
            
            # Add to vector store
            vector_store.add(doc_id, str(file_path), content)
            indexed_count += 1
            
            # Progress update every 50 files
            if (i + 1) % 50 == 0:
                await ctx.host.emit("system", f"[INDEX-REPO] Indexed {i + 1}/{len(files_to_index[:max_files])} files...")
        
        except Exception as exc:
            pass  # Skip files that fail to read
    
    await ctx.host.emit("system", f"[INDEX-REPO] ✓ Indexed {indexed_count} files from {repo_name}")
    await ctx.host.emit("system", f"[INDEX-REPO] Vector store size: {vector_store.count()} documents")
    
    summary = (
        f"Successfully indexed repository: {repo_name}\n"
        f"  Path: {target_path}\n"
        f"  Files indexed: {indexed_count}\n"
        f"  Total vectors: {vector_store.count()}\n"
        f"\nUse /search <query> to search across all indexed repositories."
    )
    
    return _ok(summary)


@command(
    name="search",
    aliases=("find", "query"),
    description="Hybrid search across all indexed repositories (BM25 + vector similarity)",
    category="Context",
    usage="/search <query>",
)
async def cmd_search(ctx: ExecutionContext) -> CommandResult:
    """Perform hybrid search across all indexed repositories."""
    query = ctx.argline.strip()
    if not query:
        return _fail("Usage: /search <query>")
    
    from robber.memory.multi_repo_context import get_multi_repo_context_engine
    
    engine = get_multi_repo_context_engine(ctx.workspace)
    
    # Check if any repos are registered
    repos = engine.list_repos()
    if not repos:
        return _fail(
            "No repositories indexed yet. Use /index-repo <path> to index a repository first."
        )
    
    await ctx.host.emit("system", f"[SEARCH] Searching across {len(repos)} repositories: {', '.join(repos)}")
    await ctx.host.emit("system", f"[SEARCH] Query: {query}")
    
    # Perform hybrid search
    start_time = time.time()
    context = engine.assemble_context(query, top_k=5)
    elapsed_ms = int((time.time() - start_time) * 1000)
    
    await ctx.host.emit("system", f"[SEARCH] Found {len(context.snippets)} results in {elapsed_ms}ms")
    
    if not context.snippets:
        return _fail(f"No results found for query: {query}")
    
    # Format results
    lines = [
        f"Hybrid Search Results for: {query}",
        f"Searched {len(repos)} repositories in {elapsed_ms}ms",
        f"Token budget: {context.total_tokens}/{engine.token_budget}",
        "",
    ]
    
    for i, snippet in enumerate(context.snippets, 1):
        lines.append(f"{i}. [{snippet.repo_name}] {snippet.file_path}")
        lines.append(f"   Lines {snippet.line_start}-{snippet.line_end} | Relevance: {snippet.relevance_score:.3f} | Tokens: {snippet.tokens}")
        lines.append(f"   Preview: {snippet.content[:150]}...")
        lines.append("")
    
    # Emit results via IPC
    for snippet in context.snippets:
        await ctx.host.emit("system", f"[RESULT] {snippet.repo_name}/{snippet.file_path}:{snippet.line_start}")
    
    return _ok("\n".join(lines))


@command(
    name="git",
    description="Show agent branches, stash state, and current git workspace",
    category="Workspace",
    usage="/git [branch|status|restore]",
)
async def cmd_git(ctx: ExecutionContext) -> CommandResult:
    from robber.tools.git_manager import GitManager

    git = GitManager(ctx.workspace)
    if not git.available():
        return _fail("git is not installed")
    if not git.is_repo():
        return _fail("workspace is not a git repository")
    sub = (ctx.args[0].strip().lower() if ctx.args else "branch")
    if sub in {"restore", "reset"}:
        report = git.restore_workspace()
        return _ok(
            f"Restored workspace to {report.get('restored')} "
            f"(now {report.get('branch')}, stash_popped={report.get('stash_popped')})"
        )
    status = git.status()
    session = git.load_session()
    lines = [
        f"Git workspace  branch={status.branch or 'HEAD'}  default={status.default_branch}",
        f"  clean: {'yes' if status.clean else 'no'}  stash: {status.stash_count}",
        f"  ahead/behind: {status.ahead}/{status.behind}",
    ]
    if session:
        lines.append(
            f"  session: {session.branch} (from {session.original_branch}"
            f"{', stashed' if session.stashed else ''})"
        )
    lines.append("  agent branches:")
    if status.agent_branches:
        lines.extend(f"    - {name}" for name in status.agent_branches)
    else:
        lines.append("    (none)")
    if status.porcelain:
        lines.append("  porcelain:")
        lines.extend(f"    {row}" for row in status.porcelain.splitlines()[:20])
    return _ok("\n".join(lines))


@command(
    name="pr",
    aliases=("pull-request", "pullrequest"),
    description="Stage AST-aware commits, push the task branch, and open a pull request",
    category="Workspace",
    usage="/pr [create] [--draft]",
)
async def cmd_pr(ctx: ExecutionContext) -> CommandResult:
    from robber.agents.pr_writer import PRWriterWorker
    from robber.state_manager import StateManager
    from robber.tools.git_manager import GitManager, slug_from_query
    from robber.tools.git_provider import GitProvider

    args = [part.lower() for part in ctx.args]
    draft = "--draft" in args or "draft" in args
    git = GitManager(ctx.workspace)
    if not git.available():
        return _fail("git is not installed")
    git._ensure_repo()
    session = git.load_session()
    branch = git.current_branch()
    if session is None or not branch.startswith("agent/"):
        slug = slug_from_query(ctx.argline or git.root.name)
        branch = git.create_feature_branch("pr", slug)
        await ctx.host.emit("system", f"[PR] isolated branch {branch}")
    commit = git.conventional_commit()
    if commit.ok:
        await ctx.host.emit("system", f"[PR] committed {commit.message}")
    elif "nothing to commit" not in (commit.output or "").lower() and commit.output:
        await ctx.host.emit("system", f"[PR] commit skipped: {commit.output[:200]}")
    manager = StateManager(ctx.workspace)
    git.manager = manager
    writer = PRWriterWorker(ctx.workspace, manager=manager)
    doc = writer.synthesize(
        query=ctx.argline or commit.message,
        files=commit.files,
        commit_message=commit.message,
        branch=branch,
    )
    await ctx.host.emit("system", "[PR] synthesized pull request body")
    provider = GitProvider(ctx.workspace, manager=manager)
    result = provider.push_and_create_pr(branch, doc.title, doc.body, draft=draft)
    if result.ok and result.url:
        await ctx.host.emit("system", f"[PR] {result.url}")
        return _ok(f"{doc.title}\n{result.url}\n\n{doc.body}")
    if result.ok:
        return _ok(f"{doc.title}\n\n{doc.body}\n\n(push/PR remote skipped: no origin credentials)")
    return _fail(
        f"{doc.title}\n\n{doc.body}\n\nPR not opened: {result.error or 'remote failed'}"
    )


def register_default_commands() -> None:
    """Importing this module registers handlers via @command. This is a no-op hook."""
    get_registry()
