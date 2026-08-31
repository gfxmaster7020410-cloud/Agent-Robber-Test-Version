"""Unified execution loop that bridges Phase 1–5 engines onto /ws."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from robber.prefs import CONTROL_MODES, load_control_mode, save_control_mode

logger = logging.getLogger(__name__)

SUITE_EVENT_TYPES = (
    "self_heal",
    "vision_alert",
    "vault_alert",
    "worker_tick",
    "loop_state",
    "checkpoint",
)

CHECKPOINT_TIMEOUT = 180.0
AUTOMATION_RE = re.compile(
    r"^\s*/?(start|stop|status|logs)\s+(automation|workers?|shark|loop|engine)s?\s*$",
    re.IGNORECASE,
)
QUEUE_WORKER_RE = re.compile(
    r"^\s*/?(?:queue|run)\s+worker\s+(\S+)(?:\s+(\S+))?\s*$",
    re.IGNORECASE,
)

DispatchFn = Callable[[Any, str], Awaitable[None]]
TurnFn = Callable[[str], Awaitable[None]]

_LOOP: AgentLoopManager | None = None


def suite_event(kind: str, content: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    if kind not in SUITE_EVENT_TYPES:
        raise ValueError(f"unknown suite event '{kind}'")
    return {
        "type": kind,
        "content": content,
        "metadata": dict(metadata or {}),
        "timestamp": time.time(),
    }


def get_loop() -> AgentLoopManager | None:
    return _LOOP


def bind_loop(loop: AgentLoopManager | None) -> None:
    global _LOOP
    _LOOP = loop


def _public_event(payload: dict[str, Any]) -> dict[str, Any]:
    meta = payload.get("metadata")
    return {
        "type": payload.get("type"),
        "content": payload.get("content"),
        "metadata": meta if isinstance(meta, dict) else {},
        "timestamp": payload.get("timestamp") or time.time(),
    }


class AgentLoopManager:
    """Orchestrates router → workers → proxies → browser → heal/vision."""

    def __init__(
        self,
        hub: Any,
        *,
        command_router: Any = None,
        worker_pool: Any = None,
        proxy_manager: Any = None,
        vault: Any = None,
        vision: Any = None,
        healer: Any = None,
        session_manager: Any = None,
        account_manager: Any = None,
        database: Any = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.hub = hub
        self.command_router = command_router
        self.worker_pool = worker_pool
        self.proxy_manager = proxy_manager
        self.vault = vault
        self.vision = vision
        self.healer = healer
        self.session_manager = session_manager
        self.account_manager = account_manager
        self.database = database
        self.state = state if state is not None else {}
        self.control_mode = load_control_mode()
        self.phase = "idle"
        self.last_heal: dict[str, Any] | None = None
        self.last_vision: dict[str, Any] | None = None
        self.last_tick: dict[str, Any] | None = None
        self.last_vault: dict[str, Any] | None = None
        self.pending_checkpoint: dict[str, Any] | None = None
        self._checkpoint: asyncio.Future[bool] | None = None
        self._pending_diff: str | None = None
        self._run_turn: TurnFn | None = None
        self._dispatch: DispatchFn | None = None
        self._snapshot: Callable[[], dict[str, Any]] | None = None
        self._turn_lock = asyncio.Lock()
        self._install_observer()

    def bind_runtime(
        self,
        *,
        run_turn: TurnFn,
        dispatch: DispatchFn,
        turn_lock: asyncio.Lock | None = None,
        snapshot: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._run_turn = run_turn
        self._dispatch = dispatch
        self._snapshot = snapshot
        if turn_lock is not None:
            self._turn_lock = turn_lock

    def is_autonomous(self) -> bool:
        return self.control_mode == "autonomous"

    def tree(self) -> dict[str, Any]:
        workers = self._worker_rows()
        proxies = self.proxy_manager.proxies if self.proxy_manager is not None else {}
        assigned = self.proxy_manager.profile_proxy_map if self.proxy_manager is not None else {}
        return {
            "control_mode": self.control_mode,
            "agent_mode": self.state.get("mode", "act"),
            "phase": self.phase,
            "busy": bool(self.state.get("busy")),
            "workers": workers,
            "proxy_count": len(proxies),
            "proxy_assigned": len(assigned),
            "vision_ready": self.vision is not None,
            "healer_bound": self.healer is not None,
            "playwright_workers": sum(1 for row in workers if row.get("status") == "running"),
            "last_heal": self.last_heal,
            "last_vision": self.last_vision,
            "last_tick": self.last_tick,
            "last_vault": self.last_vault,
            "checkpoint": self.pending_checkpoint,
        }

    def loop_state_frame(self) -> dict[str, Any]:
        tree = self.tree()
        return suite_event(
            "loop_state",
            f"[LOOP] {tree['control_mode']} · {tree['phase']}",
            tree,
        )

    def observe(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "self_heal":
            self.last_heal = _public_event(payload)
        elif kind == "vision_alert":
            self.last_vision = _public_event(payload)
        elif kind == "worker_tick":
            self.last_tick = _public_event(payload)
        elif kind == "vault_alert":
            self.last_vault = _public_event(payload)
        elif kind == "checkpoint":
            meta = payload.get("metadata")
            if isinstance(meta, dict):
                self.pending_checkpoint = meta

    async def set_control_mode(self, mode: str) -> str:
        value = save_control_mode(mode)
        self.control_mode = value
        if value == "autonomous":
            await self.resolve_checkpoint(True, reason="switched to autonomous")
        await self._emit_system(f"Control suite set to {value}")
        if self.hub is not None and self._snapshot is not None:
            await self.hub.broadcast(self._snapshot())
        await self.broadcast_tree()
        return value

    async def broadcast_tree(self) -> None:
        if self.hub is None:
            return
        try:
            await self.hub.broadcast(self.loop_state_frame())
        except Exception:
            logger.debug("loop_state broadcast skipped")

    async def handle_ws(self, sender: Any, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self._forward_dispatch(sender, raw)
            return
        if not isinstance(data, dict):
            await self._forward_dispatch(sender, raw)
            return

        message_type = str(data.get("type") or data.get("action") or "").strip()
        lowered = message_type.lower()

        if lowered in {"set_control_mode", "set_loop_mode"}:
            mode = str(data.get("mode") or data.get("control_mode") or "").strip().lower()
            if mode not in CONTROL_MODES:
                await self._error(sender, f"Unknown control mode '{mode}'. Valid: {', '.join(CONTROL_MODES)}")
                return
            await self.set_control_mode(mode)
            return

        if lowered == "set_mode":
            mode = str(data.get("mode") or "").strip().lower()
            if mode in CONTROL_MODES:
                await self.set_control_mode(mode)
                return

        if lowered in {"heal_approve", "checkpoint_approve"}:
            await self.resolve_checkpoint(True)
            return
        if lowered in {"heal_reject", "heal_deny", "checkpoint_reject"}:
            await self.resolve_checkpoint(False)
            return
        if lowered == "loop_status":
            if self.hub is not None:
                await self.hub.send_to(sender, self.loop_state_frame())
            return

        if lowered == "command":
            content = str(data.get("content") or data.get("text") or data.get("prompt") or "")
            await self._run_command(content, sender)
            return

        if lowered in {"prompt", "user_prompt"}:
            text = str(data.get("text") or data.get("prompt") or data.get("content") or "").strip()
            if not text:
                await self._error(sender, "Prompt is empty")
                return
            await self.execute_prompt(text, sender=sender)
            return

        await self._forward_dispatch(sender, raw)

    def parse_intent(self, query: str) -> tuple[str, str]:
        text = query.strip()
        queued = QUEUE_WORKER_RE.match(text)
        if queued:
            profile_id = queued.group(1)
            target = queued.group(2) or "default"
            return "queue_worker", f"{profile_id} {target}"
        automation = AUTOMATION_RE.match(text)
        if automation:
            return "automation", automation.group(1).lower()
        return "agent", text

    async def execute_prompt(self, query: str, *, sender: Any = None) -> None:
        if self._turn_lock.locked():
            await self._error(sender, "Agent is busy — wait for the current turn")
            return
        async with self._turn_lock:
            intent, payload = self.parse_intent(query)
            await self._set_phase("parse", intent=intent)
            try:
                if intent == "automation":
                    await self._run_command(payload, sender)
                    return
                if intent == "queue_worker":
                    profile_id, _, target = payload.partition(" ")
                    await self.queue_worker(profile_id, target or "default")
                    return
                await self._set_phase("turn")
                await self._run_agent_turn(query)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("agent loop turn failed (%s)", type(exc).__name__)
                await self._emit_system(f"Loop fell back to operator ({type(exc).__name__})")
                if self.hub is not None:
                    await self.hub.broadcast(
                        suite_event("loop_state", f"[LOOP] {self.control_mode} · error: {type(exc).__name__}", self.tree())
                    )
            finally:
                await self._set_phase("idle")
                await self.broadcast_tree()

    async def queue_worker(self, profile_id: str, target: str) -> None:
        profile_id = profile_id.strip()
        target = (target or "default").strip() or "default"
        if not profile_id:
            raise ValueError("profile_id is required")
        if self.proxy_manager is not None:
            self.proxy_manager.assign_to_profile(profile_id)
        if self.worker_pool is None:
            raise RuntimeError("worker pool is not bound")
        await self.worker_pool.start_worker(profile_id, target)
        await self._set_phase("queued", profile_id=profile_id, target=target)
        if self.hub is not None:
            await self.hub.broadcast(
                suite_event(
                    "worker_tick",
                    f"[WORKER:{profile_id}] queued",
                    {"profile_id": profile_id, "target": target, "status": "queued"},
                )
            )

    async def request_heal_approval(self, diff: str, meta: dict[str, Any] | None = None) -> bool:
        if self.is_autonomous():
            return True
        if self._checkpoint is not None and not self._checkpoint.done():
            self._checkpoint.set_result(False)
        self._pending_diff = diff
        self.pending_checkpoint = {
            "pending": True,
            "reason": "heal_patch",
            **(meta or {}),
        }
        loop = asyncio.get_running_loop()
        self._checkpoint = loop.create_future()
        if self.hub is not None:
            await self.hub.broadcast(
                suite_event(
                    "checkpoint",
                    "[LOOP] Patch ready — waiting for operator (/approve or /reject)",
                    self.pending_checkpoint,
                )
            )
        try:
            return await asyncio.wait_for(self._checkpoint, timeout=CHECKPOINT_TIMEOUT)
        except asyncio.TimeoutError:
            await self._emit_system("Heal checkpoint timed out — operator needed")
            return False
        finally:
            self.pending_checkpoint = None
            self._pending_diff = None
            self._checkpoint = None

    async def resolve_checkpoint(self, approved: bool, *, reason: str = "") -> None:
        future = self._checkpoint
        self.pending_checkpoint = None
        if future is not None and not future.done():
            future.set_result(approved)
        label = "approved" if approved else "rejected"
        extra = f" ({reason})" if reason else ""
        if self.hub is not None:
            await self.hub.broadcast(
                suite_event(
                    "checkpoint",
                    f"[LOOP] Checkpoint {label}{extra}",
                    {"pending": False, "approved": approved},
                )
            )
        await self.broadcast_tree()

    async def collect_health(self) -> dict[str, Any]:
        engines = {
            "database": await self._check_database(),
            "proxies": self._check_proxies(),
            "playwright": await self._check_playwright(),
            "self_healing": {
                "ok": self.healer is not None,
                "bound": self.healer is not None,
                "control_mode": self.control_mode,
            },
            "vision": {
                "ok": self.vision is not None,
                "multimodal": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
            },
            "mcp": self._check_mcp(),
            "ssot_watch": self._check_ssot_watch(),
            "ssot_context": self._check_ssot_context(),
        }
        ok = all(bool(item.get("ok")) for item in engines.values())
        return {
            "ok": ok,
            "control_mode": self.control_mode,
            "phase": self.phase,
            "engines": engines,
            "loop": self.tree(),
            "timestamp": time.time(),
        }

    async def _run_agent_turn(self, query: str) -> None:
        if self._run_turn is None:
            raise RuntimeError("agent turn is not bound")

        async def factory() -> None:
            await self._run_turn(query)

        if self.is_autonomous() and self.healer is not None:
            await self.healer.run_with_heal("agent_turn", factory)
            return
        await factory()

    async def _run_command(self, cmd: str, sender: Any) -> None:
        if self.command_router is None:
            await self._error(sender, "Command router is not bound")
            return
        raw = json.dumps({"type": "command", "content": cmd})
        client_id = str(id(sender)) if sender is not None else "loop"
        await self._set_phase("command", command=cmd)
        async for response in self.command_router.process_message(client_id, raw):
            if self.hub is None:
                continue
            if sender is not None:
                await self.hub.send_to(sender, response)
            else:
                await self.hub.broadcast(response)
        await self.broadcast_tree()

    async def _set_phase(self, phase: str, **meta: Any) -> None:
        self.phase = phase
        if self.hub is None:
            return
        try:
            await self.hub.broadcast(
                suite_event("loop_state", f"[LOOP] {self.control_mode} · {phase}", {**self.tree(), **meta})
            )
        except Exception:
            logger.debug("phase broadcast skipped")

    async def _emit_system(self, content: str) -> None:
        if self.hub is None:
            return
        await self.hub.broadcast(
            {
                "type": "message",
                "role": "system",
                "content": content,
                "append": False,
                "timestamp": time.time(),
            }
        )

    async def _error(self, sender: Any, message: str) -> None:
        frame = {"type": "error", "content": message, "timestamp": time.time()}
        if self.hub is None:
            return
        if sender is not None:
            await self.hub.send_to(sender, frame)
        else:
            await self.hub.broadcast(frame)

    async def _forward_dispatch(self, sender: Any, raw: str) -> None:
        if self._dispatch is None:
            await self._error(sender, "Dispatcher is not bound")
            return
        await self._dispatch(sender, raw)

    def _worker_rows(self) -> list[dict[str, Any]]:
        pool = self.worker_pool
        if pool is None:
            return []
        rows: list[dict[str, Any]] = []
        states = getattr(pool, "worker_states", {}) or {}
        running = getattr(pool, "workers", {}) or {}
        for profile_id, info in states.items():
            rows.append(
                {
                    "profile_id": profile_id,
                    "target": info.get("target"),
                    "status": "running" if profile_id in running else str(info.get("status") or "idle"),
                }
            )
        return rows

    async def _check_database(self) -> dict[str, Any]:
        if self.database is None:
            return {"ok": False, "detail": "unbound"}
        try:
            if not getattr(self.database, "connected", False):
                await self.database.connect()
            row = await self.database.fetchone("SELECT 1 AS ok")
            return {"ok": row is not None, "detail": "wal"}
        except Exception as exc:
            return {"ok": False, "detail": type(exc).__name__}

    def _check_proxies(self) -> dict[str, Any]:
        if self.proxy_manager is None:
            return {"ok": True, "count": 0, "assigned": 0, "optional": True}
        count = len(self.proxy_manager.proxies)
        assigned = len(self.proxy_manager.profile_proxy_map)
        return {"ok": True, "count": count, "assigned": assigned, "optional": count == 0}

    def _check_mcp(self) -> dict[str, Any]:
        from robber.mcp.host import get_mcp_host

        snap = get_mcp_host().snapshot()
        return {
            "ok": True,
            "started": bool(snap.get("started")),
            "count": len(snap.get("servers") or []),
            "assigned": int(snap.get("enabled") or 0),
            "detail": f"{snap.get('enabled', 0)} enabled / {len(snap.get('tools') or [])} tools",
        }

    def _check_ssot_watch(self) -> dict[str, Any]:
        from robber.ssot_watch import get_ssot_watcher

        stats = get_ssot_watcher().public_stats()
        return {
            "ok": True,
            "bound": True,
            "started": bool(stats.get("watching")),
            "count": int(stats.get("watch_events") or 0),
            "detail": "watching" if stats.get("watching") else "idle",
        }

    def _check_ssot_context(self) -> dict[str, Any]:
        from robber.ssot_context import DEFAULT_CONTEXT_WINDOW, format_window

        window = int((self.state or {}).get("context_window") or DEFAULT_CONTEXT_WINDOW)
        packed = int((self.state or {}).get("context_packed") or 0)
        return {
            "ok": window >= 1,
            "bound": True,
            "count": window,
            "assigned": packed,
            "detail": f"{format_window(window)} window · {packed} packed",
        }

    async def _check_playwright(self) -> dict[str, Any]:
        try:
            from playwright.async_api import async_playwright

            playwright = await async_playwright().start()
            try:
                path = Path(playwright.chromium.executable_path)
                exists = path.is_file()
                return {
                    "ok": exists,
                    "detail": str(path) if exists else "chromium missing — run playwright install chromium",
                }
            finally:
                await playwright.stop()
        except Exception as exc:
            return {"ok": False, "detail": type(exc).__name__}

    def _install_observer(self) -> None:
        if self.hub is None or not hasattr(self.hub, "broadcast"):
            return
        inner = self.hub.broadcast

        async def observed(payload: dict[str, Any]) -> None:
            try:
                self.observe(payload)
            except Exception:
                logger.debug("loop observe skipped")
            await inner(payload)

        self.hub.broadcast = observed  # type: ignore[method-assign]
