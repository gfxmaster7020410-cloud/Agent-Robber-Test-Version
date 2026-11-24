import asyncio
import json
import logging
import os
import shutil
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from robber.vault import load_dotenv

# Load repo `.env` before engines read os.environ. Do not require python-dotenv.
load_dotenv()
logger = logging.getLogger("robber.server")

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
from robber.engine.discovery import (
    discover_and_ingest,
    discovery_log,
    failover_log,
    get_discovery,
)
from robber.engine.failover_pool import get_failover_pool
from robber.engine.model_manager import CustomModelManager
from robber.engine.presets import batch_to_tier, preset_public
import robber.commands.handlers  # noqa: F401 — registers slash commands
from robber.commands.registry import ExecutionContext, get_registry, parse_slash

from robber.engine.vision_engine import VisionInspector
from robber.tools.patch_applier import JailedPatchApplicator
from robber.agent_loop import AgentLoopManager, bind_loop
from robber.canvas import build_canvas
from robber.markdown_ansi import ACT_SILENT_DIRECTIVE


def _sync_frame(payload: dict[str, Any]) -> dict[str, Any]:
    from robber.events import attach_sync_envelope, set_elapsed_ms
    from robber.mutations import get_mutations

    try:
        set_elapsed_ms(get_mutations().elapsed_ms())
    except Exception:
        pass
    return attach_sync_envelope(dict(payload))


# App Initialization
app = FastAPI(title="Agent Robber API Center", version="1.0.0")

class Hub:
    def __init__(self, session_manager: SessionManager) -> None:
        self._clients: dict[WebSocket, asyncio.Queue] = {}
        self._senders: dict[WebSocket, asyncio.Task] = {}
        self._sse: list[asyncio.Queue] = []
        self._session_manager = session_manager
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def connect(self, client: WebSocket) -> None:
        await client.accept()
        async with self._lock:
            self._clients[client] = asyncio.Queue(maxsize=512)
            self._senders[client] = asyncio.create_task(self._client_sender(client))

    async def _client_sender(self, client: WebSocket):
        queue = self._clients.get(client)
        if not queue:
            return
        try:
            while True:
                payload = await queue.get()
                await client.send_text(json.dumps(payload, default=str))
                queue.task_done()
        except Exception:
            await self.disconnect(client)

    async def disconnect(self, client: WebSocket) -> None:
        async with self._lock:
            self._clients.pop(client, None)
            sender = self._senders.pop(client, None)
        if sender is not None:
            sender.cancel()

    def _enqueue(self, queue: asyncio.Queue, frame: dict[str, Any]) -> None:
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    async def send_to(self, client: WebSocket, payload: dict[str, Any]) -> None:
        frame = _sync_frame(payload)
        async with self._lock:
            queue = self._clients.get(client)
        if queue:
            self._enqueue(queue, frame)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        frame = _sync_frame(payload)
        async with self._lock:
            queues = list(self._clients.values())
            sse = list(self._sse)
        for queue in queues:
            self._enqueue(queue, frame)
        for queue in sse:
            self._enqueue(queue, frame)

    def subscribe_sse(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._sse.append(queue)
        return queue

    def unsubscribe_sse(self, queue: asyncio.Queue) -> None:
        try:
            self._sse.remove(queue)
        except ValueError:
            return












# CORS Middleware Configuration
def _cors_origins() -> list[str]:
    extra = os.environ.get("ROBBER_CORS_ORIGINS", "")
    origins = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
        "http://127.0.0.1:1420",
        "http://localhost:1420",
        "http://127.0.0.1:8787",
        "http://localhost:8787",
        "https://tauri.localhost",
        "http://tauri.localhost",
    ]
    for item in extra.split(","):
        host = item.strip()
        if host and host != "*" and host not in origins:
            origins.append(host)
    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
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
worker_pool = WorkerPool(
    hub,
    session_manager,
    snapshot_vault,
    proxy_manager,
    account_manager,
    vision_inspector=vision_inspector,
    healer=self_healing,
)


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
    clip_to_token_budget,
    estimate_tokens,
    format_window,
    pack_ssot_context,
    parse_window,
    retrieval_k,
    set_context_window,
    window_public,
    working_window,
)
from robber.ssot_watch import SsotWatcher, bind_ssot_watcher, get_ssot_watcher
from robber.terminal import TerminalSupervisor
from robber.intent import classify_intent, is_inquiry, should_use_distributed_brain
from robber.tools import ToolResult, execute_tool, select_tool
from robber.events import mutation_sync_frame, set_elapsed_ms, set_last_command, state_ack_frame
from robber.paste_buffer import prepare_prompt, public_stats, save_paste_buffer, sanitize_paste
from robber.engine.distributed_brain import DistributedAgentBrain, bind_distributed_brain, get_distributed_brain
from robber.orchestrator import (
    bind_orchestrator,
    get_orchestrator,
    is_failover_error,
    is_transient_error,
    parse_dashboard_mode,
    step_action_command,
    StreamThrottle,
)
from robber.mutations import FileMutationManager, TurnAborted, bind_mutations, get_mutations

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

def now() -> float:
    return time.time()


def _session_id_of(raw: object = None) -> str:
    value = str(raw or state.get("session_id") or "current_session").strip()
    return value or "current_session"


def _estimate_tokens(text: str) -> int:
    return max(0, (len(text or "") + 3) // 4)


async def persist_conversation(role: str, content: str, *, session_id: str | None = None) -> None:
    text = (content or "").strip()
    if not text:
        return
    try:
        await database.add_conversation_message(
            _session_id_of(session_id),
            role,
            text,
            tokens=_estimate_tokens(text),
        )
    except Exception:
        pass


async def history_frame(session_id: str | None = None, *, limit: int = 200) -> dict[str, Any]:
    sid = _session_id_of(session_id)
    try:
        turns = await database.get_conversation_history(sid, limit=limit)
    except Exception:
        turns = []
    return {
        "type": "history",
        "session_id": sid,
        "turns": turns,
        "timestamp": now(),
    }


supervisor = TerminalSupervisor(root=workspace_root())
bind_mutations(FileMutationManager(workspace_root()))
bind_orchestrator(custom_model_manager)
bind_distributed_brain(DistributedAgentBrain(custom_model_manager))
mcp_host = bind_mcp(McpHost())
mcp_host.bind(supervisor)
ssot_watcher = bind_ssot_watcher(SsotWatcher())


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
    "hybrid": False,
    "elapsed_ms": 0,
    "kill_requested": False,
    "session_id": "current_session",
}
state["supervisor"] = supervisor
state["worker_pool"] = worker_pool
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


class PasteIn(BaseModel):
    text: str


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


