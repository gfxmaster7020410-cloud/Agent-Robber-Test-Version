import asyncio
import json
import os
import shutil
import time
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError

import robber.api.fb_shark as fb_shark
from robber.api import memo
from robber.engine.vault import SnapshotVaultManager
from robber.engine.proxy_rotator import ProxyConfig, ProxyManager
from robber.engine.session_manager import SessionManager
from robber.engine.worker_pool import WorkerPool
from robber.engine.command_router import CommandRouter
from robber.db.database import Database
from robber.engine.account_manager import AccountManager

from robber.engine.self_healing import SelfHealingEngine, bind_healer
from robber.engine.model_manager import CustomModelManager
from robber.engine.presets import batch_to_tier, preset_public
import robber.commands.handlers  # noqa: F401 — registers slash commands
from robber.commands.registry import ExecutionContext, get_registry, parse_slash

from robber.engine.vision_engine import VisionInspector
from robber.tools.patch_applier import JailedPatchApplicator
from robber.agent_loop import AgentLoopManager, bind_loop
from robber.canvas import build_canvas

# Load environment variables from .env before initializing engines
load_dotenv()

# App Initialization
app = FastAPI(title="Agent Robber API Center", version="1.0.0")

class Hub:
    def __init__(self, session_manager: SessionManager) -> None:
        self._clients: dict[WebSocket, asyncio.Queue] = {}
        self._session_manager = session_manager
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def connect(self, client: WebSocket) -> None:
        await client.accept()
        async with self._lock:
            self._clients[client] = asyncio.Queue()
        # Start background sender for this client
        asyncio.create_task(self._client_sender(client))

    async def _client_sender(self, client: WebSocket):
        queue = self._clients.get(client)
        if not queue:
            return
        try:
            while True:
                payload = await queue.get()
                await client.send_text(json.dumps(payload))
                queue.task_done()
        except Exception:
            await self.disconnect(client)

    async def disconnect(self, client: WebSocket) -> None:
        async with self._lock:
            if client in self._clients:
                del self._clients[client]

    async def send_to(self, client: WebSocket, payload: dict[str, Any]) -> None:
        async with self._lock:
            queue = self._clients.get(client)
        if queue:
            await queue.put(payload)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            queues = list(self._clients.values())
        for queue in queues:
            await queue.put(payload)












# CORS Middleware Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
        "*",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Router Inclusion
app.include_router(memo.router)
app.include_router(fb_shark.router)

session_manager = SessionManager()
hub = Hub(session_manager)
snapshot_vault = SnapshotVaultManager(hub)
proxy_manager = ProxyManager()
database = Database()
custom_model_manager = CustomModelManager(database)
account_manager = AccountManager(database, proxy_manager)
patch_applier = JailedPatchApplicator()
vision_inspector = VisionInspector()
self_healing = SelfHealingEngine(hub, patch_applier)
bind_healer(self_healing)
# worker_pool = WorkerPool(
#     hub,
#     session_manager,
#     snapshot_vault,
#     proxy_manager,
#     account_manager,
#     vision_inspector=vision_inspector,
#     healer=self_healing,
# )
worker_pool = None


from robber.catalog import (
    add_model,
    add_models_from_tags,
    catalog_snapshot,
    consensus_labels,
    infer_litellm_model,
    load_catalog,
    patch_model,
    providers_status,
    save_consensus,
    upsert_key,
    upsert_named,
)
from robber.core.v_ssot_engine import VirtualSSOTEngine
from robber.engine.gemini_stream import GeminiStreamEngine
from robber.engine.instruction_interceptor import InstructionInterceptor
from robber.engine.model_router import ModelRouter
from robber.health import probe_engine, scan_ollama_tags
from robber.jail import workspace_root
from robber.notes import add_note, load_notes, purge_notes, search_notes
from robber.prefs import CONTROL_MODES, load_batch, load_context_window, load_theme, save_batch, save_context_window, save_theme
from robber.router import (
    HYBRID_LABEL,
    MAX_CONSENSUS,
    MODEL_META,
    Engine,
    collect_completion,
    display_models,
    resolve_engine,
    resolve_live_engines,
    stream_completion,
    worker_rows,
)
from robber.mcp.host import McpHost, bind_mcp, get_mcp_host
from robber.ssot import get_index
from robber.ssot_context import (
    DEFAULT_CONTEXT_WINDOW,
    format_window,
    pack_ssot_context,
    parse_window,
    retrieval_k,
    window_public,
)
from robber.ssot_watch import SsotWatcher, bind_ssot_watcher, get_ssot_watcher
from robber.terminal import TerminalSupervisor
from robber.tools import ToolResult, execute_tool, select_tool

v_ssot_engine = VirtualSSOTEngine()
gemini_stream_engine = GeminiStreamEngine()
command_router = CommandRouter(session_manager, gemini_stream_engine)
model_router = ModelRouter()
instruction_interceptor = InstructionInterceptor()

load_catalog()

MODES: tuple[str, ...] = ("plan", "act", "review")

THINKING_LABEL = "Thinking & querying memory..."
MEMORY_LABEL = "Recalling SSOT context..."
TOOL_LABEL = "Executing tool..."
STREAM_LABEL = "Streaming model..."

FLUSH_CHARS = 32
FLUSH_SECONDS = 0.05


def now() -> float:
    return time.time()


class Hub:
    def __init__(self, session_manager: SessionManager) -> None:
        self._clients: dict[WebSocket, asyncio.Queue] = {}
        self._session_manager = session_manager
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def connect(self, client: WebSocket) -> None:
        await client.accept()
        async with self._lock:
            self._clients[client] = asyncio.Queue()
        # Start background sender for this client
        asyncio.create_task(self._client_sender(client))

    async def _client_sender(self, client: WebSocket):
        queue = self._clients.get(client)
        if not queue:
            return
        try:
            while True:
                payload = await queue.get()
                await client.send_text(json.dumps(payload))
                queue.task_done()
        except Exception:
            await self.disconnect(client)

    async def disconnect(self, client: WebSocket) -> None:
        async with self._lock:
            if client in self._clients:
                del self._clients[client]

    async def send_to(self, client: WebSocket, payload: dict[str, Any]) -> None:
        async with self._lock:
            queue = self._clients.get(client)
        if queue:
            await queue.put(payload)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            queues = list(self._clients.values())
        for queue in queues:
            await queue.put(payload)


supervisor = TerminalSupervisor(root=workspace_root())
mcp_host = bind_mcp(McpHost())
mcp_host.bind(supervisor)
# ssot_watcher = bind_ssot_watcher(SsotWatcher())
ssot_watcher = None


state: dict[str, Any] = {
    "mode": "act",
    "model": display_models()[0],
    "busy": False,
    "engine": "fallback",
    "live": False,
    "theme": load_theme(),
    "batch": load_batch(),
    "ttft_ms": 0,
    "tps": 0.0,
    "context_window": load_context_window(),
    "context_packed": 0,
    "context_hits": 0,
    "harness": None,
    "notify_on_complete": False,
    "force_kind": None,
}
state["supervisor"] = supervisor
turn_lock = asyncio.Lock()

agent_loop = AgentLoopManager(
    hub,
    command_router=command_router,
    worker_pool=worker_pool,
    proxy_manager=proxy_manager,
    vault=snapshot_vault,
    vision=vision_inspector,
    healer=self_healing,
    session_manager=session_manager,
    account_manager=account_manager,
    database=database,
    state=state,
)
bind_loop(agent_loop)
state["control_mode"] = agent_loop.control_mode


