"""Unified execution loop that bridges Phase 1–5 engines onto /ws."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from robber.config import MAX_TOKENS_PER_TURN
from robber.events import state_ack_frame
from robber.prefs import CONTROL_MODES, load_control_mode, save_control_mode

logger = logging.getLogger(__name__)

CYCLE_PHASES = ("ingest", "assemble", "plan", "dispatch", "verify", "mutate")

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


def inject_error_delta(exc: BaseException, query: str, *, root: Path | None = None) -> str:
    """Record a heal delta on the state graph and handoff ledger (no secrets)."""
    from robber.handoff_ledger import HandoffLedger
    from robber.state_graph import StateGraphTracker

    detail = f"{type(exc).__name__}: {exc}"
    body = (
        f"ERROR DELTA\nquery: {(query or '')[:400]}\n{detail[:1200]}"
    )
    tracker = StateGraphTracker(root)
    tracker.commit(body, summary="self-heal error delta")
    HandoffLedger(root).post(body, source="heal", target="coding", kind="error_delta")
    return body


def format_telemetry(workspace: Path | None = None, loop: AgentLoopManager | None = None) -> str:
    from robber.jail import workspace_root
    from robber.ssot import DeadlockRecoveryWatchdog
    from robber.state_graph import StateGraphTracker

    root = (workspace or workspace_root()).resolve()
    manager = loop or get_loop()
    tree = manager.tree() if manager is not None else {}
    locks = DeadlockRecoveryWatchdog(root / ".robber" / "locks").count_stale()
    graph = StateGraphTracker(root)
    head = graph.current()
    session = tree.get("session") if isinstance(tree.get("session"), dict) else {}
    lines = [
        "LOOP telemetry",
        f"phase    {tree.get('phase') or 'idle'}",
        f"control  {tree.get('control_mode') or '-'}",
        f"tokens   {tree.get('turn_tokens') or 0} / {tree.get('max_tokens_per_turn') or 0}",
        f"locks    {locks} stale micro-lock(s)",
        f"graph    {len(graph.nodes)} nodes  head {head.version if head else '-'}",
        f"session  {session.get('session_id') or '-'}  strength {session.get('strength') or '-'}",
        f"router   rotations {session.get('rotations') or 0}  failovers {session.get('failovers') or 0}",
        f"engine   {session.get('engine') or tree.get('phase') or '-'}",
    ]
    if head is not None:
        lines.append(f"lineage  {head.branch} {head.summary}")
    job = None
    if manager is not None:
        pacing = getattr(manager, "pacing", None)
        job = getattr(pacing, "job", None) if pacing is not None else None
    if job is not None:
        lines.append(f"horizon  {job.status} {job.cursor}/{job.total} pauses {job.pauses}")
    return "\n".join(lines)


class PerceptionActionCycle:
    """State ingest -> SSOT assemble -> plan -> dispatch -> ACRM verify -> mutate."""

    def __init__(self, loop: AgentLoopManager) -> None:
        self.loop = loop
        self.last_ssot = ""
        self.last_verify: dict[str, Any] = {}

    def _root(self) -> Path:
        from robber.jail import workspace_root

        raw = (self.loop.state or {}).get("workspace")
        if raw:
            return Path(str(raw)).resolve()
        return workspace_root()

    async def before_turn(self, query: str) -> None:
        try:
            await self._before_turn_inner(query)
        except UnicodeDecodeError:
            logger.exception("perception ingest skipped (decode)")
            self.last_ssot = ""

    async def _before_turn_inner(self, query: str) -> None:
        from robber.router import assign_engine, bind_session_router
        from robber.ssot import SsotIndex
        from robber.ssot_context import bidirectional_gap_scan
        from robber.state_graph import StateGraphTracker

        bind_session_router(self.loop.session_router)
        root = self._root()
        await self.loop._set_phase("ingest")
        index = SsotIndex(root)
        index.load()
        await self.loop._set_phase("assemble")
        hits = index.format_hits(index.search(query, k=4))
        gaps = bidirectional_gap_scan()
        gap_text = "\n".join(f"- {item}" for item in gaps[:8])
        self.last_ssot = "\n".join(part for part in (hits, gap_text) if part).strip()
        tracker = StateGraphTracker(root)
        tracker.commit(
            self.last_ssot or query,
            summary=f"ingest {(query or '')[:80]}",
        )
        await self.loop._set_phase("plan")
        engine = assign_engine(query, str((self.loop.state or {}).get("model") or ""))
        tracker.commit(
            f"plan {engine.label} [{engine.provider}]\n{(query or '')[:400]}",
            summary=f"plan {engine.label}",
        )
        from robber.ssot import DeadlockRecoveryWatchdog

        self.loop._graph_public = tracker.public()
        self.loop._stale_locks = DeadlockRecoveryWatchdog(root / ".robber" / "locks").count_stale()

    async def after_turn(self, query: str) -> None:
        try:
            await self._after_turn_inner(query)
        except UnicodeDecodeError:
            logger.exception("perception verify skipped (decode)")

    async def _after_turn_inner(self, query: str) -> None:
        from robber.conflict_resolver import ConflictResolutionMatrix
        from robber.mutations import get_mutations
        from robber.state_graph import StateGraphTracker
        from robber.tools.dispatcher import looks_like_compiler_error

        await self.loop._set_phase("verify")
        root = self._root()
        cards = get_mutations().turn_cards()
        sources: dict[str, str] = {}
        if cards:
            import json

            sources["coding"] = json.dumps(cards, ensure_ascii=False)
        if self.last_ssot:
            sources["planning"] = self.last_ssot
        tool = getattr(self.loop, "last_tool", None)
        if tool is not None and getattr(tool, "output", ""):
            sources["tool"] = str(tool.output)
        report = ConflictResolutionMatrix().report(sources) if len(sources) >= 2 else {"count": 0, "conflicts": [], "kinds": []}
        self.last_verify = report
        tracker = StateGraphTracker(root)
        tracker.commit(
            f"verify conflicts={report.get('count')}\n{(query or '')[:200]}",
            summary="verify ACRM",
        )
        output = str(getattr(tool, "output", "") or "")
        if tool is not None and (not getattr(tool, "ok", True) or looks_like_compiler_error(output)):
            healer = self.loop.healer
            if healer is None:
                from robber.engine.self_healing import get_healer

                healer = get_healer()
            if healer is not None:
                await healer.heal_from_stderr(output, task_name=getattr(tool, "name", "tool"))
        await self.loop._set_phase("mutate")
        if cards:
            tracker.commit(
                "\n".join(str(item.get("path") or "") for item in cards[:12]),
                summary=f"mutate {len(cards)} file(s)",
            )
        self.loop._graph_public = tracker.public()

    async def on_failure(self, query: str, exc: BaseException) -> None:
        from robber.engine.self_healing import get_healer

        await self.loop._set_phase("heal")
        try:
            inject_error_delta(exc, query, root=self._root())
        except Exception:
            logger.debug("error delta inject skipped")
        healer = self.loop.healer or get_healer()
        if healer is not None and not self.loop.is_autonomous():
            try:
                await healer.heal_exception(exc, task_name="agent_turn")
            except Exception:
                logger.debug("heal_exception skipped")


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
        max_tokens_per_turn: int | None = None,
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
        self.max_tokens_per_turn = (
            MAX_TOKENS_PER_TURN if max_tokens_per_turn is None else int(max_tokens_per_turn)
        )
        self._turn_tokens = 0
        self._budget_exceeded = False
        from robber.router import SessionFailover

        self.session_router = SessionFailover()
        self.cycle = PerceptionActionCycle(self)
        self.last_tool: Any = None
        self._graph_public: dict[str, Any] = {}
        self._stale_locks = 0
        from robber.engine.pacing_engine import PacingEngine

        self.pacing = PacingEngine(self._workspace_root())
        self._horizon_task: asyncio.Task[Any] | None = None
        self._swarm_public: dict[str, Any] = {}
        self._install_observer()

    def _workspace_root(self) -> Path:
        from robber.jail import workspace_root

        raw = (self.state or {}).get("workspace")
        if raw:
            return Path(str(raw)).resolve()
        return workspace_root()

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

    def reset_turn_budget(self) -> None:
        self._turn_tokens = 0
        self._budget_exceeded = False

    def consume_tokens(self, count: int) -> bool:
        self._turn_tokens += max(0, int(count))
        if self._turn_tokens >= self.max_tokens_per_turn:
            self._budget_exceeded = True
            return False
        return True

    def budget_exceeded(self) -> bool:
        return self._budget_exceeded or self._turn_tokens >= self.max_tokens_per_turn

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
            "turn_tokens": self._turn_tokens,
            "max_tokens_per_turn": self.max_tokens_per_turn,
            "token_cap_hit": self._budget_exceeded,
            "session": self.session_router.public(),
            "verify": getattr(self.cycle, "last_verify", {}),
            "tool": getattr(getattr(self, "last_tool", None), "name", None),
            "graph": dict(self._graph_public),
            "stale_locks": self._stale_locks,
            "horizon": self.pacing.job.public() if self.pacing.job is not None else {},
            "swarm": dict(self._swarm_public),
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

        if lowered in {"prompt", "user_prompt", "execute_prompt"}:
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
        await self._emit_state_ack(query)
        if self._turn_lock.locked():
            await self._error(sender, "Agent is busy - wait for the current turn")
            return
        launch = ""
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
                from robber.engine.pacing_engine import looks_like_horizon

                if looks_like_horizon(query, root=self._workspace_root()):
                    launch = query
                else:
                    await self._set_phase("turn")
                    await self._run_agent_turn(query)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("agent loop turn failed (%s)", type(exc).__name__)
                await self._error(None, f"Loop fell back to operator ({type(exc).__name__})")
                if self.hub is not None:
                    await self.hub.broadcast(
                        suite_event("loop_state", f"[LOOP] {self.control_mode} · error: {type(exc).__name__}", self.tree())
                    )
            finally:
                await self._set_phase("idle")
                await self.broadcast_tree()
        if launch:
            job = await self.start_horizon(launch)
            await self._emit_system(
                f"Horizon {job.id} started - {job.total} micro-batches (ingest/analyze/checkpoint/pause)"
            )
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

    async def start_horizon(self, query: str) -> Any:
        if self.pacing.root != self._workspace_root():
            from robber.engine.pacing_engine import PacingEngine

            self.pacing = PacingEngine(self._workspace_root())
        if self._horizon_task is not None and not self._horizon_task.done():
            if self.pacing.job is not None:
                return self.pacing.job
        job = self.pacing.create_job(query)
        self._horizon_task = asyncio.create_task(self._horizon_main(job=job))
        return job

    async def pause_horizon(self) -> Any:
        self.pacing.request_pause()
        job = self.pacing.job
        if job is not None:
            job.status = "paused"
        return job

    async def resume_horizon(self) -> Any:
        job = self.pacing.load_job()
        if job is None:
            return None
        if self._horizon_task is not None and not self._horizon_task.done():
            return job
        job.status = "running"
        self.pacing.pause_requested = False
        self.pacing.job = job
        self._horizon_task = asyncio.create_task(self._horizon_main(job=job))
        return job

    async def _horizon_main(self, query: str = "", *, job: Any = None) -> Any:
        from robber.config import PACING_BATCH_TIMEOUT
        from robber.engine.pacing_engine import BATCH_MARK, _default_analyze

        pacing = self.pacing
        active = job or pacing.job or pacing.create_job(query)
        await self._set_phase("ingest", job=active.id, total=active.total)

        async def analyze(chunk: Any, current: Any) -> str:
            await self._set_phase("analyze", batch=chunk.index, total=current.total)
            prompt = (
                f"{BATCH_MARK} {chunk.index + 1}/{current.total}]\n"
                "Continue long-horizon research. Do not reload the full corpus.\n"
                f"Query: {(current.query or '')[:400]}\n"
                f"Section: {chunk.macro_title} {chunk.path} L{chunk.start}-{chunk.end}\n\n"
                f"{(chunk.text or '')[:8000]}"
            )
            if self._run_turn is None:
                return await _default_analyze(chunk, current)

            async def invoke() -> None:
                await self._run_turn(prompt)

            async with self._turn_lock:
                await asyncio.wait_for(invoke(), timeout=PACING_BATCH_TIMEOUT)
            return str((self.state or {}).get("last_output") or "")[:4000]

        async def nap(seconds: float) -> None:
            await self._set_phase("pause", seconds=seconds)
            await asyncio.sleep(seconds)

        result = await pacing.run(active.query, analyze=analyze, sleep=nap, job=active)
        if result.status == "done":
            await self._set_phase("synthesize")
            await self._emit_system((result.synthesis or "Horizon complete.")[:4000])
        else:
            await self._emit_system(f"Horizon {result.status} at {result.cursor}/{result.total}")
        await self.broadcast_tree()
        return result

    async def request_heal_approval(
        self, diff: str, meta: dict[str, Any] | None = None, *, force: bool = False
    ) -> bool:
        if self.is_autonomous() and not force:
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
        ram: dict[str, Any] = {}
        try:
            from robber.memory.ram_guard import get_ram_guard

            ram = get_ram_guard(self._workspace_root()).inspect()
        except Exception:
            ram = {"ok": True}
        return {
            "ok": ok,
            "control_mode": self.control_mode,
            "phase": self.phase,
            "engines": engines,
            "loop": self.tree(),
            "ram": ram,
            "timestamp": time.time(),
        }

    async def _run_swarm(self, query: str) -> None:
        from robber.agents.swarm_orchestrator import SwarmOrchestrator
        from robber.state_manager import StateManager

        await self._set_phase("swarm")
        orch = SwarmOrchestrator(
            self._workspace_root(),
            manager=StateManager(self._workspace_root()),
            hub=self.hub,
        )
        report = await orch.execute_task_dag(query)
        self._swarm_public = report.public()
        await self.broadcast_tree()

    async def _run_agent_turn(self, query: str) -> None:
        if self._run_turn is None:
            raise RuntimeError("agent turn is not bound")
        from robber.agents.swarm_orchestrator import wants_swarm

        if wants_swarm(query):
            try:
                await self._run_swarm(query)
            except Exception:
                logger.exception("swarm skipped")
        cycle_on = self.hub is not None or bool((self.state or {}).get("amace_cycle"))
        if cycle_on:
            await self.cycle.before_turn(query)

        async def factory() -> None:
            from robber.config import TURN_RETRY_ATTEMPTS, TURN_RETRY_BASE_SECONDS
            from robber.orchestrator import is_transient_error

            last_exc: BaseException | None = None
            for attempt in range(1, TURN_RETRY_ATTEMPTS + 1):
                try:
                    await self._set_phase("dispatch")
                    await self._run_turn(query)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_exc = exc
                    from robber.router.failover import ProviderExhaustionError

                    if isinstance(exc, ProviderExhaustionError):
                        raise
                    if not is_transient_error(exc) or attempt >= TURN_RETRY_ATTEMPTS:
                        raise
                    delay = TURN_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                    logger.info(
                        "turn retry %s/%s after %s (sleep %.1fs)",
                        attempt,
                        TURN_RETRY_ATTEMPTS,
                        type(exc).__name__,
                        delay,
                    )
                    await asyncio.sleep(delay)
            if last_exc is not None:
                raise last_exc

        try:
            if self.is_autonomous() and self.healer is not None:
                await self.healer.run_with_heal("agent_turn", factory)
            else:
                await factory()
            if cycle_on:
                try:
                    await self.cycle.after_turn(query)
                except UnicodeDecodeError:
                    logger.exception("after_turn decode failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if cycle_on:
                await self.cycle.on_failure(query, exc)
            raise

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
        frame = {
            "type": "error",
            "role": "error",
            "content": message,
            "stream_kind": "error",
            "timestamp": time.time(),
        }
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
            event = str((payload or {}).get("event") or "")
            kind = str((payload or {}).get("type") or "")
            if event == "STATE_ACK" or kind in {"message", "stream_token"} or (payload or {}).get("append"):
                await asyncio.sleep(0)

        self.hub.broadcast = observed  # type: ignore[method-assign]

    async def _emit_state_ack(self, query: str = "") -> None:
        self.phase = "routing"
        if self.hub is None:
            return
        frame = state_ack_frame("routing", "Routing to a live provider")
        if query:
            payload = frame.get("payload")
            if isinstance(payload, dict):
                payload["command"] = query[:200]
        try:
            await self.hub.broadcast(frame)
            await asyncio.sleep(0)
        except Exception:
            logger.debug("state ack skipped")


class NonRecoverableError(RuntimeError):
    """Task failed in a way that should roll the state graph back."""


@dataclass
class StepResult:
    output: str = ""
    pause: bool = False
    tokens: int = 0
    ok: bool = True


@dataclass
class LoopReport:
    status: str
    completed: list[str]
    paused: str | None
    failed: list[str]
    steps: int
    pacing: dict[str, Any]

    def public(self) -> dict[str, Any]:
        return asdict(self)


ExecuteStepFn = Callable[[dict[str, Any], Any], Awaitable[StepResult] | StepResult]
_AGENT_WALK = ("idle", "planning", "running", "paused", "completed")
LOOP_WORKER = "autonomous-loop"


def _is_non_recoverable(exc: BaseException) -> bool:
    if isinstance(exc, NonRecoverableError):
        return True
    blob = f"{type(exc).__name__} {exc}".lower()
    return "non-recoverable" in blob or type(exc).__name__ in {"IntegrityError", "SystemError"}


def _agent_path(src: str, dest: str) -> list[str]:
    if src == dest:
        return []
    from collections import deque

    from robber.state_manager import AGENT_TRANSITIONS

    queue: deque[tuple[str, list[str]]] = deque([(src, [])])
    seen = {src}
    while queue:
        node, path = queue.popleft()
        neighbors = AGENT_TRANSITIONS.get(node, frozenset())
        for nxt in _AGENT_WALK:
            if nxt not in neighbors or nxt in seen:
                continue
            trail = path + [nxt]
            if nxt == dest:
                return trail
            seen.add(nxt)
            queue.append((nxt, trail))
    from robber.state_manager import StateTransitionError

    raise StateTransitionError(f"agent {src} -> {dest} is not allowed")


class AutonomousAgentLoop:
    """Pull pending DAG tasks from SSOT, assemble context, execute, checkpoint, resume."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        manager: Any = None,
        planner: Any = None,
        assembler: Any = None,
        pacing: Any = None,
        execute: ExecuteStepFn | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        window: int | None = None,
        model_capacity: int | None = None,
    ) -> None:
        from robber.context_assembly import ContextAssembler
        from robber.engine.pacing_engine import PacingEngine
        from robber.jail import workspace_root
        from robber.planner import AutonomousPlanner
        from robber.state_manager import StateManager

        self.root = Path(root).resolve() if root else workspace_root()
        self.manager = manager or StateManager(self.root)
        self.planner = planner or AutonomousPlanner(self.root, manager=self.manager)
        self.assembler = assembler or ContextAssembler(
            self.root,
            manager=self.manager,
            window=window,
            model_capacity=model_capacity,
        )
        self.pacing = pacing or PacingEngine(self.root)
        self._execute = execute
        self._sleep = sleep or asyncio.sleep
        self.agent: dict[str, Any] | None = None
        self._safe_graph_id: str | None = None
        self.last_context: Any = None
        self.swarm: Any = None

    def request_pause(self) -> None:
        self.pacing.request_pause()

    async def run(self, query: str | None = None, *, resume: bool = False, max_steps: int = 256) -> LoopReport:
        from robber.config import TURN_RETRY_ATTEMPTS, TURN_RETRY_BASE_SECONDS
        from robber.context_assembly import merge_payload
        from robber.orchestrator import is_transient_error
        from robber.state_graph import StateGraphTracker
        from robber.state_manager import COMPLETED, FAILED, IN_PROGRESS, PAUSED, PENDING

        self._ensure_agent()
        if query:
            await self._set_agent("planning", loop_status="plan")
            self.planner.plan(query)
        if resume or query:
            self.pacing.pause_requested = False
            self.pacing.flush_caches()
        await self._set_agent("running", loop_status="resume" if resume else "run")
        completed: list[str] = []
        failed: list[str] = []
        paused_id: str | None = None
        steps = 0
        for _ in range(max(1, int(max_steps))):
            ready = self._ready_tasks()
            if not ready:
                break
            task = ready[0]
            task_id = str(task["id"])
            self.pacing.bind_execution_task(task_id)
            status = str(task.get("status") or PENDING)
            if status in {PAUSED, PENDING}:
                task = self.manager.transition_task(task_id, IN_PROGRESS)
            await self._touch_agent(task_id=task_id, loop_status="dispatch")
            self._checkpoint_if_risky(task)
            context = self.assembler.assemble(task)
            self.last_context = context
            result: StepResult | None = None
            last_exc: BaseException | None = None
            for attempt in range(1, TURN_RETRY_ATTEMPTS + 1):
                try:
                    result = await self._step(task, context)
                    last_exc = None
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_exc = exc
                    if _is_non_recoverable(exc) or not is_transient_error(exc) or attempt >= TURN_RETRY_ATTEMPTS:
                        break
                    await self._sleep(TURN_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
            steps += 1
            if last_exc is not None:
                failed.append(task_id)
                await self._fail_task(task, last_exc, context)
                continue
            assert result is not None
            self.pacing.record_request(result.tokens, heavy=True)
            if not result.ok:
                from robber.tools.dispatcher import looks_like_compiler_error

                output = result.output or ""
                if looks_like_compiler_error(output) and self._heal_compiler(output, task_id):
                    result = await self._step(task, context)
                if not result.ok:
                    failed.append(task_id)
                    await self._fail_task(task, RuntimeError(result.output or "step failed"), context)
                    continue
            if result.pause or self.pacing.should_pause():
                self.pacing.request_pause()
                paused = self.pacing.commit_execution_checkpoint(
                    manager=self.manager,
                    task=self.manager.get_task(task_id) or task,
                    reason="tactical-pause",
                    output=result.output,
                    context=context.text,
                )
                paused_id = str(paused.get("id") or task_id)
                await self._set_agent("paused", task_id=paused_id, loop_status="paused")
                self.pacing.flush_caches()
                wait = self.pacing.cool_down_interval()
                if wait > 0:
                    await self._sleep(wait)
                break
            payload = merge_payload(task.get("payload_json"))
            payload["result"] = (result.output or "")[:4000]
            payload.pop("checkpoint", None)
            self.manager.transition_task(task_id, COMPLETED, payload=payload)
            self.manager.add_artifact(
                str(payload.get("tag") or "step"),
                body=result.output or "ok",
                kind="loop",
                task_id=task_id,
                exit_code=0,
            )
            completed.append(task_id)
            self._summarize_completed(task_id)
            node = StateGraphTracker(self.root).commit(
                f"LOOP DONE {task_id}\n{result.output[:800]}",
                summary=f"loop done {(task.get('title') or '')[:40]}",
            )
            self._safe_graph_id = node.id
            wait = self.pacing.cool_down_interval()
            if wait > 0 and not self.pacing.should_pause():
                await self._sleep(wait)
        leftover = self.manager.list_queue(limit=128)
        if paused_id:
            status_out = PAUSED
        elif self._ready_tasks():
            status_out = "running"
        elif leftover:
            status_out = FAILED
            await self._set_agent("completed", loop_status="blocked")
        else:
            status_out = COMPLETED
            await self._set_agent("completed", loop_status="done")
        return LoopReport(
            status=status_out,
            completed=completed,
            paused=paused_id,
            failed=failed,
            steps=steps,
            pacing=self.pacing.health_snapshot(),
        )

    async def resume(self, *, max_steps: int = 256) -> LoopReport:
        return await self.run(resume=True, max_steps=max_steps)

    def _ready_tasks(self) -> list[dict[str, Any]]:
        from robber.planner import parse_depends
        from robber.state_manager import PAUSED, PENDING

        done = {row["id"] for row in self.manager.list_tasks(status="completed", limit=256)}
        ready: list[dict[str, Any]] = []
        for row in self.manager.list_queue(limit=128):
            status = str(row.get("status") or PENDING)
            if status not in {PENDING, PAUSED}:
                continue
            deps = parse_depends(row.get("depends_on"))
            if all(dep in done for dep in deps):
                ready.append(row)
        ready.sort(
            key=lambda row: (
                0 if str(row.get("status")) == PAUSED else 1,
                -int(row.get("priority") or 0),
                int(row.get("queue_order") or 0),
            )
        )
        return ready

    async def _step(self, task: dict[str, Any], context: Any) -> StepResult:
        from robber.agents.swarm_orchestrator import SwarmOrchestrator
        from robber.agents.sub_agents import ROLE_LABELS, WorkerResult, role_for_task

        plugin_result = self._maybe_plugin_step(task, context)
        if self.swarm is None:
            self.swarm = SwarmOrchestrator(
                self.root,
                manager=self.manager,
                execute=self._execute,
                sleep=self._sleep,
                assembler=self.assembler,
            )
        if plugin_result is not None:
            role = role_for_task(task)
            worker = self.swarm.workers.get(role)
            if worker is not None:
                await worker.emit("plugin")
            self.swarm.hops.append(ROLE_LABELS.get(role, "CODER"))
            return plugin_result
        result = await self.swarm.dispatch(task, context, fallback=self._execute, close=False)
        if isinstance(result, WorkerResult):
            return StepResult(output=result.output, pause=result.pause, tokens=result.tokens, ok=result.ok)
        return StepResult(output=str(result or ""))

    def _maybe_plugin_step(self, task: dict[str, Any], context: Any) -> StepResult | None:
        from robber.context_assembly import merge_payload
        from robber.plugins.plugin_loader import get_loader

        payload = merge_payload(task.get("payload_json"))
        name = str(payload.get("plugin") or "").strip()
        if not name:
            loader = get_loader(self.root)
            for item in payload.get("tools") or []:
                key = str(item or "").strip()
                if key and loader.get(key) is not None:
                    name = key
                    break
        if not name:
            return None
        result = get_loader(self.root).execute(
            name,
            {},
            task_id=str(task.get("id") or "") or None,
        )
        return StepResult(output=result.output, tokens=getattr(context, "tokens", 0) or 0, ok=result.ok)

    def _checkpoint_if_risky(self, task: dict[str, Any]) -> None:
        from robber.context_assembly import merge_payload

        payload = merge_payload(task.get("payload_json"))
        tag = str(payload.get("tag") or "")
        kind = str(payload.get("task_type") or "")
        if tag not in {"mutate", "fix"} and kind != "CODE_GEN":
            return
        files: list[str] = []
        for key in ("outputs", "inputs", "files"):
            items = payload.get(key) or []
            if isinstance(items, list):
                files.extend(str(item) for item in items if item)
        try:
            from robber.engine.recovery import SnapshotRecoveryEngine

            SnapshotRecoveryEngine(self.root, manager=self.manager).snapshot(
                label=f"pre-{tag or kind or 'mutate'}",
                files=files[:12],
                reason="high-risk",
            )
        except Exception:
            logger.debug("pre-mutation snapshot skipped")

    def _heal_compiler(self, output: str, task_id: str) -> bool:
        try:
            from robber.engine.self_healing import SelfHealingEngine, get_healer

            healer = get_healer() or SelfHealingEngine(
                hub=None,
                root=self.root,
                manager=self.manager,
            )
            return healer.repair_sync(output, task_name=f"loop:{task_id}")
        except Exception:
            logger.debug("loop self-heal skipped")
            return False

    def _summarize_completed(self, task_id: str) -> None:
        try:
            from robber.memory.context_compressor import ContextCompressor

            ContextCompressor(self.root, manager=self.manager).summarize_task(task_id, force=True)
            try:
                from robber.memory.ram_guard import get_ram_guard

                get_ram_guard(self.root).maybe_reclaim()
            except Exception:
                pass
        except Exception:
            logger.debug("task summary skipped")

    async def _fail_task(self, task: dict[str, Any], exc: BaseException, context: Any) -> None:
        from robber.conflict_resolver import ConflictResolutionMatrix
        from robber.context_assembly import merge_payload
        from robber.handoff_ledger import HandoffLedger
        from robber.state_graph import StateGraphTracker
        from robber.state_manager import FAILED

        task_id = str(task.get("id") or "")
        detail = f"{type(exc).__name__}: {exc}"
        payload = merge_payload(task.get("payload_json"))
        payload["error"] = detail[:1200]
        try:
            self.manager.transition_task(task_id, FAILED, payload=payload)
        except Exception:
            logger.debug("fail transition skipped")
        self.manager.record_decision(
            f"conflict {task_id}",
            rationale=detail,
            task_id=task_id,
            branch="loop",
            version_tag="conflict",
        )
        report = ConflictResolutionMatrix().report({"loop": detail, "context": (context.text if context else "")[:800]})
        HandoffLedger(self.root).post(
            f"LOOP FAIL {task_id}\n{detail}\nconflicts={report.get('count')}",
            source="loop",
            target="coding",
            kind="loop_error",
        )
        tracker = StateGraphTracker(self.root)
        tracker.commit(f"LOOP FAIL {task_id}\n{detail}", summary="loop fail")
        if _is_non_recoverable(exc) and self._safe_graph_id:
            tracker.rollback(self._safe_graph_id)

    def _ensure_agent(self) -> dict[str, Any]:
        rows = self.manager.list_agents(worker_id=LOOP_WORKER, limit=4)
        if rows:
            self.agent = rows[0]
            return rows[0]
        self.agent = self.manager.put_agent(LOOP_WORKER, phase="idle", loop_status="idle")
        return self.agent

    async def _set_agent(self, phase: str, *, task_id: str | None = None, loop_status: str = "") -> None:
        from robber.state_manager import normalize_phase

        row = self._ensure_agent()
        current = normalize_phase(str(row.get("phase") or "idle"), kind="agent")
        dest = normalize_phase(phase, kind="agent")
        for nxt in _agent_path(current, dest):
            row = self.manager.transition_agent(
                str(row["id"]),
                nxt,
                loop_status=loop_status or nxt,
                task_id=task_id,
            )
        if dest == current and (task_id is not None or loop_status):
            row = self.manager.put_agent(
                str(row.get("worker_id") or LOOP_WORKER),
                language=str(row.get("language") or "python"),
                phase=current,
                loop_status=loop_status or str(row.get("loop_status") or current),
                task_id=task_id if task_id is not None else row.get("task_id"),
                telemetry=row.get("telemetry_json"),
                agent_id=str(row["id"]),
            )
        self.agent = row

    async def _touch_agent(self, *, task_id: str, loop_status: str) -> None:
        await self._set_agent("running", task_id=task_id, loop_status=loop_status)