class DiscoverModelsMessage(BaseModel):
    type: str = "discover_models"
    force: bool = False
    base_url: str = ""
    api_key: str | None = None
    provider_id: str = ""


class PatchDiscoveryMessage(BaseModel):
    type: str = "patch_discovery"
    model_id: str
    disabled: bool | None = None
    priority_high: bool | None = None


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
    limit: str | int | float | None = None
    window: str | int | float | None = None


class SsotWindowBody(BaseModel):
    window: str | int | float | None = None
    limit: str | int | float | None = None


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


class DiscoverIn(BaseModel):
    force: bool = False
    base_url: str = ""
    api_key: str | None = None
    provider_id: str = ""


class DiscoveryOverrideIn(BaseModel):
    model_id: str
    disabled: bool | None = None
    priority_high: bool | None = None


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
    payload: dict[str, Any] = {
        "files": 0,
        "chunks": 0,
        "backend": "hash-json",
        "watching": False,
        "watch_events": 0,
        "last_watch": 0.0,
    }
    try:
        index = get_index()
        stats = getattr(index, "stats", None) if index is not None else None
        if isinstance(stats, dict):
            payload.update(stats)
    except Exception:
        pass
    try:
        watcher = get_ssot_watcher()
        public = watcher.public_stats() if watcher is not None else None
        if isinstance(public, dict):
            payload.update(public)
    except Exception:
        pass
    payload.update(
        window_public(
            int(state.get("context_window") or DEFAULT_CONTEXT_WINDOW),
            packed=int(state.get("context_packed") or 0),
            hit_count=int(state.get("context_hits") or 0),
        )
    )
    return payload


async def persist_context_window(raw: object) -> int:
    value = set_context_window(raw)
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
        "mutations": get_mutations().public(),
        "hybrid": bool(state.get("hybrid")),
        "elapsed_ms": get_mutations().elapsed_ms() if state.get("busy") else int(state.get("elapsed_ms") or 0),
        "discovery": get_discovery().public_summary(),
        "failover_pool": get_failover_pool().public(),
        "context_window": int(state.get("context_window") or DEFAULT_CONTEXT_WINDOW),
        "context_packed": int(state.get("context_packed") or 0),
        "context_hits": int(state.get("context_hits") or 0),
        "pending_injects": instruction_interceptor.pending_count(),
        "turn_tokens": agent_loop._turn_tokens,
        "max_tokens_per_turn": agent_loop.max_tokens_per_turn,
        "timestamp": now(),
    }


def _model_capacity() -> int:
    label = str(state.get("model") or "")
    cap = 0
    meta = MODEL_META.get(label) or {}
    try:
        cap = int(meta.get("context_length") or 0)
    except (TypeError, ValueError):
        cap = 0
    for row in custom_model_manager.models_cache:
        name = str(row.get("name") or "")
        entry_id = str(row.get("id") or "")
        if name == label or entry_id == label:
            try:
                cap = int(row.get("context_length") or cap)
            except (TypeError, ValueError):
                pass
            break
    return cap if cap > 0 else DEFAULT_CONTEXT_WINDOW


def compose_reply(query: str, tool: ToolResult, memory: str | None = None) -> str:
    from robber.intent import is_inquiry

    if tool.name == "Conversation" or is_inquiry(query):
        return (
            "Yes, I have full read and write access to your local files and terminal. "
            "What file or project would you like me to work on?"
        )
    scan_names = {"Local Directory Scan", "Workspace Context Scan"}
    lowered = query.lower()
    listing_intent = any(
        token in lowered for token in ("scan", "list", "tree", "folder", "director")
    )
    if tool.name in scan_names and not listing_intent:
        n = len(query)
        preview = query.strip()[:400]
        extra = "…" if n > 400 else ""
        mark = "ok" if tool.ok else "blocked"
        return (
            f"Received {n} characters.\n\n{preview}{extra}\n\n"
            "No live model completion. "
            f"Last tool: {tool.name} [{mark}]."
        )
    short = query if len(query) <= 64 else f"{query[:61]}…"
    mark = "ok" if tool.ok else "blocked"
    preview_lines = tool.output.splitlines()[:12]
    preview = "\n".join(preview_lines)
    if len(tool.output.splitlines()) > 12:
        preview += "\n…"
    parts = [
        f"### {tool.name} [{mark}]",
        "",
        preview or f'Working on "{short}".',
    ]
    if memory:
        summary = memory.splitlines()[0]
        if summary:
            parts.extend(["", summary])
    if str(state.get("mode") or "act") in {"plan", "review"}:
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


async def emit(
    role: str,
    content: str,
    *,
    append: bool = False,
    stream_kind: str | None = None,
    ansi: list[dict[str, Any]] | None = None,
    ansi_raw: str | None = None,
) -> None:
    from robber.ansi import parse_ansi
    from robber.events import next_frame_id, stream_kind_for_role
    from robber.markdown_ansi import colorize_for_emit
    from robber.thoughts import strip_thoughts

    if role == "assistant":
        content = strip_thoughts(content)
        if not (content or "").strip() and not append:
            return
    if ansi is None and role in {"assistant", "system"}:
        content = colorize_for_emit(content, append=append)
    plain, segments = (content, ansi) if ansi is not None else parse_ansi(content)
    kind = stream_kind or stream_kind_for_role(role)
    if role == "assistant":
        previous = str(state.get("last_output") or "")
        state["last_output"] = (previous + plain) if append else plain
    elif role == "reasoning":
        previous = str(state.get("last_reasoning") or "")
        state["last_reasoning"] = (previous + plain) if append else plain
    elif role == "tool":
        state["last_tool"] = plain
    await hub.broadcast(
        {
            "id": next_frame_id(),
            "type": "message",
            "role": role,
            "content": plain,
            "text": plain,
            "append": append,
            "timestamp": now(),
            "ansi": segments,
            "ansi_raw": ansi_raw or "",
            "stream_kind": kind,
        }
    )


def _header_map(value: object) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(val) for key, val in value.items()}
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return {str(key): str(val) for key, val in parsed.items()}
    return {}


async def _notify_discovery(result: object) -> None:
    from robber.engine.discovery import DiscoveryResult

    if not isinstance(result, DiscoveryResult):
        return
    if result.ok:
        await emit("user", discovery_log(result.active_count))
    elif result.error:
        await emit("system", f"Model discovery failed ({result.error})")


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

    async def undo_step(self) -> str:
        return get_mutations().undo()

    async def redo_step(self) -> str:
        return get_mutations().redo()

    async def accept_all(self) -> str:
        return get_mutations().accept_all()

    async def revert_all(self) -> str:
        return get_mutations().revert_all()

    async def run_argv(self, argv: list[str], timeout: float = 30.0) -> tuple[int, str]:
        result = await supervisor.run(argv, timeout=timeout)
        return result.exit_code, result.output

    async def start_argv(self, argv: list[str]) -> int:
        job = await supervisor.start(argv)
        return job.pid

    async def discover_models(self, force: bool = False) -> str:
        results = await custom_model_manager.discover_known(force=force)
        ok = [item for item in results if item.ok]
        count = sum(item.active_count for item in ok)
        await emit("user", discovery_log(count))
        await hub.broadcast(snapshot())
        pool = get_failover_pool().public()
        return (
            f"Discovered {count} active models across {len(ok)} endpoints. "
            f"Failover pool size {pool['size']}."
        )