class SetModeMessage(BaseModel):
    type: str = "set_mode"
    mode: str


class SetModelMessage(BaseModel):
    type: str = "set_model"
    model: str


class PromptMessage(BaseModel):
    type: str = "prompt"
    text: str


class PurgeMessage(BaseModel):
    type: str = "purge"
    pid: int | None = None


class MemoryQuery(BaseModel):
    type: str = "memory_query"
    text: str = ""


class McpIdMessage(BaseModel):
    type: str = "mcp_enable"
    id: str = ""
    server: str = ""


class McpAddMessage(BaseModel):
    type: str = "mcp_add"
    id: str
    label: str = ""
    command: str
    args: list[str] = Field(default_factory=list)
    env_keys: list[str] = Field(default_factory=list)
    description: str = ""
    enabled: bool = False


class McpCallMessage(BaseModel):
    type: str = "mcp_call"
    server: str
    tool: str
    arguments: dict[str, Any] | str | None = None


class AddModelMessage(BaseModel):
    type: str = "add_model"
    label: str = ""
    provider: str
    litellm_model: str = ""
    api_key: str | None = None
    provider_type: str = "native"
    base_url: str = ""
    batch_class: str = "normal"
    context_length: int = 8192
    fallback_label: str = ""
    fallback_model_id: str = ""
    custom_headers: str | dict[str, str] | None = None
    ollama_models: list[str] = Field(default_factory=list)
    active: bool = True


class PatchModelMessage(BaseModel):
    type: str = "patch_model"
    label: str
    batch_class: str | None = None
    fallback_label: str | None = None
    fallback_model_id: str | None = None
    active: bool | None = None
    context_length: int | None = None


class SetBatchMessage(BaseModel):
    type: str = "set_batch"
    batch: str


class SetThemeMessage(BaseModel):
    type: str = "set_theme"
    theme: str


class ScanOllamaMessage(BaseModel):
    type: str = "scan_ollama"
    host: str = "http://127.0.0.1:11434"


class PingModelMessage(BaseModel):
    type: str = "ping_model"
    label: str


class SetKeyMessage(BaseModel):
    type: str = "set_key"
    provider: str
    secret: str


class SetConsensusMessage(BaseModel):
    type: str = "set_consensus"
    models: list[str]


class MemoryInject(BaseModel):
    type: str = "memory_inject"
    key: str
    value: str
    category: str = "user"
    tags: list[str] = Field(default_factory=list)


class MemoryPurge(BaseModel):
    type: str = "memory_purge"


class ProxySpec(BaseModel):
    id: str | None = None
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    protocol: str = "http"


class AddAccountRequest(BaseModel):
    profile_id: str | None = None
    username: str
    password: str | None = None
    status: str = "idle"
    proxy_id: str | None = None
    proxy: ProxySpec | None = None


class UserPromptMessage(BaseModel):
    action: str = "USER_PROMPT"
    prompt: str
    mode: str = "Coder"
    active_file: str = ""
    pinned_anchors: list[str] = Field(default_factory=list)


class InjectInstructionMessage(BaseModel):
    action: str = "INJECT_INSTRUCTION"
    instruction: str


class SetActiveWindowMessage(BaseModel):
    action: str = "SET_ACTIVE_WINDOW"
    type: str = "set_context_window"
    limit: object | None = None
    window: object | None = None


class SsotWindowBody(BaseModel):
    window: object | None = None
    limit: object | None = None


class OrchModelIn(BaseModel):
    id: str | None = None
    name: str
    provider_preset: str = "CUSTOM"
    protocol_type: str = "OPENAI_COMPATIBLE"
    base_url: str = ""
    model_string: str = ""
    api_key: str | None = None
    custom_headers: dict[str, str] | str | None = None
    context_length: int = 128000
    fallback_model_id: str | None = None
    is_active: bool = True
    latency_ms: int = 0
    cost_per_1m: float = 0.0
    temperature: float = 0.2


class OrchModelPatch(BaseModel):
    name: str | None = None
    provider_preset: str | None = None
    protocol_type: str | None = None
    base_url: str | None = None
    model_string: str | None = None
    api_key: str | None = None
    custom_headers: dict[str, str] | str | None = None
    context_length: int | None = None
    fallback_model_id: str | None = None
    is_active: bool | None = None
    latency_ms: int | None = None
    cost_per_1m: float | None = None
    temperature: float | None = None


class ComboIn(BaseModel):
    id: str | None = None
    name: str
    tier: str = "BALANCED"
    reasoning_model_id: str | None = None
    coding_model_ids: list[str] = Field(default_factory=list)
    general_model_ids: list[str] = Field(default_factory=list)
    is_active: bool = True


class ComboPatch(BaseModel):
    name: str | None = None
    tier: str | None = None
    reasoning_model_id: str | None = None
    coding_model_ids: list[str] | None = None
    general_model_ids: list[str] | None = None
    is_active: bool | None = None


class RouteModeIn(BaseModel):
    mode: str


class SlashCommandMessage(BaseModel):
    type: str = "slash_command"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    session_id: str = "current_session"
    text: str | None = None


def ssot_payload() -> dict[str, Any]:
    payload = dict(get_index().stats)
    payload.update(get_ssot_watcher().public_stats())
    payload.update(
        window_public(
            int(state.get("context_window") or DEFAULT_CONTEXT_WINDOW),
            packed=int(state.get("context_packed") or 0),
            hit_count=int(state.get("context_hits") or 0),
        )
    )
    return payload


async def persist_context_window(raw: object) -> int:
    value = parse_window(raw)
    save_context_window(value)
    try:
        await database.set_ssot_window(value)
    except Exception:
        pass
    state["context_window"] = value
    v_ssot_engine.set_active_window_limit(value)
    return value


async def remember_packed(packed) -> None:
    state["context_packed"] = int(packed.packed)
    state["context_hits"] = int(packed.hit_count)
    try:
        await database.set_ssot_packed(packed.packed, packed.hit_count)
    except Exception:
        pass


def snapshot() -> dict[str, Any]:
    batch = str(state.get("batch") or load_batch())
    workers = worker_rows(state["model"], batch=batch)
    live_workers = [row for row in workers if row["live"] is True]
    engine = resolve_engine(state["model"], batch=batch)
    if len(live_workers) > 1:
        engine_label = f"parallel:{len(live_workers)}"
    else:
        engine_label = engine.litellm_model or "fallback"
    return {
        "type": "state",
        "mode": state["mode"],
        "model": state["model"],
        "busy": state["busy"],
        "engine": engine_label,
        "live": len(live_workers) > 0,
        "theme": state["theme"],
        "batch": batch,
        "workers": workers,
        "keys": providers_status(),
        "clients": hub.client_count,
        "modes": list(MODES),
        "models": display_models(),
        "catalog": catalog_snapshot(),
        "orch_models": custom_model_manager.models_cache,
        "combos": custom_model_manager.combos_cache,
        "route_mode": custom_model_manager.route_mode,
        "presets": preset_public(),
        "slash_commands": get_registry().catalog(),
        "harness": state.get("harness"),
        "consensus": consensus_labels(),
        "notes": load_notes(),
        "ttft_ms": state["ttft_ms"],
        "tps": state["tps"],
        "workspace": str(workspace_root()),
        "ssot": ssot_payload(),
        "jobs": supervisor.snapshot(),
        "control_mode": agent_loop.control_mode,
        "control_modes": list(CONTROL_MODES),
        "loop": agent_loop.tree(),
        "mcp": get_mcp_host().snapshot(),
        "context_window": int(state.get("context_window") or DEFAULT_CONTEXT_WINDOW),
        "context_packed": int(state.get("context_packed") or 0),
        "context_hits": int(state.get("context_hits") or 0),
        "pending_injects": instruction_interceptor.pending_count(),
        "timestamp": now(),
    }


def compose_reply(query: str, tool: ToolResult, memory: str | None = None) -> str:
    short = query if len(query) <= 64 else f"{query[:61]}…"
    mark = "ok" if tool.ok else "blocked"
    preview_lines = tool.output.splitlines()[:6]
    preview = "\n".join(preview_lines)
    if len(tool.output.splitlines()) > 6:
        preview += "\n…"
    parts = [
        f'Working on "{short}". Here is the current picture:',
        "",
        f"• Ran {tool.name} [{mark}] inside the workspace jail",
        "",
        preview,
    ]
    if memory:
        summary = memory.splitlines()[0]
        parts.extend(["", f"• {summary}"])
    parts.extend(
        [
            "",
            "Proposed next step:",
            "```ts",
            "// agent plan preview",
            "const plan = await robber.plan({",
            f"    goal: {json.dumps(short)},",
            '    memoryScope: "project",',
            "});",
            "await robber.execute(plan);",
            "```",
            "",
            "Say the word and I will execute it — or type /help for commands.",
        ]
    )
    return "\n".join(parts)


def keys_report() -> str:
    rows = providers_status()
    configured = [name for name, ok in rows.items() if ok]
    lines = ["BYOK vault (values never printed):"]
    for name, ok in rows.items():
        mark = "ready" if ok else "missing"
        lines.append(f"  {name:<10} {mark}")
    if not configured:
        lines.append("")
        lines.append("No keys loaded. Copy .env.example to .env and restart the daemon.")
    return "\n".join(lines)


async def emit(role: str, content: str, *, append: bool = False) -> None:
    if role == "assistant":
        previous = str(state.get("last_output") or "")
        state["last_output"] = (previous + content) if append else content
    elif role == "reasoning":
        previous = str(state.get("last_reasoning") or "")
        state["last_reasoning"] = (previous + content) if append else content
    elif role == "tool":
        state["last_tool"] = content
    await hub.broadcast(
        {
            "type": "message",
            "role": role,
            "content": content,
            "append": append,
            "timestamp": now(),
        }
    )


class SlashHost:
    """Daemon-side host for slash handlers (never imported by handlers)."""

    async def emit(self, role: str, content: str) -> None:
        await emit(role, content)

    async def broadcast(self) -> None:
        await hub.broadcast(snapshot())

    async def set_turn_mode(self, mode: str) -> str:
        if mode not in MODES:
            raise ValueError(f"Unknown mode '{mode}'")
        state["mode"] = mode
        return mode

    async def set_control_mode(self, mode: str) -> str:
        value = await agent_loop.set_control_mode(mode)
        state["control_mode"] = value
        return value

    async def set_model(self, label: str) -> str:
        names = self.list_models()
        if label not in names:
            raise ValueError(f"Unknown model '{label}'")
        state["model"] = label
        return label

    async def set_route(self, mode: str) -> str:
        state["batch"] = save_batch(mode)
        return custom_model_manager.set_route_mode(state["batch"])

    def inject(self, instruction: str) -> None:
        instruction_interceptor.inject_instruction(instruction)

    def list_models(self) -> list[str]:
        names = list(display_models())
        for row in custom_model_manager.models_cache:
            name = str(row.get("name") or "")
            if name and name not in names:
                names.append(name)
        return names

    def mcp_catalog(self) -> str:
        return get_mcp_host().format_catalog()

    async def mcp_enable(self, server_id: str) -> None:
        await get_mcp_host().enable(server_id)

    async def mcp_disable(self, server_id: str) -> None:
        await get_mcp_host().disable(server_id)

    def pack_digest(self, query: str, window: int) -> str:
        packed = pack_ssot_context(query, window=window)
        return packed.text

    async def persist_window(self, raw: object) -> int:
        return await persist_context_window(raw)

    async def run_argv(self, argv: list[str], timeout: float = 30.0) -> tuple[int, str]:
        result = await supervisor.run(argv, timeout=timeout)
        return result.exit_code, result.output

    async def start_argv(self, argv: list[str]) -> int:
        job = await supervisor.start(argv)
        return job.pid


slash_host = SlashHost()


async def run_slash_command(
    *,
    command: str,
    args: list[str],
    session_id: str,
    sender: WebSocket | None = None,
) -> dict[str, Any]:
    name = (command or "").strip().lower().lstrip("/")
    ctx = ExecutionContext(
        args=list(args),
        session_id=session_id or "current_session",
        workspace=workspace_root(),
        state=state,
        host=slash_host,
    )
    result = await get_registry().dispatch(name, ctx)
    frame = {
        "type": "slash_result",
        "command": name,
        "ok": result.ok,
        "content": result.content,
        "ephemeral": result.ephemeral,
        "extra": result.extra,
        "timestamp": now(),
    }
    if result.ephemeral:
        if sender is not None:
            await hub.send_to(sender, frame)
        else:
            await emit("system", f"[btw] {result.content}")
        return frame
    await emit("system" if result.ok else "error", result.content)
    if sender is not None:
        await hub.send_to(sender, frame)
    await hub.broadcast(snapshot())
    return frame


async def flush_stream(role: str, started: dict[str, bool], chunk: str) -> None:
    if not chunk:
        return
    if started[role]:
        await emit(role, chunk, append=True)
    else:
        await emit(role, chunk, append=False)
        started[role] = True


async def stream_model(
    query: str, mode: str, engine: Engine, tool_context: str | None = None
) -> bool:
    """Return True if at least one assistant token arrived."""
    buffers = {"reasoning": "", "assistant": ""}
    started = {"reasoning": True, "assistant": False}  # canvas already seeded
    last_flush = time.monotonic()
    first_token_at: float | None = None
    chars = 0
    t0 = time.monotonic()

    try:
        async for role, delta in stream_completion(engine, query, mode, tool_context):
            if first_token_at is None:
                first_token_at = time.monotonic()
                state["ttft_ms"] = int((first_token_at - t0) * 1000)
            chars += len(delta)
            buffers[role] = buffers.get(role, "") + delta
            elapsed = time.monotonic() - last_flush
            ready = role in buffers and (
                len(buffers[role]) >= FLUSH_CHARS or elapsed >= FLUSH_SECONDS
            )
            if ready:
                await flush_stream(role, started, buffers[role])
                buffers[role] = ""
                last_flush = time.monotonic()
    except Exception as exc:
        await emit("error", f"Model router failed — falling back. ({type(exc).__name__})")
        return started["assistant"]

    for role, buf in buffers.items():
        if buf:
            await flush_stream(role, started, buf)

    elapsed = time.monotonic() - t0
    if chars and elapsed > 0:
        state["tps"] = round(chars / elapsed, 1)
    return started["assistant"]