slash_host = SlashHost()


async def run_slash_command(
    *,
    command: str,
    args: list[str],
    session_id: str,
    sender: WebSocket | None = None,
) -> dict[str, Any]:
    name = (command or "").strip().lower().lstrip("/")
    set_last_command(f"/{name}" if name else "")
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
    await hub.broadcast(mutation_sync_frame(get_mutations().public()))
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
    """Return True if at least one assistant token arrived.

    Transient errors retry locally (3 attempts, exponential backoff) before
    the caller hops to the next failover engine.
    """
    from robber.config import TURN_RETRY_ATTEMPTS, TURN_RETRY_BASE_SECONDS
    from robber.thoughts import ThoughtGate

    for attempt in range(1, TURN_RETRY_ATTEMPTS + 1):
        buffers = {"assistant": ""}
        started = {"assistant": False}
        gate = ThoughtGate()
        throttle = StreamThrottle()
        first_token_at: float | None = None
        chars = 0
        t0 = time.monotonic()
        try:
            async for role, delta in stream_completion(engine, query, mode, tool_context):
                if state.get("kill_requested"):
                    leftover = throttle.flush()
                    if leftover:
                        buffers["assistant"] += leftover
                        await flush_stream("assistant", started, buffers["assistant"])
                        buffers["assistant"] = ""
                    await emit("error", "Task killed")
                    return started["assistant"]
                if role == "reasoning":
                    previous = str(state.get("last_reasoning") or "")
                    state["last_reasoning"] = previous + delta
                    continue
                visible = gate.push(delta)
                if not visible:
                    continue
                if first_token_at is None:
                    first_token_at = time.monotonic()
                    state["ttft_ms"] = int((first_token_at - t0) * 1000)
                chars += len(visible)
                if not agent_loop.consume_tokens(estimate_tokens(visible)):
                    state["token_cap_hit"] = True
                    chunk = throttle.feed(visible) or throttle.flush()
                    if chunk:
                        buffers["assistant"] += chunk
                        await flush_stream("assistant", started, buffers["assistant"])
                    leftover = throttle.flush()
                    if leftover:
                        await flush_stream("assistant", started, leftover)
                    return started["assistant"]
                chunk = throttle.feed(visible)
                if chunk:
                    buffers["assistant"] += chunk
                    await flush_stream("assistant", started, buffers["assistant"])
                    buffers["assistant"] = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            streamed = started["assistant"]
            if (not streamed) and is_transient_error(exc) and attempt < TURN_RETRY_ATTEMPTS:
                delay = TURN_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                await emit("user", f"✦ Retry {attempt}/{TURN_RETRY_ATTEMPTS} · {type(exc).__name__}")
                await asyncio.sleep(delay)
                continue
            if is_failover_error(exc):
                get_failover_pool().quarantine(engine.model_string or engine.label)
                return False
            await emit("error", f"Model router failed — falling back. ({type(exc).__name__})")
            return streamed

        tail = gate.flush()
        if tail:
            if not agent_loop.consume_tokens(estimate_tokens(tail)):
                state["token_cap_hit"] = True
            throttle.feed(tail)
        leftover = throttle.flush()
        if leftover:
            buffers["assistant"] += leftover
        for role, buf in buffers.items():
            if buf:
                await flush_stream(role, started, buf)

        elapsed = time.monotonic() - t0
        if chars and elapsed > 0:
            state["tps"] = round(chars / elapsed, 1)
        if first_token_at is not None:
            get_failover_pool().note_latency(engine.model_string or engine.label, int((first_token_at - t0) * 1000))
        return started["assistant"]
    return False


async def pick_healthy_engine(engine: Engine, used: set[str]) -> Engine | None:
    """Probe the primary, then one fallback hop. Never loops."""
    if engine.label in used:
        return None
    local = (engine.provider or "").strip().lower() in {"ollama", "vllm", "lmstudio", "local"}
    if engine.live and engine.api_key and not local:
        return engine
    if engine.live and (engine.api_key or local):
        if await probe_engine(engine):
            return engine
    fallback_name = (engine.fallback_label or "").strip()
    if not fallback_name or fallback_name in used or fallback_name == engine.label:
        await emit("system", f"{engine.label} endpoint failed health check", stream_kind="ack")
        return None
    fallback = resolve_engine(fallback_name)
    if fallback.label in used:
        return None
    if await probe_engine(fallback):
        await emit("system", f"{engine.label} unhealthy - failing over to {fallback.label}", stream_kind="ack")
        return fallback
    await emit("system", f"{engine.label} endpoint failed health check", stream_kind="ack")
    return None


async def stream_with_failover(
    query: str, mode: str, engine: Engine, tool_context: str | None = None, kind: str = "general"
) -> bool:
    from robber.config import ROUTER_FAILOVER_ATTEMPTS
    from robber.router.failover import MAX_FALLBACK_DEPTH

    used: set[str] = set()
    current = engine
    pool = get_failover_pool()
    errors: list[str] = []
    for _ in range(max(1, min(MAX_FALLBACK_DEPTH, ROUTER_FAILOVER_ATTEMPTS))):
        if pool.is_quarantined(current.model_string or current.label):
            nxt = pool.next_after(current, kind, used)
            if nxt is None:
                fallback_name = (current.fallback_label or "").strip()
                if not fallback_name or fallback_name in used:
                    break
                nxt = resolve_engine(fallback_name)
            if nxt.label == current.label or nxt.label in used:
                errors.append(f"{current.label} quarantined")
                break
            await emit("system", failover_log(current.label, nxt.label), stream_kind="ack")
            used.add(current.label)
            current = nxt
            continue
        chosen = await pick_healthy_engine(current, used)
        if chosen is None:
            nxt = pool.next_after(current, kind, used)
            if nxt is None:
                errors.append(f"{current.label} unhealthy")
                break
            if nxt.label in used or nxt.label == current.label:
                errors.append(f"{current.label} unhealthy")
                break
            await emit("system", failover_log(current.label, nxt.label), stream_kind="ack")
            used.add(current.label)
            current = nxt
            continue
        if state.get("kill_requested") or state.get("token_cap_hit"):
            return False
        used.add(chosen.label)
        if chosen.model_string:
            used.add(chosen.model_string)
        produced = await stream_model(query, mode, chosen, tool_context)
        if state.get("kill_requested") or state.get("token_cap_hit"):
            return produced
        if produced:
            return True
        from robber.router import get_session_router

        hop = get_session_router().next_channel(chosen)
        if hop is not None and hop.api_key and hop.api_key != chosen.api_key:
            produced = await stream_model(query, mode, hop, tool_context)
            if produced:
                return True
        pool.quarantine(chosen.model_string or chosen.label)
        nxt = pool.next_after(chosen, kind, used)
        if nxt is None:
            fallback_name = (chosen.fallback_label or "").strip()
            if not fallback_name or fallback_name in used:
                errors.append(f"{chosen.label} empty or failed")
                break
            nxt = resolve_engine(fallback_name)
        if nxt.label in used or nxt.label == chosen.label:
            errors.append(f"{chosen.label} empty or failed")
            break
        await emit("system", failover_log(chosen.label, nxt.label), stream_kind="ack")
        current = nxt
    detail = "; ".join(errors) if errors else "no live engines with usable API keys"
    state["router_exhausted"] = detail
    await emit("error", f"All API providers failed: {detail}")
    return False


async def healthy_workers(engines: list[Engine]) -> list[Engine]:
    picked: list[Engine] = []
    seen: set[str] = set()
    for engine in engines[:4]:
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


async def _publish_turn_mutations() -> None:
    cards = get_mutations().turn_cards()
    for card in cards:
        await emit("file", json.dumps(card, ensure_ascii=False))
        action = str(card.get("action") or "edit")
        if action == "create":
            verb = "Wrote to"
        elif action == "delete":
            verb = "Deleted"
        else:
            verb = "Edit~to"
        await emit("tool", f"{verb} {card['path']}")
    await hub.broadcast(mutation_sync_frame(get_mutations().public()))


def _abort_if_killed() -> None:
    if state.get("kill_requested"):
        raise TurnAborted("killed")


async def run_turn(query: str) -> None:
    query, notice = prepare_prompt(query)
    set_last_command(query)
    get_mutations().begin_turn()
    agent_loop.reset_turn_budget()
    agent_loop.consume_tokens(estimate_tokens(query))
    state["busy"] = True
    state["ttft_ms"] = 0
    state["tps"] = 0.0
    state["elapsed_ms"] = 0
    state["kill_requested"] = False
    state["token_cap_hit"] = False
    state["last_output"] = ""
    state["last_reasoning"] = ""
    state["router_exhausted"] = ""
    await hub.broadcast(state_ack_frame("routing", "Routing to a live provider"))
    await hub.broadcast(snapshot())
    if notice:
        await emit("user", notice)
    await emit("user", query)
    await persist_conversation("user", query)

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
        await emit("system", f"Combo {custom_model_manager.route_mode} {kind} {workers[0].label}", stream_kind="ack")
    state["engine"] = (
        f"parallel:{len(workers)}" if len(workers) > 1 and not combo_failover else (engine.litellm_model or "fallback")
    )
    if combo_pool:
        state["engine"] = f"combo:{custom_model_manager.route_mode.lower()}:{workers[0].label}"
    state["live"] = len(workers) > 0

    try:
        await hub.broadcast({"type": "busy", "kind": "thinking", "label": THINKING_LABEL})
        tool_name = select_tool(query)
        prompt_intent = classify_intent(query)
        index = get_index()
        canvas = build_canvas(
            query,
            mode=state["mode"],
            engine=state["engine"],
            tool=tool_name,
            live=state["live"],
            ssot=f"{index.stats['files']} files / {index.stats['chunks']} chunks",
        )
        await emit("reasoning", canvas)
        if str(state.get("mode") or "act") in {"plan", "review"}:
            await asyncio.sleep(0.65)

        await hub.broadcast({"type": "busy", "kind": "memory", "label": MEMORY_LABEL})
        window = working_window(int(state.get("context_window") or DEFAULT_CONTEXT_WINDOW))
        directives = instruction_interceptor.consume_pending_instructions()
        if str(state.get("mode") or "act") not in {"plan", "review"} and tool_name not in {
            "Conversation",
            "Direct Execution",
        }:
            directives = "\n".join(part for part in (directives, ACT_SILENT_DIRECTIVE) if part)
        if tool_name == "Conversation" or prompt_intent == "inquiry":
            memory_blob = ""
            hits = []
            await emit("memory", "Inquiry — skipped SSOT workspace dump")
        else:
            packed = await asyncio.to_thread(
                pack_ssot_context,
                query,
                window=window,
                extra_directives=directives,
                model_capacity=_model_capacity(),
            )
            await remember_packed(packed)
            memory_blob = packed.text
            hits = packed.hits
            agent_loop.consume_tokens(int(packed.packed))
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
                        for hit in hits[:16]
                    ],
                    "window": packed.configured,
                    "packed": packed.packed,
                    "timestamp": now(),
                }
            )
        if str(state.get("mode") or "act") in {"plan", "review"}:
            await asyncio.sleep(0.42)

        await hub.broadcast({"type": "busy", "kind": "tool", "label": TOOL_LABEL})
        await emit("tool", tool_name)
        mode_now = str(state.get("mode") or "act")
        allow_writes = mode_now not in {"plan", "review"}
        manual = agent_loop.control_mode == "manual"
        allow_shell = allow_writes and not manual
        if manual and tool_name == "Command Runner":
            await emit("user", "Manual mode — confirm to run the shell command.")
            allow_shell = await agent_loop.request_heal_approval(
                "Command Runner", {"reason": "shell"}
            )
        term_open = {"on": False}

        async def on_term_line(line: str) -> None:
            from robber.ansi import parse_ansi

            raw_line = line.rstrip("\r\n")
            plain, segs = parse_ansi(raw_line)
            if not plain and not raw_line:
                return
            await emit(
                "tool",
                plain if plain.endswith("\n") else f"{plain}\n",
                append=term_open["on"],
                stream_kind="terminal",
                ansi=segs,
                ansi_raw=raw_line,
            )
            term_open["on"] = True

        result = await execute_tool(
            query,
            supervisor,
            allow_writes=allow_writes,
            allow_shell=allow_shell,
            on_line=on_term_line,
        )
        agent_loop.last_tool = result
        agent_loop.consume_tokens(estimate_tokens(result.output or ""))
        _abort_if_killed()
        await emit("tool", f"{result.name} {'ok' if result.ok else 'blocked'}")
        if str(state.get("mode") or "act") in {"plan", "review"}:
            await asyncio.sleep(0.72)

        mcp_blob = get_mcp_host().prompt_catalog()
        from robber.router import clip_tool_context

        if tool_name == "Conversation" or prompt_intent == "inquiry":
            tool_context = (
                "INQUIRY / META QUESTION. Answer in 1-3 natural sentences confirming "
                "capabilities or explaining steps. Do not execute file tools, do not "
                "emit diffs or file cards, and do not invent dummy files "
                "(NodeA.ts, architecture trees, package.json, git logs)."
            )
        else:
            raw_context = f"{memory_blob}\n\n{mcp_blob}\n\n{result.name}\n{result.output}"
            tool_context = clip_tool_context(raw_context)
            tool_context = clip_to_token_budget(tool_context or "", window)
        orch = get_orchestrator()
        hybrid = bool(state.get("hybrid")) or state.get("model") == HYBRID_LABEL
        if prompt_intent == "inquiry":
            hybrid = False
        if orch is not None and hybrid:
            orch_plan = await orch.resolve(
                query,
                mode=str(state.get("mode") or "act"),
                hybrid=hybrid,
                model=str(state.get("model") or ""),
                batch=str(state.get("batch") or ""),
            )
        else:
            from robber.orchestrator import OrchestrationPlan, OrchestrationStage

            orch_plan = OrchestrationPlan(
                hybrid=False,
                stages=[OrchestrationStage("single", workers)],
            )
        produced = False
        brain = get_distributed_brain()
        assignment = None
        task_kind = custom_model_manager.classify_task(query, str(state.get("mode") or "act"))
        use_brain = should_use_distributed_brain(
            query, tool_name=tool_name, task_kind=task_kind
        )
        if brain is not None and use_brain:
            assignment = await brain.assign(
                query,
                mode=str(state.get("mode") or "act"),
                hybrid=hybrid,
                model=str(state.get("model") or ""),
                batch=str(state.get("batch") or ""),
            )
        if assignment is not None and assignment.parallel and not state.get("kill_requested"):
            if use_brain:
                await emit("user", assignment.status_line())
                await hub.broadcast({"type": "busy", "kind": "thinking", "label": "Distributed Brain..."})
                labels = [group[0].label for group in assignment.engines.values() if group]
                if labels:
                    state["engine"] = "brain:" + "+".join(labels[:3])

                def _note_ttft(ms: int) -> None:
                    current = int(state.get("ttft_ms") or 0)
                    if current <= 0 or ms < current:
                        state["ttft_ms"] = ms

                merged = await brain.run(
                    query,
                    mode=str(state.get("mode") or "act"),
                    assignment=assignment,
                    tool_context=tool_context,
                    emit=emit,
                    on_ttft=_note_ttft,
                )
                if merged.strip():
                    agent_loop.consume_tokens(estimate_tokens(merged))
                    await emit("assistant", merged)
                    produced = True
                else:
                    await emit(
                        "error",
                        "Distributed Brain: all specialists failed — trying the primary engine.",
                    )
        if not produced and orch_plan.hybrid:
            reasoning = await healthy_workers(orch_plan.stages[0].engines)
            coding = (
                await healthy_workers(orch_plan.stages[1].engines)
                if len(orch_plan.stages) > 1
                else []
            )
            if not coding:
                coding = reasoning
            if reasoning or coding:
                left = reasoning[0].label if reasoning else "none"
                right = coding[0].label if coding else "none"
                state["engine"] = f"hybrid:{left}->{right}"
            exec_mode = "plan" if str(state.get("mode") or "act") in {"plan", "review"} else "act"
            await hub.broadcast({"type": "busy", "kind": "thinking", "label": STREAM_LABEL})
            code_query = (
                f"{query}\n\n{ACT_SILENT_DIRECTIVE} Prefer unified diffs and JSON file cards."
                if exec_mode == "act" and prompt_intent != "inquiry"
                else query
            )
            for item in coding:
                if state.get("kill_requested"):
                    break
                produced = await stream_with_failover(
                    code_query, exec_mode, item, tool_context, kind="coding"
                )
                if produced:
                    break
            if not produced and not state.get("kill_requested") and not state.get("token_cap_hit") and not state.get("router_exhausted"):
                fallback = compose_reply(query, result, memory_blob)
                agent_loop.consume_tokens(estimate_tokens(fallback))
                await emit("assistant", fallback)
        elif not produced:
            routed = await healthy_workers(orch_plan.stages[0].engines or workers)
            if routed:
                await hub.broadcast({"type": "busy", "kind": "thinking", "label": STREAM_LABEL})
                produced = await stream_with_failover(
                    query, state["mode"], routed[0], tool_context, kind=orch_plan.kind
                )
            elif not state.get("router_exhausted"):
                detail = "no live engines with usable API keys"
                state["router_exhausted"] = detail
                await emit("error", f"All API providers failed: {detail}")
            if not produced and not state.get("kill_requested") and not state.get("token_cap_hit") and not state.get("router_exhausted"):
                fallback = compose_reply(query, result, memory_blob)
                agent_loop.consume_tokens(estimate_tokens(fallback))
                await emit("assistant", fallback)

        if (
            not str(state.get("last_output") or "").strip()
            and not state.get("kill_requested")
            and not state.get("token_cap_hit")
            and not state.get("router_exhausted")
        ):
            fallback = compose_reply(query, result, memory_blob)
            agent_loop.consume_tokens(estimate_tokens(fallback))
            await emit("assistant", fallback)

        _abort_if_killed()

        token_paused = bool(state.get("token_cap_hit")) or agent_loop.budget_exceeded()
        if token_paused:
            await emit(
                "user",
                f"Turn paused — {agent_loop._turn_tokens:,} / "
                f"{agent_loop.max_tokens_per_turn:,} tokens. Approve to keep workspace changes.",
            )
            ok = await agent_loop.request_heal_approval(
                str(state.get("last_output") or "")[:4000] or "token cap",
                {"reason": "token_cap"},
                force=True,
            )
            if not ok:
                raise TurnAborted("token_cap_rejected")
            await emit("system", "Token cap approved — workspace preserved.")

        mode = str(state.get("mode") or "act")
        pending = int(get_mutations().public().get("pending") or 0)
        if mode not in {"plan", "review"} and prompt_intent != "inquiry":
            output = str(state.get("last_output") or "")
            if agent_loop.control_mode == "manual" and pending > 0:
                await emit("user", "Manual mode — Accept All to commit file mutations.")
                ok = await agent_loop.request_heal_approval(
                    output[:4000] or "pending file mutations",
                    {"reason": "file_mutations"},
                )
                if ok:
                    get_mutations().apply_from_text(output)
                else:
                    await emit("system", "File mutations skipped (rejected or timed out).")
            elif agent_loop.control_mode != "manual":
                get_mutations().apply_from_text(output)
        await _publish_turn_mutations()

        await emit("success", "Turn paused at token cap" if token_paused else "Turn complete")
    except asyncio.CancelledError:
        get_mutations().rollback_turn()
        raise
    except TurnAborted as abort:
        note = get_mutations().rollback_turn()
        await emit("system", f"{note} ({abort.reason})")
    except Exception as exc:
        get_mutations().rollback_turn()
        await emit("error", f"Turn failed ({type(exc).__name__}): {exc}")
        await persist_conversation("error", f"Turn failed ({type(exc).__name__}): {exc}")
        if not str(state.get("last_output") or "").strip():
            await emit(
                "assistant",
                f"I received your message ({len(query)} characters) but the turn stopped ({type(exc).__name__}).",
            )
    else:
        get_mutations().commit_turn()
    finally:
        output = str(state.get("last_output") or "").strip()
        if output:
            await persist_conversation("assistant", output)
        state["elapsed_ms"] = get_mutations().elapsed_ms()
        set_elapsed_ms(int(state["elapsed_ms"]))
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