async def pick_healthy_engine(engine: Engine, used: set[str]) -> Engine | None:
    """Probe the primary, then one fallback hop. Never loops."""
    if engine.label not in used and await probe_engine(engine):
        return engine
    fallback_name = (engine.fallback_label or "").strip()
    if not fallback_name or fallback_name in used or fallback_name == engine.label:
        return None
    fallback = resolve_engine(fallback_name)
    if fallback.label in used:
        return None
    if await probe_engine(fallback):
        await emit("system", f"{engine.label} unhealthy — failing over to {fallback.label}")
        return fallback
    await emit("system", f"{engine.label} endpoint failed health check")
    return None


async def stream_with_failover(
    query: str, mode: str, engine: Engine, tool_context: str | None = None
) -> bool:
    used: set[str] = set()
    current = engine
    for _ in range(2):
        chosen = await pick_healthy_engine(current, used)
        if chosen is None:
            return False
        used.add(chosen.label)
        produced = await stream_model(query, mode, chosen, tool_context)
        if produced:
            return True
        nxt = (chosen.fallback_label or "").strip()
        if not nxt or nxt in used:
            return False
        await emit("system", f"{chosen.label} produced no tokens — failing over to {nxt}")
        current = resolve_engine(nxt)
    return False


async def healthy_workers(engines: list[Engine]) -> list[Engine]:
    picked: list[Engine] = []
    seen: set[str] = set()
    for engine in engines:
        chosen = await pick_healthy_engine(engine, seen)
        if chosen is None or chosen.label in seen:
            continue
        seen.add(chosen.label)
        picked.append(chosen)
    return picked


async def stream_parallel(
    engines: list[Engine],
    query: str,
    mode: str,
    tool_context: str | None = None,
) -> bool:
    """Run up to 10 live models on the same prompt + SSOT + tool context."""
    await emit(
        "system",
        f"Parallel consensus: {len(engines)} models on this task (shared SSOT).",
    )
    gathered = await asyncio.gather(
        *[collect_completion(engine, query, mode, tool_context) for engine in engines],
        return_exceptions=True,
    )
    produced = False
    t0 = time.monotonic()
    chars = 0
    for engine, item in zip(engines, gathered, strict=True):
        if isinstance(item, BaseException):
            await emit("error", f"{engine.label} failed ({type(item).__name__})")
            continue
        _reasoning, text = item
        chars += len(text)
        if not text.strip():
            await emit("system", f"▸ {engine.label} — empty")
            continue
        produced = True
        await emit("system", f"▸ {engine.label}")
        await emit("assistant", text)
    elapsed = time.monotonic() - t0
    if chars and elapsed > 0:
        state["tps"] = round(chars / elapsed, 1)
    return produced


async def run_turn(query: str) -> None:
    state["busy"] = True
    state["ttft_ms"] = 0
    state["tps"] = 0.0
    await hub.broadcast(snapshot())
    await emit("user", query)

    engine = resolve_engine(state["model"], batch=str(state.get("batch") or ""))
    workers = resolve_live_engines(state["model"], batch=str(state.get("batch") or ""))
    combo_pool = await custom_model_manager.engines_for_prompt(
        ("architecture design reason " + query) if state.get("force_kind") == "reasoning" else query,
        mode=str(state.get("mode") or "act"),
        tier=batch_to_tier(str(state.get("batch") or "normal")),
    )
    if state.get("force_kind") == "reasoning":
        state["force_kind"] = None
    combo_failover = False
    if combo_pool:
        workers = combo_pool
        combo_failover = True
        kind = custom_model_manager.classify_task(query, str(state.get("mode") or "act"))
        await emit("system", f"Combo {custom_model_manager.route_mode} · {kind} · {len(workers)} in failover pool")
    state["engine"] = (
        f"parallel:{len(workers)}" if len(workers) > 1 and not combo_failover else (engine.litellm_model or "fallback")
    )
    if combo_pool:
        state["engine"] = f"combo:{custom_model_manager.route_mode.lower()}:{workers[0].label}"
    state["live"] = len(workers) > 0

    try:
        await hub.broadcast({"type": "busy", "kind": "thinking", "label": THINKING_LABEL})
        tool_name = select_tool(query)
        index = get_index()
        canvas = build_canvas(
            query,
            mode=state["mode"],
            engine=state["engine"],
            tool=tool_name,
            live=state["live"],
            ssot=f"{index.stats['files']} files / {index.stats['chunks']} chunks",
        )
        if len(workers) > 1:
            names = ", ".join(item.label for item in workers)
            canvas = f"{canvas}\nparallel {len(workers)}/{MAX_CONSENSUS}: {names}"
        await emit("reasoning", canvas)
        await asyncio.sleep(0.65)

        await hub.broadcast({"type": "busy", "kind": "memory", "label": MEMORY_LABEL})
        window = int(state.get("context_window") or DEFAULT_CONTEXT_WINDOW)
        directives = instruction_interceptor.consume_pending_instructions()
        packed = await asyncio.to_thread(
            pack_ssot_context,
            query,
            window=window,
            extra_directives=directives,
        )
        await remember_packed(packed)
        memory_blob = packed.text
        hits = packed.hits
        await emit(
            "memory",
            f"SSOT context {packed.packed:,} / {format_window(window)} · {packed.hit_count} hits",
        )
        await hub.broadcast(
            {
                "type": "memory_hits",
                "query": query,
                "hits": [
                    {
                        "path": hit.path,
                        "kind": hit.kind,
                        "start": hit.start,
                        "end": hit.end,
                        "score": hit.score,
                        "preview": hit.preview,
                    }
                    for hit in hits
                ],
                "window": packed.configured,
                "packed": packed.packed,
                "timestamp": now(),
            }
        )
        await asyncio.sleep(0.42)

        await hub.broadcast({"type": "busy", "kind": "tool", "label": TOOL_LABEL})
        await emit("tool", tool_name)
        result = await execute_tool(query, supervisor)
        await emit("tool", f"{result.name} {'ok' if result.ok else 'blocked'}")
        await asyncio.sleep(0.72)

        mcp_blob = get_mcp_host().prompt_catalog()
        tool_context = f"{memory_blob}\n\n{mcp_blob}\n\n{result.name}\n{result.output}"
        routed = await healthy_workers(workers)
        if combo_failover and routed:
            await hub.broadcast({"type": "busy", "kind": "thinking", "label": STREAM_LABEL})
            produced = False
            for item in routed:
                produced = await stream_with_failover(query, state["mode"], item, tool_context)
                if produced:
                    break
                await emit("system", f"{item.label} produced no tokens — next combo member")
            if not produced:
                await emit("assistant", compose_reply(query, result, memory_blob))
        elif len(routed) > 1:
            await hub.broadcast(
                {
                    "type": "busy",
                    "kind": "thinking",
                    "label": f"Streaming {len(routed)} models in parallel...",
                }
            )
            produced = await stream_parallel(routed, query, state["mode"], tool_context)
            if not produced:
                await emit("assistant", compose_reply(query, result, memory_blob))
        elif len(routed) == 1:
            await hub.broadcast({"type": "busy", "kind": "thinking", "label": STREAM_LABEL})
            produced = await stream_with_failover(query, state["mode"], routed[0], tool_context)
            if not produced:
                await emit("assistant", compose_reply(query, result, memory_blob))
        else:
            await emit("assistant", compose_reply(query, result, memory_blob))

        await emit("success", "Turn complete")
    finally:
        state["busy"] = False
        if state.get("notify_on_complete"):
            state["notify_on_complete"] = False
            from robber.commands.handlers import _notify_os

            await _notify_os("Agent Robber", "Turn complete")
        await hub.broadcast(snapshot())