async def apply_dashboard_mode(raw: str) -> None:
    parsed = parse_dashboard_mode(raw)
    notices: list[str] = []
    if parsed.get("beast"):
        state["mode"] = "act"
        state["hybrid"] = False
        value = await agent_loop.set_control_mode("autonomous")
        state["control_mode"] = value
        state["batch"] = save_batch("heavy")
        custom_model_manager.set_route_mode("BEST")
        notices.append("Beast Mode — autonomous act + BEST routing")
    else:
        if parsed.get("mode"):
            state["mode"] = parsed["mode"]
            notices.append(f"Mode switched to {parsed['mode']}")
        if parsed.get("control"):
            value = await agent_loop.set_control_mode(str(parsed["control"]))
            state["control_mode"] = value
            notices.append(f"Control suite {value}")
        if parsed.get("hybrid") is True:
            state["hybrid"] = True
            names = display_models()
            wanted = parsed.get("model")
            if wanted in names:
                state["model"] = wanted
            notices.append("Hybrid Multi-LLM pipeline armed")
        elif parsed.get("hybrid") is False:
            state["hybrid"] = False
        if parsed.get("tier"):
            custom_model_manager.set_route_mode(str(parsed["tier"]))
            notices.append(f"Route tier {parsed['tier']}")
    if not notices:
        await emit("error", f"Unknown dashboard mode '{raw}'")
        return
    for note in notices:
        await emit("user", note)
    await hub.broadcast(snapshot())