def _mcp_arguments(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"text": value, "query": value}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {}


async def _mcp_emit_result(sender: WebSocket, result: Any) -> None:
    await emit("tool", f"mcp__{result.server}__{result.tool} {'ok' if result.ok else 'blocked'}")
    await emit("assistant" if result.ok else "error", result.text)
    await hub.send_to(
        sender,
        {
            "type": "mcp_result",
            "ok": result.ok,
            "server": result.server,
            "tool": result.tool,
            "content": result.text,
            "timestamp": now(),
        },
    )
    await hub.broadcast(snapshot())


def error_frame(message: str) -> dict[str, Any]:
    return {"type": "error", "content": message, "timestamp": now()}


async def dispatch(sender: WebSocket, raw: str) -> None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await hub.send_to(sender, error_frame("Malformed JSON"))
        return

    if not isinstance(data, dict):
        await hub.send_to(sender, error_frame("Payload must be a JSON object"))
        return

    message_type = data.get("type") or data.get("action")

    if message_type == "USER_PROMPT":
        try:
            message = UserPromptMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid USER_PROMPT: {exc.errors()[0]['msg']}"))
            return
        
        payload = v_ssot_engine.build_context_payload(message.prompt, message.pinned_anchors)
        directives = instruction_interceptor.consume_pending_instructions()
        config = model_router.get_route_config(message.mode)
        
        system_instr = f"{config.get('default_system_prompt', '')}\n\n{directives}\n\nContext:\n{payload['payload_text']}"
        
        async for chunk in gemini_stream_engine.stream_completion(
            message.prompt, 
            system_instruction=system_instr, 
            model=config.get("model", "gemini-3.6-flash"),
            temperature=config.get("temperature", 0.2)
        ):
            await sender.send_json({"type": "STREAM_CHUNK", "content": chunk})
            
        await sender.send_json({"type": "STREAM_END"})
        return

    elif message_type == "INJECT_INSTRUCTION":
        try:
            message = InjectInstructionMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid INJECT_INSTRUCTION: {exc.errors()[0]['msg']}"))
            return
        instruction_interceptor.inject_instruction(message.instruction)
        await sender.send_json({"type": "ACK", "message": "Instruction injected"})
        return

    elif message_type in {"SET_ACTIVE_WINDOW", "set_active_window", "set_context_window"}:
        try:
            message = SetActiveWindowMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid SET_ACTIVE_WINDOW: {exc.errors()[0]['msg']}"))
            return
        raw_limit = message.window if message.window is not None else message.limit
        if raw_limit is None:
            await hub.send_to(sender, error_frame("context window is empty"))
            return
        try:
            value = await persist_context_window(raw_limit)
        except ValueError as exc:
            await hub.send_to(sender, error_frame(str(exc)))
            return
        await emit("system", f"SSOT context window set to {format_window(value)} ({value} tokens)")
        await hub.broadcast(snapshot())
        return

    elif message_type == "set_mode":
        try:
            message = SetModeMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid set_mode: {exc.errors()[0]['msg']}"))
            return
        if message.mode not in MODES:
            await hub.send_to(
                sender,
                error_frame(f"Unknown mode '{message.mode}'. Valid modes: {', '.join(MODES)}"),
            )
            return
        state["mode"] = message.mode
        await emit("system", f"Mode switched to {message.mode}")
        await hub.broadcast(snapshot())

    elif message_type == "set_model":
        try:
            message = SetModelMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid set_model: {exc.errors()[0]['msg']}"))
            return
        names = display_models()
        if message.model not in names:
            await hub.send_to(
                sender,
                error_frame(f"Unknown model '{message.model}'. Valid: {', '.join(names)}"),
            )
            return
        state["model"] = message.model
        engine = resolve_engine(message.model)
        await emit(
            "system",
            f"Active model set to {message.model} ({engine.litellm_model or 'fallback'})",
        )
        await hub.broadcast(snapshot())

    elif message_type == "prompt":
        try:
            message = PromptMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid prompt: {exc.errors()[0]['msg']}"))
            return
        text = message.text.strip()
        if not text:
            await hub.send_to(sender, error_frame("Prompt is empty"))
            return
        if turn_lock.locked():
            await hub.send_to(sender, error_frame("Agent is busy — wait for the current turn"))
            return
        async with turn_lock:
            await run_turn(text)

    elif message_type == "slash_command":
        try:
            message = SlashCommandMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid slash_command: {exc.errors()[0]['msg']}"))
            return
        name = message.command
        args = list(message.args)
        if message.text:
            parsed_name, parsed_args = parse_slash(message.text)
            name = name or parsed_name
            args = args or parsed_args
        if not name:
            await hub.send_to(sender, error_frame("slash command is empty"))
            return
        await run_slash_command(
            command=name,
            args=args,
            session_id=message.session_id,
            sender=sender,
        )

    elif message_type == "keys_status":
        await emit("system", keys_report())

    elif message_type == "purge":
        try:
            message = PurgeMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid purge: {exc.errors()[0]['msg']}"))
            return
        if message.pid is not None:
            ok = await supervisor.kill(message.pid)
            await emit("system", f"purge pid {message.pid}: {'killed' if ok else 'not found'}")
        else:
            count = await supervisor.kill_all()
            await emit("system", f"purged {count} agent-spawned process(es)")
        await hub.broadcast(snapshot())

    elif message_type == "memory_query":
        try:
            message = MemoryQuery.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(
                sender, error_frame(f"Invalid memory_query: {exc.errors()[0]['msg']}")
            )
            return
        index = get_index()
        text = message.text.strip()
        if text.lower() in {"watch", "watcher"}:
            watch = get_ssot_watcher().public_stats()
            await emit("memory", "SSOT watcher")
            await emit(
                "assistant",
                "watching {watching} · events {watch_events} · last {last}".format(
                    watching="yes" if watch["watching"] else "no",
                    watch_events=watch["watch_events"],
                    last=time.strftime("%H:%M:%S", time.localtime(watch["last_watch"]))
                    if watch["last_watch"]
                    else "never",
                ),
            )
            return
        if text.lower() == "window" or text.lower().startswith("window "):
            rest = text[6:].strip()
            if not rest:
                current = int(state.get("context_window") or DEFAULT_CONTEXT_WINDOW)
                packed = int(state.get("context_packed") or 0)
                await emit("memory", "SSOT context window")
                await emit(
                    "assistant",
                    f"{format_window(current)} ({current} tokens) · packed {packed}",
                )
                return
            try:
                value = await persist_context_window(rest)
            except ValueError as exc:
                await hub.send_to(sender, error_frame(str(exc)))
                return
            await emit("system", f"SSOT context window set to {format_window(value)} ({value} tokens)")
            await hub.broadcast(snapshot())
            return
        if text.lower() in {"reindex", "rebuild", "refresh"}:
            stats = await asyncio.to_thread(index.reindex)
            await emit("memory", "SSOT Memory Snapshot")
            await emit(
                "assistant",
                f"SSOT rebuilt: {stats['files']} files · {stats['chunks']} chunks · {stats['backend']}",
            )
            await hub.broadcast(snapshot())
            return
        query = text or "workspace architecture FastAPI TUI daemon tools"
        window = int(state.get("context_window") or DEFAULT_CONTEXT_WINDOW)
        k = retrieval_k(window, len(index.chunks))
        hits = await asyncio.to_thread(index.search, query, k)
        notes = await asyncio.to_thread(search_notes, query)
        await emit("memory", "SSOT Memory Snapshot")
        await emit("assistant", index.format_hits(hits))
        await hub.send_to(
            sender,
            {
                "type": "memory_hits",
                "query": query,
                "hits": [
                    {
                        "path": hit.path,
                        "kind": hit.kind,
                        "start": hit.start,
                        "end": hit.end,
                        "score": hit.score,
                        "preview": hit.preview,
                    }
                    for hit in hits
                ],
                "notes": notes,
                "timestamp": now(),
            },
        )

    elif message_type == "add_model":
        try:
            message = AddModelMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid add_model: {exc.errors()[0]['msg']}"))
            return
        ptype = message.provider_type.strip().lower() or "native"
        try:
            if ptype == "local_ollama" and message.ollama_models:
                labels = add_models_from_tags(
                    message.ollama_models,
                    host=message.base_url or "http://127.0.0.1:11434",
                    batch_class=message.batch_class,
                    context_length=message.context_length,
                    fallback_label=message.fallback_label,
                    display_prefix=message.label,
                )
            else:
                model_id = message.litellm_model.strip()
                if not model_id:
                    raise ValueError("model id is empty")
                litellm_model = (
                    model_id
                    if "/" in model_id
                    else infer_litellm_model(message.provider, model_id, ptype)
                )
                label = add_model(
                    message.label.strip() or model_id,
                    message.provider,
                    litellm_model,
                    provider_type=ptype,
                    base_url=message.base_url,
                    batch_class=message.batch_class,
                    context_length=message.context_length,
                    fallback_label=message.fallback_label,
                    fallback_model_id=message.fallback_model_id,
                    custom_headers=message.custom_headers,
                    active=message.active,
                )
                labels = [label]
            if message.api_key:
                for name in labels:
                    meta = MODEL_META.get(name, {})
                    key_env = str(meta.get("key_env") or "")
                    if key_env:
                        upsert_named(key_env, message.api_key)
                    elif ptype != "local_ollama" and message.provider != "ollama":
                        upsert_key(message.provider, message.api_key)
                        break
        except ValueError as exc:
            await hub.send_to(sender, error_frame(str(exc)))
            return
        await emit("system", f"Model added: {', '.join(labels)}")
        await hub.broadcast(snapshot())

    elif message_type == "patch_model":
        try:
            message = PatchModelMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid patch_model: {exc.errors()[0]['msg']}"))
            return
        try:
            patch_model(
                message.label,
                batch_class=message.batch_class,
                fallback_label=message.fallback_label,
                fallback_model_id=message.fallback_model_id,
                active=message.active,
                context_length=message.context_length,
            )
        except ValueError as exc:
            await hub.send_to(sender, error_frame(str(exc)))
            return
        await emit("system", f"Updated {message.label}")
        await hub.broadcast(snapshot())

    elif message_type == "set_batch":
        try:
            message = SetBatchMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid set_batch: {exc.errors()[0]['msg']}"))
            return
        try:
            state["batch"] = save_batch(message.batch)
            custom_model_manager.set_batch_mode(state["batch"])
        except ValueError as exc:
            await hub.send_to(sender, error_frame(str(exc)))
            return
        await emit("system", f"Workload batch set to {state['batch']}")
        await hub.broadcast(snapshot())

    elif message_type == "scan_ollama":
        try:
            message = ScanOllamaMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid scan_ollama: {exc.errors()[0]['msg']}"))
            return
        try:
            tags = await scan_ollama_tags(message.host)
        except ValueError as exc:
            await hub.send_to(
                sender,
                {
                    "type": "ollama_tags",
                    "ok": False,
                    "host": message.host,
                    "models": [],
                    "error": str(exc),
                    "timestamp": now(),
                },
            )
            return
        except Exception as exc:
            await hub.send_to(
                sender,
                {
                    "type": "ollama_tags",
                    "ok": False,
                    "host": message.host,
                    "models": [],
                    "error": f"Ollama scan failed ({type(exc).__name__})",
                    "timestamp": now(),
                },
            )
            return
        await hub.send_to(
            sender,
            {
                "type": "ollama_tags",
                "ok": True,
                "host": message.host,
                "models": tags,
                "error": "",
                "timestamp": now(),
            },
        )

    elif message_type == "ping_model":
        try:
            message = PingModelMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid ping_model: {exc.errors()[0]['msg']}"))
            return
        try:
            engine = resolve_engine(message.label)
        except Exception as exc:
            await hub.send_to(sender, error_frame(str(exc)))
            return
        
        t0 = time.monotonic()
        ok = await probe_engine(engine)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        
        if ok:
            from robber.router import merge_meta
            merge_meta(message.label, latency_ms=elapsed_ms, health="online")
            await emit("system", f"Pinged {message.label}: {elapsed_ms}ms (Online)")
        else:
            from robber.router import merge_meta
            merge_meta(message.label, health="offline")
            await emit("error", f"Pinged {message.label} failed (Offline)")
        await hub.broadcast(snapshot())

    elif message_type == "set_key":
        try:
            message = SetKeyMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid set_key: {exc.errors()[0]['msg']}"))
            return
        try:
            env_name = upsert_key(message.provider, message.secret)
        except ValueError as exc:
            await hub.send_to(sender, error_frame(str(exc)))
            return
        await emit("system", f"Key saved for {message.provider} ({env_name}) — value not shown")
        await hub.broadcast(snapshot())

    elif message_type == "set_consensus":
        try:
            message = SetConsensusMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(
                sender, error_frame(f"Invalid set_consensus: {exc.errors()[0]['msg']}")
            )
            return
        used = save_consensus(message.models)
        if used:
            state["model"] = HYBRID_LABEL
        label = ", ".join(used) if used else "default Hybrid routes"
        await emit(
            "system",
            f"Task consensus set to {label} — up to {MAX_CONSENSUS} models run in parallel on Hybrid",
        )
        await hub.broadcast(snapshot())

    elif message_type == "memory_inject":
        try:
            message = MemoryInject.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(
                sender, error_frame(f"Invalid memory_inject: {exc.errors()[0]['msg']}")
            )
            return
        try:
            note = add_note(message.key, message.value, message.category, message.tags)
        except ValueError as exc:
            await hub.send_to(sender, error_frame(str(exc)))
            return
        await emit("memory", f"Injected {note['key']}")
        await hub.broadcast(snapshot())

    elif message_type == "memory_purge":
        count = purge_notes()
        await emit("system", f"purged {count} injected memory note(s)")
        await hub.broadcast(snapshot())

    elif message_type == "set_theme":
        try:
            message = SetThemeMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid set_theme: {exc.errors()[0]['msg']}"))
            return
        try:
            state["theme"] = save_theme(message.theme)
        except ValueError as exc:
            await hub.send_to(sender, error_frame(str(exc)))
            return
        await emit("system", f"Theme set to {state['theme']} (GUI and TUI)")
        await hub.broadcast(snapshot())

    elif message_type == "ping":
        await hub.send_to(sender, snapshot())

    elif message_type == "mcp_list":
        await emit("system", get_mcp_host().format_catalog())
        await hub.send_to(sender, {"type": "mcp", **get_mcp_host().snapshot(), "timestamp": now()})

    elif message_type in {"mcp_enable", "mcp_disable"}:
        try:
            message = McpIdMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid {message_type}: {exc.errors()[0]['msg']}"))
            return
        server_id = (message.id or message.server).strip()
        if not server_id:
            await hub.send_to(sender, error_frame("MCP id is required"))
            return
        try:
            if message_type == "mcp_enable":
                await get_mcp_host().enable(server_id)
                await emit("system", f"MCP enabled: {server_id}")
            else:
                await get_mcp_host().disable(server_id)
                await emit("system", f"MCP disabled: {server_id}")
        except ValueError as exc:
            await hub.send_to(sender, error_frame(str(exc)))
            return
        await hub.broadcast(snapshot())

    elif message_type == "mcp_add":
        try:
            message = McpAddMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid mcp_add: {exc.errors()[0]['msg']}"))
            return
        try:
            await get_mcp_host().add(
                server_id=message.id,
                label=message.label or message.id,
                command=message.command,
                args=list(message.args),
                env_keys=list(message.env_keys),
                description=message.description,
                enabled=message.enabled,
            )
        except ValueError as exc:
            await hub.send_to(sender, error_frame(str(exc)))
            return
        await emit("system", f"MCP added: {message.id} (local catalog, no download)")
        await hub.broadcast(snapshot())

    elif message_type == "mcp_remove":
        try:
            message = McpIdMessage.model_validate({**data, "type": "mcp_enable"})
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid mcp_remove: {exc.errors()[0]['msg']}"))
            return
        server_id = (message.id or message.server).strip()
        if not server_id:
            await hub.send_to(sender, error_frame("MCP id is required"))
            return
        try:
            await get_mcp_host().remove(server_id)
        except ValueError as exc:
            await hub.send_to(sender, error_frame(str(exc)))
            return
        await emit("system", f"MCP removed: {server_id}")
        await hub.broadcast(snapshot())

    elif message_type == "mcp_call":
        try:
            message = McpCallMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid mcp_call: {exc.errors()[0]['msg']}"))
            return
        result = await get_mcp_host().call(
            message.server,
            message.tool,
            _mcp_arguments(message.arguments),
        )
        await _mcp_emit_result(sender, result)

    else:
        await hub.send_to(
            sender,
            error_frame(
                "Unknown type "
                f"'{message_type}'. Expected set_mode, set_model, prompt, "
                "keys_status, purge, memory_query, add_model, set_key, "
                "set_consensus, memory_inject, memory_purge, set_theme, "
                "patch_model, set_batch, scan_ollama, ping, set_control_mode, "
                "heal_approve, heal_reject, loop_status, mcp_list, mcp_enable, "
                "mcp_disable, mcp_add, mcp_remove, mcp_call or slash_command"
            ),
        )


agent_loop.bind_runtime(
    run_turn=run_turn,
    dispatch=dispatch,
    turn_lock=turn_lock,
    snapshot=snapshot,
)


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "name": "agent-robber-server",
        "ws": "/ws",
        "gui": "http://127.0.0.1:5173",
        "engine": resolve_engine(state["model"], batch=str(state.get("batch") or "")).litellm_model
        or "fallback",
        "theme": state["theme"],
        "batch": state.get("batch"),
        "keys": providers_status(),
        "workspace": str(workspace_root()),
        "ssot": ssot_payload(),
        "jobs": supervisor.snapshot(),
        "self_heal": True,
        "vision": True,
        "control_mode": agent_loop.control_mode,
        "loop": True,
        "mcp": True,
        "ssot_watch": True,
        "ssot_context": True,
        "orchestration": True,
        "slash_commands": get_registry().catalog(),
        "route_mode": custom_model_manager.route_mode,
        "context_window": int(state.get("context_window") or DEFAULT_CONTEXT_WINDOW),
    }


def _purge_browser_session(profile_id: str) -> None:
    from robber.jail import JailError, resolve_inside

    try:
        target = resolve_inside(workspace_root(), f"data/browser_sessions/{profile_id}")
    except JailError:
        return
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)


@app.post("/api/models/batch-mode")
async def set_batch_mode_endpoint(mode: str) -> dict[str, Any]:
    try:
        state["batch"] = save_batch(mode)
        tier = custom_model_manager.set_batch_mode(state["batch"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await hub.broadcast(snapshot())
    return {"status": "ok", "mode": tier, "batch": state["batch"]}


@app.get("/api/presets")
async def list_presets() -> dict[str, Any]:
    return {"presets": preset_public()}


@app.get("/api/commands")
async def list_slash_commands() -> dict[str, Any]:
    registry = get_registry()
    return {"commands": registry.catalog(), "help": registry.help_text()}


@app.post("/api/commands/run")
async def rest_run_slash(payload: SlashCommandMessage) -> dict[str, Any]:
    name = payload.command
    args = list(payload.args)
    if payload.text:
        parsed_name, parsed_args = parse_slash(payload.text)
        name = name or parsed_name
        args = args or parsed_args
    if not name:
        raise HTTPException(status_code=400, detail="command is empty")
    try:
        return await run_slash_command(
            command=name,
            args=args,
            session_id=payload.session_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/models")
async def orch_list_models() -> dict[str, Any]:
    rows = await custom_model_manager.get_models()
    return {"models": rows, "route_mode": custom_model_manager.route_mode}


@app.post("/api/models")
async def orch_create_model(payload: OrchModelIn) -> dict[str, Any]:
    try:
        entry_id = await custom_model_manager.add_model(payload.model_dump(exclude_none=True))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await hub.broadcast(snapshot())
    rows = await custom_model_manager.get_models()
    row = next((item for item in rows if item["id"] == entry_id), None)
    return {"model": row}


@app.post("/api/models/add")
async def add_model_endpoint(model_data: dict[str, Any]) -> dict[str, Any]:
    try:
        entry_id = await custom_model_manager.add_model(model_data)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await hub.broadcast(snapshot())
    return {"status": "ok", "id": entry_id}


@app.get("/api/models/ollama/scan")
async def scan_ollama() -> dict[str, Any]:
    return {"models": await custom_model_manager.scan_ollama_models()}


@app.post("/api/models/status")
async def update_model_status(model_id: str, is_active: bool) -> dict[str, Any]:
    ok = await custom_model_manager.update_model(model_id, {"is_active": is_active})
    if not ok:
        raise HTTPException(status_code=404, detail="model not found")
    await hub.broadcast(snapshot())
    return {"status": "ok"}


@app.put("/api/models/{model_id}")
async def orch_update_model(model_id: str, payload: OrchModelPatch) -> dict[str, Any]:
    updates = {key: value for key, value in payload.model_dump().items() if value is not None}
    try:
        ok = await custom_model_manager.update_model(model_id, updates)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="model not found")
    await hub.broadcast(snapshot())
    rows = await custom_model_manager.get_models()
    row = next((item for item in rows if item["id"] == model_id), None)
    return {"model": row}


@app.delete("/api/models/{model_id}")
async def orch_delete_model(model_id: str) -> dict[str, Any]:
    ok = await custom_model_manager.delete_model(model_id)
    if not ok:
        raise HTTPException(status_code=404, detail="model not found")
    await hub.broadcast(snapshot())
    return {"deleted": model_id}


@app.post("/api/models/{model_id}/test")
async def orch_test_model(model_id: str) -> dict[str, Any]:
    try:
        result = await custom_model_manager.test_model(model_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await hub.broadcast(snapshot())
    return result


@app.get("/api/combos")
async def orch_list_combos() -> dict[str, Any]:
    return {"combos": await custom_model_manager.get_combo_models()}


@app.post("/api/combos")
async def orch_upsert_combo(payload: ComboIn) -> dict[str, Any]:
    data = payload.model_dump(exclude_none=True)
    try:
        if payload.id:
            existing = await database.fetchone("SELECT id FROM combo_models WHERE id = ?", (payload.id,))
            if existing:
                ok = await custom_model_manager.update_combo_model(payload.id, data)
                if not ok:
                    raise HTTPException(status_code=404, detail="combo not found")
                combo_id = payload.id
            else:
                combo_id = await custom_model_manager.create_combo_model(data)
        else:
            combo_id = await custom_model_manager.create_combo_model(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await hub.broadcast(snapshot())
    rows = await custom_model_manager.get_combo_models()
    row = next((item for item in rows if item["id"] == combo_id), None)
    return {"combo": row}


@app.post("/api/system/route-mode")
async def orch_set_route_mode(payload: RouteModeIn) -> dict[str, Any]:
    try:
        state["batch"] = save_batch(payload.mode)
        tier = custom_model_manager.set_route_mode(state["batch"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await hub.broadcast(snapshot())
    return {"route_mode": tier, "batch": state["batch"]}

@app.get("/api/accounts")
async def list_registered_accounts() -> dict[str, Any]:
    rows = await account_manager.list_accounts()
    for row in rows:
        worker = worker_pool.worker_states.get(row["profile_id"], {})
        row["worker"] = {
            "status": "running" if worker_pool.is_running(row["profile_id"]) else "idle",
            "target": worker.get("target"),
        }
    return {"accounts": rows}


@app.post("/api/accounts/add")
async def add_registered_account(payload: AddAccountRequest) -> dict[str, Any]:
    try:
        proxy_obj = None
        if payload.proxy is not None:
            proxy_obj = ProxyConfig.from_mapping(payload.proxy.model_dump())
        account = await account_manager.upsert_account(
            username=payload.username,
            password=payload.password,
            profile_id=payload.profile_id,
            status=payload.status,
            proxy_id=payload.proxy_id,
            proxy=proxy_obj,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"account": account}


@app.delete("/api/accounts/{profile_id}")
async def delete_registered_account(profile_id: str) -> dict[str, Any]:
    await worker_pool.stop_worker(profile_id)
    try:
        removed = await account_manager.delete_account(profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="profile not found")
    _purge_browser_session(profile_id)
    return {"deleted": profile_id}


@app.get("/api/mcp")
async def mcp_catalog() -> dict[str, Any]:
    return get_mcp_host().snapshot()


class McpEnableBody(BaseModel):
    id: str


class McpAddBody(BaseModel):
    id: str
    label: str = ""
    command: str
    args: list[str] = Field(default_factory=list)
    env_keys: list[str] = Field(default_factory=list)
    description: str = ""
    enabled: bool = False


class McpCallBody(BaseModel):
    server: str
    tool: str
    arguments: dict[str, Any] | str | None = None


@app.post("/api/mcp/enable")
async def mcp_enable(body: McpEnableBody) -> dict[str, Any]:
    try:
        await get_mcp_host().enable(body.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await hub.broadcast(snapshot())
    return get_mcp_host().snapshot()


@app.post("/api/mcp/disable")
async def mcp_disable(body: McpEnableBody) -> dict[str, Any]:
    try:
        await get_mcp_host().disable(body.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await hub.broadcast(snapshot())
    return get_mcp_host().snapshot()


@app.post("/api/mcp/add")
async def mcp_add(body: McpAddBody) -> dict[str, Any]:
    try:
        await get_mcp_host().add(
            server_id=body.id,
            label=body.label or body.id,
            command=body.command,
            args=list(body.args),
            env_keys=list(body.env_keys),
            description=body.description,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await hub.broadcast(snapshot())
    return get_mcp_host().snapshot()


@app.delete("/api/mcp/{server_id}")
async def mcp_remove(server_id: str) -> dict[str, Any]:
    try:
        await get_mcp_host().remove(server_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await hub.broadcast(snapshot())
    return get_mcp_host().snapshot()


@app.post("/api/mcp/call")
async def mcp_call(body: McpCallBody) -> dict[str, Any]:
    result = await get_mcp_host().call(body.server, body.tool, _mcp_arguments(body.arguments))
    return {
        "ok": result.ok,
        "server": result.server,
        "tool": result.tool,
        "content": result.text,
    }


@app.get("/api/system/health")
async def system_health() -> dict[str, Any]:
    return await agent_loop.collect_health()


@app.get("/api/ssot/window")
async def get_ssot_window() -> dict[str, Any]:
    return window_public(
        int(state.get("context_window") or DEFAULT_CONTEXT_WINDOW),
        packed=int(state.get("context_packed") or 0),
        hit_count=int(state.get("context_hits") or 0),
    )


@app.post("/api/ssot/window")
async def set_ssot_window(payload: SsotWindowBody) -> dict[str, Any]:
    raw = payload.window if payload.window is not None else payload.limit
    if raw is None:
        raise HTTPException(status_code=400, detail="context window is empty")
    try:
        value = await persist_context_window(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await hub.broadcast(snapshot())
    return window_public(
        value,
        packed=int(state.get("context_packed") or 0),
        hit_count=int(state.get("context_hits") or 0),
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await hub.connect(websocket)
    # Heartbeat task
    heartbeat_task = asyncio.create_task(_heartbeat(websocket))
    
    await hub.send_to(websocket, snapshot())
    await hub.send_to(websocket, agent_loop.loop_state_frame())
    await hub.broadcast({"type": "event", "event": "client_joined", "clients": hub.client_count})

    try:
        while True:
            raw = await websocket.receive_text()
            await agent_loop.handle_ws(websocket, raw)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        heartbeat_task.cancel()
        await hub.disconnect(websocket)
        await hub.broadcast({"type": "event", "event": "client_left", "clients": hub.client_count})
        await hub.broadcast(snapshot())

async def _heartbeat(websocket: WebSocket):
    try:
        while True:
            await asyncio.sleep(30)
            await websocket.send_json({"type": "ping"})
    except Exception:
        pass


async def _on_ssot_watch(_stats: dict[str, object]) -> None:
    await hub.broadcast(snapshot())


async def _boot_ssot() -> None:
    await asyncio.to_thread(get_index().reindex)
    await hub.broadcast(snapshot())
    await get_ssot_watcher().start(on_refresh=_on_ssot_watch)


async def _boot_mcp() -> None:
    try:
        await get_mcp_host().start(supervisor)
    except Exception:
        pass
    await hub.broadcast(snapshot())


@app.on_event("startup")
async def startup_ssot() -> None:
    await database.connect()
    await database.init_schema()
    db_window = await database.get_ssot_window()
    pref_window = load_context_window()
    window = db_window if db_window is not None else pref_window
    try:
        window = parse_window(window)
    except ValueError:
        window = DEFAULT_CONTEXT_WINDOW
    await persist_context_window(window)
    custom_model_manager.set_batch_mode(str(state.get("batch") or "normal"))
    await custom_model_manager.bootstrap()
    proxy_manager.bootstrap_from_env()
    await account_manager.sync_proxies()
    # asyncio.create_task(_boot_ssot())
    # asyncio.create_task(_boot_mcp())



@app.on_event("shutdown")
async def shutdown_store() -> None:
    # await get_ssot_watcher().stop()
    await get_mcp_host().stop()
    await database.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=int(os.environ.get("ROBBER_PORT", "8787")), reload=True)