async def dispatch(sender: WebSocket, raw: str) -> None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await hub.send_to(sender, error_frame("Malformed JSON"))
        return

    if not isinstance(data, dict):
        await hub.send_to(sender, error_frame("Payload must be a JSON object"))
        return

    message_type = data.get("type") or data.get("action") or data.get("event")
    if not isinstance(message_type, str):
        message_type = ""
    message_type = message_type.strip()
    aliases = {
        "EXECUTE_PROMPT": "prompt",
        "execute_prompt": "prompt",
        "SET_MODE": "set_dashboard_mode",
        "TRIGGER_STEP_ACTION": "step_action",
        "trigger_step_action": "step_action",
        "KILL_TASK": "purge",
        "kill_task": "purge",
        "DISCOVER_MODELS": "discover_models",
        "discover": "discover_models",
        "INGEST_PASTE": "ingest_paste",
        "ingest-paste": "ingest_paste",
        "GET_HISTORY": "get_history",
        "history": "get_history",
        "CLEAR_HISTORY": "clear_history",
    }
    message_type = aliases.get(message_type, message_type)
    if message_type == "get_history":
        await hub.send_to(sender, await history_frame(data.get("session_id")))
        return
    if message_type == "clear_history":
        sid = _session_id_of(data.get("session_id"))
        try:
            await database.clear_conversation_history(sid)
        except Exception:
            pass
        await hub.send_to(sender, {"type": "history", "session_id": sid, "turns": [], "timestamp": now()})
        return
    if message_type == "set_mode" and str(data.get("mode") or "") not in MODES:
        message_type = "set_dashboard_mode"
    if message_type == "prompt" and not data.get("text"):
        data = {**data, "text": str(data.get("prompt") or data.get("content") or ""), "type": "prompt"}

    if message_type == "set_dashboard_mode":
        raw_mode = data.get("mode")
        payload = data.get("payload")
        if isinstance(payload, dict) and not raw_mode:
            raw_mode = payload.get("mode")
        await apply_dashboard_mode(str(raw_mode or ""))
        return

    if message_type == "step_action":
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        action = str(data.get("action") or payload.get("action") or data.get("step") or "")
        command = step_action_command(action)
        if not command:
            await hub.send_to(sender, error_frame(f"Unknown step action '{action}'"))
            return
        await run_slash_command(command=command, args=[], session_id="current_session", sender=sender)
        return

    if message_type == "USER_PROMPT":
        try:
            message = UserPromptMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid USER_PROMPT: {exc.errors()[0]['msg']}"))
            return
        
        prepared, notice = prepare_prompt(message.prompt)
        if notice:
            await emit("user", notice)
        payload = v_ssot_engine.build_context_payload(prepared, message.pinned_anchors)
        directives = instruction_interceptor.consume_pending_instructions()
        config = model_router.get_route_config(message.mode)
        
        system_instr = f"{config.get('default_system_prompt', '')}\n\n{directives}\n\nContext:\n{payload['payload_text']}"
        
        async for chunk in gemini_stream_engine.stream_completion(
            prepared, 
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
        try:
            async with turn_lock:
                await run_turn(text)
        except Exception as exc:
            await emit("error", f"Turn failed ({type(exc).__name__}): {exc}")

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

    elif message_type in {"undo", "undo_step"}:
        await run_slash_command(command="undo", args=[], session_id="current_session", sender=sender)

    elif message_type in {"redo", "redo_step"}:
        await run_slash_command(command="redo", args=[], session_id="current_session", sender=sender)

    elif message_type in {"accept", "accept_all"}:
        await run_slash_command(command="accept", args=[], session_id="current_session", sender=sender)

    elif message_type in {"revert", "revert_all"}:
        await run_slash_command(command="revert", args=[], session_id="current_session", sender=sender)

    elif message_type == "keys_status":
        await emit("system", keys_report())

    elif message_type == "purge":
        state["kill_requested"] = True
        try:
            message = PurgeMessage.model_validate({**data, "type": "purge"})
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
            watcher = get_ssot_watcher()
            watch = watcher.public_stats() if watcher is not None else {}
            last_watch = watch.get("last_watch") if isinstance(watch, dict) else None
            await emit("memory", "SSOT watcher")
            await emit(
                "assistant",
                "watching {watching} · events {watch_events} · last {last}".format(
                    watching="yes" if (isinstance(watch, dict) and watch.get("watching")) else "no",
                    watch_events=(watch.get("watch_events") if isinstance(watch, dict) else 0) or 0,
                    last=time.strftime("%H:%M:%S", time.localtime(float(last_watch)))
                    if last_watch
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
        if (message.base_url or "").strip():
            result = await discover_and_ingest(
                provider_id=message.provider,
                base_url=message.base_url,
                api_key=message.api_key or "",
                extra_headers=_header_map(message.custom_headers),
                force=True,
            )
            await _notify_discovery(result)
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

    elif message_type == "discover_models":
        try:
            message = DiscoverModelsMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid discover_models: {exc.errors()[0]['msg']}"))
            return
        if (message.base_url or "").strip():
            result = await discover_and_ingest(
                provider_id=message.provider_id or "custom",
                base_url=message.base_url,
                api_key=message.api_key or "",
                force=True,
            )
            await _notify_discovery(result)
        else:
            results = await custom_model_manager.discover_known(force=message.force)
            count = sum(item.active_count for item in results if item.ok)
            await emit("user", discovery_log(count))
        await hub.broadcast(snapshot())

    elif message_type == "patch_discovery":
        try:
            message = PatchDiscoveryMessage.model_validate(data)
        except ValidationError as exc:
            await hub.send_to(sender, error_frame(f"Invalid patch_discovery: {exc.errors()[0]['msg']}"))
            return
        model_id = message.model_id.strip()
        if not model_id:
            await hub.send_to(sender, error_frame("model_id is required"))
            return
        get_discovery().set_override(
            model_id,
            disabled=message.disabled,
            priority_high=message.priority_high,
        )
        get_failover_pool().hydrate_from_cache()
        await emit("system", f"Discovery override updated: {model_id}")
        await hub.broadcast(snapshot())

    elif message_type == "ingest_paste":
        raw_text = sanitize_paste(str(data.get("text") or data.get("prompt") or data.get("content") or ""))
        if not raw_text.strip():
            await hub.send_to(sender, error_frame("paste text is empty"))
            return
        stats = save_paste_buffer(raw_text)
        from robber.paste_buffer import ingest_notice

        await emit("user", ingest_notice(stats))
        await hub.send_to(sender, {"type": "paste_ingested", **public_stats(stats), "timestamp": now()})
        await hub.broadcast(snapshot())

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
                "mcp_disable, mcp_add, mcp_remove, mcp_call, discover_models, "
                "patch_discovery, ingest_paste or slash_command"
            ),
        )


agent_loop.bind_runtime(
    run_turn=run_turn,
    dispatch=dispatch,
    turn_lock=turn_lock,
    snapshot=snapshot,
)


def _bind_agent_runtime() -> None:
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
        "ws_dashboard": "/ws/dashboard",
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


class TerminalIn(BaseModel):
    command: str = ""
    text: str = ""
    timeout: float = 60.0
    pid: int | None = None


@app.post("/api/terminal/exec")
async def terminal_exec(payload: TerminalIn) -> dict[str, Any]:
    from robber.tools.command_guard import CommandBlockedError
    from robber.tools.terminal_engine import get_terminal_engine

    command = (payload.command or payload.text or "").strip()
    if not command:
        raise HTTPException(status_code=400, detail="command is empty")
    engine = get_terminal_engine(workspace_root())
    try:
        result = await engine.run(command, timeout=min(120.0, max(1.0, float(payload.timeout or 60))))
    except CommandBlockedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await emit("tool", result.output, stream_kind="terminal", ansi_raw=result.raw)
    return {
        "ok": not result.timed_out,
        "output": result.output,
        "raw": result.raw,
        "backend": result.backend,
        "timed_out": result.timed_out,
        "pid": result.pid,
    }


@app.post("/api/terminal/stdin")
async def terminal_stdin(payload: TerminalIn) -> dict[str, Any]:
    from robber.tools.terminal_engine import get_terminal_engine

    text = payload.text or payload.command
    engine = get_terminal_engine(workspace_root())
    await asyncio.to_thread(engine.ensure_shell)
    engine.write_stdin(text)
    return {"ok": True}


@app.post("/api/terminal/kill")
async def terminal_kill(payload: TerminalIn) -> dict[str, Any]:
    from robber.tools.terminal_engine import close_terminal_engine, peek_terminal_engine

    if payload.pid:
        engine = peek_terminal_engine()
        ok = bool(engine and engine.kill_process_group(int(payload.pid)))
        return {"ok": ok}
    close_terminal_engine()
    return {"ok": True}


class PolicyActionIn(BaseModel):
    id: str | None = None
    prefix: str | None = None


@app.get("/api/policy/pending")
async def policy_list_pending() -> dict[str, Any]:
    from robber.tools.command_policy import get_command_policy_engine

    engine = get_command_policy_engine(workspace_root())
    return {"pending": engine.list_pending()}


@app.post("/api/policy/approve")
async def policy_approve(payload: PolicyActionIn) -> dict[str, Any]:
    from robber.tools.command_policy import get_command_policy_engine

    engine = get_command_policy_engine(workspace_root())
    ok = engine.approve(payload.id)
    return {"ok": ok}


@app.post("/api/policy/deny")
async def policy_deny(payload: PolicyActionIn) -> dict[str, Any]:
    from robber.tools.command_policy import get_command_policy_engine

    engine = get_command_policy_engine(workspace_root())
    ok = engine.deny(payload.id)
    return {"ok": ok}


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
    await _notify_discovery(custom_model_manager.last_discovery)
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
    await _notify_discovery(custom_model_manager.last_discovery)
    await hub.broadcast(snapshot())
    return {"status": "ok", "id": entry_id}


@app.get("/api/models/discovered")
async def list_discovered_models() -> dict[str, Any]:
    return {
        "discovery": get_discovery().public_summary(),
        "failover_pool": get_failover_pool().public(),
    }


@app.post("/api/models/discover")
async def discover_models_endpoint(payload: DiscoverIn | None = None) -> dict[str, Any]:
    message = payload or DiscoverIn()
    if (message.base_url or "").strip():
        result = await discover_and_ingest(
            provider_id=message.provider_id or "custom",
            base_url=message.base_url,
            api_key=message.api_key or "",
            force=True,
        )
        await _notify_discovery(result)
        await hub.broadcast(snapshot())
        return {"discovery": result.public(), "failover_pool": get_failover_pool().public()}
    results = await custom_model_manager.discover_known(force=message.force)
    count = sum(item.active_count for item in results if item.ok)
    await emit("user", discovery_log(count))
    await hub.broadcast(snapshot())
    return {
        "count": count,
        "endpoints": [item.public() for item in results],
        "failover_pool": get_failover_pool().public(),
    }


@app.post("/api/models/discovery/override")
async def discovery_override_endpoint(payload: DiscoveryOverrideIn) -> dict[str, Any]:
    model_id = payload.model_id.strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="model_id is required")
    get_discovery().set_override(
        model_id,
        disabled=payload.disabled,
        priority_high=payload.priority_high,
    )
    get_failover_pool().hydrate_from_cache()
    await hub.broadcast(snapshot())
    return {"ok": True, "model_id": model_id, "failover_pool": get_failover_pool().public()}


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
    if any(key in updates for key in ("base_url", "api_key", "custom_headers", "provider_preset")):
        await _notify_discovery(custom_model_manager.last_discovery)
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


def _wants_sse(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    return "text/event-stream" in accept


async def _sse_event_stream():
    queue = hub.subscribe_sse()
    try:
        ack = state_ack_frame("ready", "Routing to a live provider")
        yield f"data: {json.dumps(ack, default=str)}\n\n"
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=20.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if not frame:
                continue
            yield f"data: {json.dumps(frame, default=str)}\n\n"
    finally:
        hub.unsubscribe_sse(queue)


@app.get("/api/system/stream")
async def system_stream() -> StreamingResponse:
    return StreamingResponse(_sse_event_stream(), media_type="text/event-stream")


@app.get("/api/system/health")
async def system_health(request: Request, reclaim: int = 0) -> Any:
    if _wants_sse(request):
        return StreamingResponse(_sse_event_stream(), media_type="text/event-stream")
    payload = await agent_loop.collect_health()
    if reclaim:
        from robber.telemetry import reclaim_local

        payload["reclaim"] = reclaim_local()
    return payload


class AgentTaskIn(BaseModel):
    prompt: str = ""
    title: str = ""
    text: str = ""
    type: str = ""
    task_id: str = ""
    task_type: str = ""


@app.post("/api/v1/agent/task")
async def python_agent_task(body: AgentTaskIn) -> JSONResponse:
    text = (body.prompt or body.text or body.title).strip()
    if not text:
        raise HTTPException(status_code=400, detail="prompt is empty")

    async def _run() -> None:
        try:
            await agent_loop.execute_prompt(text)
        except Exception:
            logger.exception("gateway task failed")

    asyncio.create_task(_run())
    return JSONResponse(
        {
            "id": body.task_id or "brain",
            "status": "accepted",
            "forwarded": True,
            "task_type": body.task_type or "FAST_DRAFT",
        },
        status_code=202,
    )


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


@app.post("/api/context/paste")
async def ingest_paste_endpoint(payload: PasteIn) -> dict[str, Any]:
    text = sanitize_paste(payload.text)
    if not text.strip():
        raise HTTPException(status_code=400, detail="paste text is empty")
    stats = save_paste_buffer(text)
    from robber.paste_buffer import ingest_notice

    await emit("user", ingest_notice(stats))
    await hub.broadcast(snapshot())
    return public_stats(stats)


@app.get("/api/history")
async def get_history_endpoint(session_id: str = "current_session", limit: int = 200) -> dict[str, Any]:
    frame = await history_frame(session_id, limit=limit)
    return {"session_id": frame["session_id"], "turns": frame["turns"]}


@app.delete("/api/history")
async def clear_history_endpoint(session_id: str = "current_session") -> dict[str, Any]:
    sid = _session_id_of(session_id)
    try:
        await database.clear_conversation_history(sid)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "session_id": sid, "turns": []}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await hub.connect(websocket)
    # Heartbeat task
    heartbeat_task = asyncio.create_task(_heartbeat(websocket))
    
    await hub.send_to(websocket, snapshot())
    await hub.send_to(websocket, agent_loop.loop_state_frame())
    await hub.send_to(websocket, await history_frame())
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


@app.websocket("/ws/dashboard")
async def websocket_dashboard(websocket: WebSocket) -> None:
    await websocket_endpoint(websocket)

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
    try:
        from robber.security.vault import get_vault

        get_vault()
    except Exception:
        pass
    try:
        from robber.local.ollama_adapter import get_adapter

        await asyncio.to_thread(get_adapter().scan)
    except Exception:
        pass
    try:
        await asyncio.to_thread(get_index().reindex)
    except Exception:
        pass
    try:
        await hub.broadcast(snapshot())
    except Exception:
        pass
    try:
        watcher = get_ssot_watcher()
        if watcher is not None:
            await watcher.start(on_refresh=_on_ssot_watch)
    except Exception:
        pass
    try:
        from robber.memory.ram_guard import get_ram_guard

        get_ram_guard().start(hub=hub)
    except Exception:
        pass
    try:
        from robber.engine.recovery import SnapshotRecoveryEngine
        from robber.jail import workspace_root

        text = SnapshotRecoveryEngine(workspace_root(), hub=hub).emit_crash_prompt()
        if text:
            await emit("system", text)
    except Exception:
        pass


@app.on_event("startup")
async def startup_ssot() -> None:
    _bind_agent_runtime()
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
    asyncio.create_task(_boot_discovery())
    asyncio.create_task(_boot_ssot())
    asyncio.create_task(_boot_mcp())
    _boot_lsp()


def _boot_lsp() -> None:
    try:
        from robber.diagnostics.lsp_security import boot_lsp_config

        boot_lsp_config()
    except Exception:
        pass


async def _boot_mcp() -> None:
    try:
        await get_mcp_host().start(supervisor)
    except Exception:
        pass
    try:
        await hub.broadcast(snapshot())
    except Exception:
        pass


async def _boot_discovery() -> None:
    try:
        results = await custom_model_manager.discover_known(force=False)
        live = [item for item in results if item.ok and not item.from_cache]
        count = sum(item.active_count for item in live)
        if count:
            await emit("user", discovery_log(count))
    except Exception:
        pass
    await hub.broadcast(snapshot())


@app.on_event("shutdown")
async def shutdown_store() -> None:
    try:
        from robber.engine.recovery import SnapshotRecoveryEngine
        from robber.jail import workspace_root

        SnapshotRecoveryEngine(workspace_root()).mark_clean()
    except Exception:
        pass
    try:
        from robber.memory.ram_guard import get_ram_guard

        get_ram_guard().stop()
    except Exception:
        pass
    try:
        watcher = get_ssot_watcher()
        if watcher is not None:
            await watcher.stop()
    except Exception:
        pass
    try:
        if worker_pool is not None:
            await worker_pool.stop_all()
    except Exception:
        pass
    try:
        await get_mcp_host().stop()
    except Exception:
        pass
    try:
        from robber.tools.terminal_engine import close_terminal_engine

        close_terminal_engine()
    except Exception:
        pass
    try:
        await database.close()
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=int(os.environ.get("ROBBER_PORT", "8787")),
        reload=False,
        ws_max_size=32 * 1024 * 1024,
    )
