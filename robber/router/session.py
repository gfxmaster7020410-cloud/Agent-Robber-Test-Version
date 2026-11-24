"""BYOK model router.

Tries LiteLLM against the first provider that has a key. If nothing is
configured, or the call fails, the caller falls back to the canned reply so
the TUI keeps working offline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Literal

from robber.engine.model_ids import completion_model_ids
from robber.markdown_ansi import ACT_SILENT_DIRECTIVE
from robber.vault import LOCAL_PROVIDERS, get_key, get_named, iter_keys

logger = logging.getLogger(__name__)

# Display name -> ordered (litellm_model, provider) candidates.
_DEFAULT_HYBRID: tuple[tuple[str, str], ...] = (
    ("openrouter/nvidia/nemotron-3.5-lightning:free", "openrouter"),
    ("groq/llama-3.3-70b-versatile", "groq"),
    ("gemini/gemini-2.5-flash", "gemini"),
)
MODEL_ROUTES: dict[str, tuple[tuple[str, str], ...]] = {
    "Hybrid Multi-LLM": _DEFAULT_HYBRID,
    "GPT-X / Local": (("ollama/llama3.2", "ollama"),),
    "Claude Opus 4": (("anthropic/claude-sonnet-4-20250514", "anthropic"),),
    "Gemini Ultra": (("gemini/gemini-2.5-flash", "gemini"),),
    "New API": (("openai/gpt-4o-mini", "new_api"),),
}
DISPLAY_MODELS: tuple[str, ...] = tuple(MODEL_ROUTES.keys())
HYBRID_LABEL = "Hybrid Multi-LLM"
MAX_CONSENSUS = 10
MAX_TOOL_CONTEXT_CHARS = 12_000
BATCH_CLASSES = ("eco", "normal", "heavy")
PROVIDER_TYPES = ("native", "custom_base", "local_ollama")

# Per-label routing metadata. Secrets never live here.
MODEL_META: dict[str, dict[str, object]] = {}

PROVIDER_API_BASE = {
    "omniroute": "https://api.omniroute.ai/v1",
    "tokenrouter": "https://api.tokenrouter.io/v1",
    "token_router": "https://api.tokenrouter.io/v1",
}


def _seed_builtin_meta() -> None:
    specs: tuple[tuple[str, str, str, int, str, float, int, str, str], ...] = (
        (
            "Hybrid Multi-LLM",
            "builtin-hybrid",
            "normal",
            128000,
            "native",
            0.4,
            90,
            "GPT-X / Local",
            "",
        ),
        (
            "GPT-X / Local",
            "builtin-local",
            "eco",
            4096,
            "local_ollama",
            0.0,
            40,
            "",
            "http://127.0.0.1:11434",
        ),
        (
            "Claude Opus 4",
            "builtin-claude",
            "heavy",
            200000,
            "native",
            15.0,
            220,
            "Gemini Ultra",
            "",
        ),
        (
            "Gemini Ultra",
            "builtin-gemini",
            "normal",
            1_000_000,
            "native",
            0.35,
            110,
            "GPT-X / Local",
            "",
        ),
        (
            "New API",
            "builtin-new-api",
            "normal",
            128000,
            "custom_base",
            0.4,
            90,
            "Gemini Ultra",
            os.environ.get("NEWAPI_BASE") or os.environ.get("NEW_API_BASE") or "",
        ),
    )
    for label, entry_id, batch, ctx, ptype, cost, latency, fallback, base in specs:
        MODEL_META[label] = {
            "id": entry_id,
            "batch_class": batch,
            "context_length": ctx,
            "provider_type": ptype,
            "cost_per_m": cost,
            "latency_ms": latency,
            "fallback_label": fallback,
            "fallback_model_id": "",
            "api_base": base,
            "custom_headers": {},
            "active": True,
            "key_env": "NEWAPI_API_KEY" if label == "New API" else "",
            "builtin": True,
        }


_seed_builtin_meta()


def display_models() -> list[str]:
    return list(MODEL_ROUTES.keys())


def label_for_id(entry_id: str) -> str | None:
    wanted = entry_id.strip()
    if not wanted:
        return None
    for label, meta in MODEL_META.items():
        if str(meta.get("id") or "") == wanted:
            return label
    return None


def merge_meta(label: str, **fields: object) -> dict[str, object]:
    current = dict(MODEL_META.get(label, {}))
    for key, value in fields.items():
        if value is not None:
            current[key] = value
    current.setdefault("id", f"route-{label.lower().replace(' ', '-')}")
    current.setdefault("batch_class", "normal")
    current.setdefault("context_length", 8192)
    current.setdefault("provider_type", "native")
    current.setdefault("cost_per_m", 0.0)
    current.setdefault("latency_ms", 0)
    current.setdefault("fallback_label", "")
    current.setdefault("fallback_model_id", "")
    current.setdefault("api_base", "")
    current.setdefault("custom_headers", {})
    current.setdefault("active", True)
    current.setdefault("key_env", "")
    current.setdefault("builtin", False)
    MODEL_META[label] = current
    return current


def register_route(
    label: str,
    provider: str,
    litellm_model: str,
    **meta: object,
) -> None:
    MODEL_ROUTES[label] = ((litellm_model, provider),)
    merge_meta(label, **meta)


def set_hybrid_from_labels(labels: list[str]) -> list[str]:
    """Point Hybrid Multi-LLM at selected engines so one task can fan out."""
    routes: list[tuple[str, str]] = []
    used: list[str] = []
    seen: set[tuple[str, str]] = set()
    for label in labels:
        if label == HYBRID_LABEL:
            continue
        pair = MODEL_ROUTES.get(label)
        if not pair:
            continue
        first = pair[0]
        if first in seen:
            continue
        seen.add(first)
        routes.append(first)
        used.append(label)
        if len(used) >= MAX_CONSENSUS:
            break
    MODEL_ROUTES[HYBRID_LABEL] = tuple(routes) if routes else _DEFAULT_HYBRID
    return used


@dataclass(frozen=True)
class Engine:
    label: str
    litellm_model: str | None
    provider: str
    api_key: str | None
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None
    fallback_label: str | None = None
    batch_class: str = "normal"
    protocol_type: str = "NATIVE"
    model_string: str = ""

    @property
    def live(self) -> bool:
        if self.litellm_model is None:
            return False
        if (self.provider or "").strip().lower() in LOCAL_PROVIDERS:
            return True
        if self.api_base:
            return self.api_key is not None
        return self.api_key is not None


TaskStrength = Literal["index", "parse", "research", "reason", "codegen"]

INDEX_RE = re.compile(r"\b(reindex|index ssot|/sync|mmap|fingerprint)\b", re.I)
PARSE_RE = re.compile(r"\b(ast|parse|symbols|syntax tree)\b", re.I)
RESEARCH_RE = re.compile(r"\b(search|scrape|docs|wikipedia|look up|fetch url)\b", re.I)
REASON_RE = re.compile(r"\b(architect|design|why|reason|plan|trade-?off|hypothesis)\b", re.I)
CODEGEN_RE = re.compile(r"\b(implement|refactor|patch|write|fix|function|class|module)\b", re.I)

STRENGTH_LABELS: dict[TaskStrength, tuple[str, ...]] = {
    "index": ("GPT-X / Local", "Hybrid Multi-LLM"),
    "parse": ("GPT-X / Local", "Gemini Ultra"),
    "research": ("Gemini Ultra", "New API", "Hybrid Multi-LLM"),
    "reason": ("Claude Opus 4", "Gemini Ultra", "Hybrid Multi-LLM"),
    "codegen": ("Claude Opus 4", "Hybrid Multi-LLM", "Gemini Ultra", "New API"),
}

_SESSION: SessionFailover | None = None


def classify_strength(query: str) -> TaskStrength:
    text = query or ""
    if INDEX_RE.search(text):
        return "index"
    if PARSE_RE.search(text):
        return "parse"
    if RESEARCH_RE.search(text):
        return "research"
    if REASON_RE.search(text):
        return "reason"
    if CODEGEN_RE.search(text):
        return "codegen"
    return "research"


def get_session_router() -> SessionFailover:
    global _SESSION
    if _SESSION is None:
        _SESSION = SessionFailover()
    return _SESSION


def bind_session_router(session: SessionFailover | None) -> SessionFailover:
    global _SESSION
    _SESSION = session if session is not None else SessionFailover()
    return _SESSION


class SessionFailover:
    """Keep session identity across key rotation and engine hops."""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.used: set[str] = set()
        self.key_cursors: dict[str, int] = {}
        self.last_engine: Engine | None = None
        self.last_strength: TaskStrength = "research"
        self.rotations = 0
        self.failovers = 0
        self._lock = threading.Lock()

    def public(self) -> dict[str, object]:
        engine = self.last_engine
        return {
            "session_id": self.session_id,
            "strength": self.last_strength,
            "engine": engine.label if engine else "",
            "rotations": self.rotations,
            "failovers": self.failovers,
            "used": sorted(self.used)[:12],
        }

    def key_for(self, provider: str) -> str | None:
        from robber.vault import canonical_provider

        provider = canonical_provider(provider)
        keys = _secured_keys(provider)
        if not keys:
            return None
        with self._lock:
            idx = self.key_cursors.get(provider, 0) % len(keys)
        return keys[idx]

    def rotate_provider(self, provider: str) -> str | None:
        from robber.vault import canonical_provider

        provider = canonical_provider(provider)
        keys = _secured_keys(provider)
        if not keys:
            return None
        with self._lock:
            if len(keys) == 1:
                self.key_cursors[provider] = 0
                return keys[0]
            nxt = (self.key_cursors.get(provider, 0) + 1) % len(keys)
            self.key_cursors[provider] = nxt
            self.rotations += 1
            return keys[nxt]

    def remember(self, engine: Engine, strength: TaskStrength) -> Engine:
        with self._lock:
            self.last_engine = engine
            self.last_strength = strength
            self.used.add(engine.label)
            if engine.model_string:
                self.used.add(engine.model_string)
        return engine

    def next_channel(self, engine: Engine, exc: BaseException | None = None) -> Engine | None:
        reason = _channel_reason(exc)
        if reason == "rate_limit":
            nxt_key = self.rotate_provider(engine.provider)
            if nxt_key and nxt_key != engine.api_key:
                self.failovers += 1
                hop = replace(engine, api_key=nxt_key)
                return self.remember(hop, self.last_strength)
        fallback = (engine.fallback_label or "").strip()
        if fallback and fallback not in self.used:
            hop = resolve_engine(fallback)
            if hop.live and hop.label != engine.label and (
                hop.api_key or hop.provider in {"ollama", "vllm", "lmstudio"}
            ):
                self.failovers += 1
                return self.remember(hop, self.last_strength)
        try:
            from robber.engine.failover_pool import get_failover_pool

            kind = "reasoning" if self.last_strength == "reason" else (
                "coding" if self.last_strength == "codegen" else "general"
            )
            nxt = get_failover_pool().next_after(engine, kind, self.used)
        except Exception:
            nxt = None
        if nxt is None or not nxt.live:
            return None
        if not nxt.api_key and nxt.provider not in {"ollama", "vllm", "lmstudio"}:
            return None
        self.failovers += 1
        return self.remember(nxt, self.last_strength)


def _channel_reason(exc: BaseException | None) -> str:
    if exc is None:
        return "error"
    text = str(exc).lower()
    name = type(exc).__name__
    if "429" in text or "rate limit" in text or name == "RateLimitError":
        return "rate_limit"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in text:
        return "timeout"
    return "error"


def assign_engine(query: str, label: str | None = None, *, batch: str | None = None) -> Engine:
    """Pick a live engine from task strength, then the operator's selected label."""
    session = get_session_router()
    strength = classify_strength(query)
    wanted = (label or "").strip()
    candidates: list[str] = []
    if wanted:
        candidates.append(wanted)
    candidates.extend(STRENGTH_LABELS.get(strength, ()))
    seen: set[str] = set()
    for name in candidates:
        if not name or name in seen:
            continue
        seen.add(name)
        engine = resolve_engine(name, batch=batch)
        if engine.live:
            return session.remember(engine, strength)
    fallback = resolve_engine(wanted or HYBRID_LABEL, batch=batch)
    return session.remember(fallback, strength)


def resolve_engine(label: str, batch: str | None = None) -> Engine:
    live = resolve_live_engines(label, batch=batch)
    if live:
        return live[0]
    return Engine(label, None, "fallback", None)


def _label_for_route(model_id: str, provider: str) -> str:
    for name, routes in MODEL_ROUTES.items():
        if name == HYBRID_LABEL:
            continue
        if routes and routes[0] == (model_id, provider):
            return name
    return f"{provider}/{model_id}"


def _headers_from_meta(meta: dict[str, object]) -> dict[str, str] | None:
    raw = meta.get("custom_headers")
    if not isinstance(raw, dict) or not raw:
        return None
    return {str(key): str(value) for key, value in raw.items()}


def _fallback_from_meta(meta: dict[str, object]) -> str | None:
    label = str(meta.get("fallback_label") or "").strip()
    if label:
        return label
    return label_for_id(str(meta.get("fallback_model_id") or ""))


def _engine_for_route(display: str, model_id: str, provider: str) -> Engine:
    meta = MODEL_META.get(display, {})
    api_base = str(meta.get("api_base") or "").strip() or None
    headers = _headers_from_meta(meta)
    if provider == "openrouter":
        headers = dict(headers or {})
        headers.setdefault("HTTP-Referer", "http://127.0.0.1:5173")
        headers.setdefault("X-Title", "Agent Robber")
    fallback = _fallback_from_meta(meta)
    batch_class = str(meta.get("batch_class") or "normal")
    if batch_class not in BATCH_CLASSES:
        batch_class = "normal"
    active = meta.get("active", True) is not False
    key_env = str(meta.get("key_env") or "").strip()
    provider_type = str(meta.get("provider_type") or "native")
    protocol = "NATIVE"
    if provider in PROVIDER_API_BASE:
        api_base = api_base or PROVIDER_API_BASE[provider]
        protocol = "OPENAI_COMPATIBLE"
    if provider == "new_api" or display == "New API":
        api_base = (
            api_base
            or os.environ.get("NEWAPI_BASE", "").strip()
            or os.environ.get("NEW_API_BASE", "").strip()
            or None
        )

    def make(model: str | None, secret: str | None) -> Engine:
        return Engine(
            display,
            model,
            provider,
            secret,
            api_base,
            headers,
            fallback,
            batch_class,
            protocol,
            model_id if model else "",
        )

    if not active:
        return make(None, None)

    session = get_session_router()
    key: str | None
    if provider == "ollama":
        key = None
    else:
        key = session.key_for(provider) or get_key(provider)
        if key_env and not key:
            key = get_named(key_env)

    if provider == "ollama":
        return Engine(
            display,
            model_id,
            provider,
            None,
            api_base or "http://127.0.0.1:11434",
            headers,
            fallback,
            batch_class,
        )

    if provider_type == "custom_base" and api_base:
        if not key and provider not in LOCAL_PROVIDERS:
            return make(None, None)
        return make(model_id, key or ("sk-local" if provider in LOCAL_PROVIDERS else key))

    if key:
        return make(model_id, key)
    return make(None, None)


def _iter_routes(label: str) -> tuple[tuple[str, str], ...]:
    return MODEL_ROUTES.get(label, MODEL_ROUTES[HYBRID_LABEL])


def _engines_in_batch(batch: str, limit: int) -> list[Engine]:
    live: list[Engine] = []
    for name, routes in MODEL_ROUTES.items():
        if name == HYBRID_LABEL or not routes:
            continue
        if str(MODEL_META.get(name, {}).get("batch_class", "normal")) != batch:
            continue
        first = routes[0]
        engine = _engine_for_route(name, first[0], first[1])
        if engine.live:
            live.append(engine)
        if len(live) >= limit:
            break
    return live


def resolve_live_engines(
    label: str,
    limit: int = MAX_CONSENSUS,
    batch: str | None = None,
) -> list[Engine]:
    """Return up to `limit` live engines for this selector (Hybrid = parallel)."""
    apply_batch = bool(batch) and batch in BATCH_CLASSES and label == HYBRID_LABEL
    live: list[Engine] = []
    seen: set[tuple[str, str]] = set()
    for model_id, provider in _iter_routes(label):
        pair = (model_id, provider)
        if pair in seen:
            continue
        seen.add(pair)
        display = label if label != HYBRID_LABEL else _label_for_route(model_id, provider)
        if apply_batch:
            engine_batch = str(MODEL_META.get(display, {}).get("batch_class", "normal"))
            if engine_batch != batch:
                continue
        engine = _engine_for_route(display, model_id, provider)
        if engine.live:
            live.append(engine)
        if len(live) >= limit:
            break
    if apply_batch and not live:
        return _engines_in_batch(batch or "normal", limit)
    return live


def worker_rows(label: str, batch: str | None = None) -> list[dict[str, str | bool]]:
    apply_batch = bool(batch) and batch in BATCH_CLASSES and label == HYBRID_LABEL
    rows: list[dict[str, str | bool]] = []
    seen: set[tuple[str, str]] = set()
    for model_id, provider in _iter_routes(label)[:MAX_CONSENSUS]:
        if (model_id, provider) in seen:
            continue
        seen.add((model_id, provider))
        display = label if label != HYBRID_LABEL else _label_for_route(model_id, provider)
        if apply_batch:
            engine_batch = str(MODEL_META.get(display, {}).get("batch_class", "normal"))
            if engine_batch != batch:
                continue
        engine = _engine_for_route(display, model_id, provider)
        rows.append(
            {
                "label": display,
                "provider": provider,
                "litellm_model": model_id,
                "live": engine.live,
            }
        )
    if apply_batch and not rows:
        extras: list[dict[str, str | bool]] = []
        for name, routes in MODEL_ROUTES.items():
            if name == HYBRID_LABEL or not routes:
                continue
            if str(MODEL_META.get(name, {}).get("batch_class", "normal")) != batch:
                continue
            first = routes[0]
            engine = _engine_for_route(name, first[0], first[1])
            extras.append(
                {
                    "label": name,
                    "provider": first[1],
                    "litellm_model": first[0],
                    "live": engine.live,
                }
            )
            if len(extras) >= MAX_CONSENSUS:
                break
        return extras
    return rows


def clip_tool_context(text: str | None, limit: int = MAX_TOOL_CONTEXT_CHARS) -> str | None:
    if not text:
        return text
    if len(text) <= limit:
        return text
    return text[: limit - 40].rstrip() + "\n… (context clipped for model limits)"


async def collect_completion(
    engine: Engine,
    query: str,
    mode: str,
    tool_context: str | None = None,
    *,
    ttft_seconds: float | None = None,
    timeout: float | None = None,
) -> tuple[str, str]:
    reasoning: list[str] = []
    assistant: list[str] = []
    agen = stream_completion(engine, query, mode, tool_context, timeout=timeout).__aiter__()

    async def _next() -> tuple[str, str]:
        return await agen.__anext__()

    try:
        if ttft_seconds is not None:
            role, delta = await asyncio.wait_for(_next(), timeout=max(0.2, ttft_seconds))
        else:
            role, delta = await _next()
    except StopAsyncIteration:
        return "", ""
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise TimeoutError("ttft") from exc
    if role == "reasoning":
        reasoning.append(delta)
    else:
        assistant.append(delta)
    try:
        async for role, delta in agen:
            if role == "reasoning":
                reasoning.append(delta)
            else:
                assistant.append(delta)
    except StopAsyncIteration:
        pass
    return "".join(reasoning), "".join(assistant)


AGENT_PERSONA = (
    "You are AGENT ROBBER, an autonomous localized CLI coding agent running natively "
    "on the user's local machine.\n"
    "CORE DIRECTIVES:\n"
    "1. UNRESTRICTED OS ACCESS: You have full access to the entire local filesystem "
    "(all drives and directories). Never claim you are restricted to a workspace jail.\n"
    "2. ACTION & CODE-FIRST RESPONSE: For explicit coding tasks, respond like an expert "
    "terminal-bound developer (e.g. Claude Code, Cursor Agent). Skip empty filler "
    '(e.g. "I am here to help", "Let me know if you need anything else").\n'
    "3. DIRECT EXECUTION: For explicit coding tasks, bug fixes, and feature requests, "
    "jump straight to tool execution, code modifications, or system commands. "
    "For capability or how-to questions, answer in 1-3 sentences of plain text.\n"
    "4. COMPACT & SURGICAL OUTPUT: When writing code, emit precise diffs. "
    "Keep prose high-density.\n"
)

BEHAVIORAL_BOUNDARIES = (
    "SYSTEM DIRECTIVE: BEHAVIORAL BOUNDARIES\n"
    "- NEVER hallucinate or invent dummy projects (e.g., NodeA.ts, my-agent/architecture) "
    "unless specifically instructed to build a project from scratch.\n"
    "- If the user asks a capability or confirmation question, answer directly in standard "
    "text without creating fake code files or execution plans.\n"
    "- Save tool execution and multi-file code writing strictly for explicit coding tasks, "
    "bug fixes, and feature requests.\n"
)


def system_prompt_for(mode: str, intent: str | None = None) -> str:
    key = (mode or "act").strip().lower()
    acting = key in {"act", "autonomous"}
    kind = (intent or "action").strip().lower()
    if kind == "inquiry":
        return (
            f"{AGENT_PERSONA}"
            f"{BEHAVIORAL_BOUNDARIES}"
            "The user asked a capability, confirmation, or how-to question. "
            "Reply in 1-3 sentences of plain text confirming what you can do or how it works. "
            "Do not emit diffs, file cards, directory trees, fake files, or tool plans."
        )
    guidance = (
        f"Operating mode: {mode}. Prefer unified diffs and tool results over plans. "
        f"{ACT_SILENT_DIRECTIVE}"
        if acting
        else (
            f"Operating mode: {mode}. Prefer plans and diffs "
            "over rewriting whole files."
        )
    )
    return (
        f"{AGENT_PERSONA}"
        f"{BEHAVIORAL_BOUNDARIES}"
        f"{guidance} "
        "If you show code, wrap it in a fenced markdown block with a language tag. "
        "Tool output below is ground truth from this machine. "
        "Do not invent package.json, git logs, or workspace dumps unless the user asked. "
        "SSOT hits are local index matches, not secrets."
    )


def _delta_text(delta: object, name: str) -> str:
    value = getattr(delta, name, None)
    return value if isinstance(value, str) else ""


def _secured_keys(provider: str) -> list[str]:
    try:
        from robber.security.vault import iter_secured_keys

        found = iter_secured_keys(provider)
        if found:
            return found
    except Exception:
        pass
    return iter_keys(provider)


async def _complete_local(engine: Engine, query: str, tool_context: str | None) -> str:
    from robber.local.ollama_adapter import get_adapter

    prompt = query or ""
    if tool_context:
        prompt = f"{str(tool_context)[-1500:]}\n{prompt}"
    name = (engine.model_string or engine.litellm_model or "").strip()
    if "/" in name:
        name = name.split("/", 1)[1]
    try:
        return await get_adapter().complete(prompt, model=name, api_base=engine.api_base or "")
    except Exception:
        logger.debug("local completion failed", exc_info=True)
        return ""


async def stream_completion(
    engine: Engine,
    query: str,
    mode: str,
    tool_context: str | None = None,
    timeout: float | None = None,
) -> AsyncIterator[tuple[str, str]]:
    """Yield `(role, text)` chunks. `role` is `reasoning` or `assistant`."""
    if not engine.live or engine.litellm_model is None:
        return
    from robber.vault import LOCAL_PROVIDERS, get_key, load_dotenv

    load_dotenv()
    local = (engine.provider or "").strip().lower() in LOCAL_PROVIDERS
    if not local and not (engine.api_key or "").strip():
        return

    tool_context = clip_tool_context(tool_context)
    if local:
        text = await _complete_local(engine, query, tool_context)
        if text:
            yield "assistant", text
        return

    http_model, litellm_model = completion_model_ids(
        model_string=engine.model_string,
        litellm_model=engine.litellm_model,
        api_base=engine.api_base,
    )

    try:
        from litellm import acompletion
    except ImportError:
        gemini_key = get_key("gemini") or os.environ.get("GEMINI_API_KEY")
        if not gemini_key:
            logger.error("litellm not installed and Gemini API key is missing")
            return
        logger.error("litellm not installed, falling back to Gemini")
        engine = Engine(engine.label, "gemini/gemini-2.5-flash", "gemini", gemini_key)
        from litellm import acompletion

    if engine.provider == "anthropic" and not engine.api_key:
        gemini_key = get_key("gemini") or os.environ.get("GEMINI_API_KEY")
        if not gemini_key:
            logger.error("[ROUTER ERROR] Provider anthropic failed with status 0: API key missing")
            return
        logger.error("Anthropic API key missing, falling back to Gemini")
        engine = Engine(engine.label, "gemini/gemini-2.5-flash", "gemini", gemini_key)

    from robber.intent import classify_intent

    messages = [
        {
            "role": "system",
            "content": system_prompt_for(mode, intent=classify_intent(query)),
        },
    ]
    if tool_context:
        messages.append({"role": "system", "content": f"Tool result:\n{tool_context}"})
    messages.append({"role": "user", "content": query})

    protocol = (engine.protocol_type or "NATIVE").upper()
    if engine.api_base and protocol in {"OPENAI_COMPATIBLE", "ANTHROPIC_COMPATIBLE"}:
        try:
            from robber.engine.protocols import PROTOCOL_ANTHROPIC, stream_anthropic, stream_openai

            model_name = http_model or engine.model_string or (engine.litellm_model or "")
            if protocol == PROTOCOL_ANTHROPIC:
                async for role, delta in stream_anthropic(
                    base_url=engine.api_base,
                    api_key=engine.api_key or "",
                    model=model_name,
                    messages=messages,
                    extra_headers=engine.extra_headers,
                ):
                    yield role, delta
                return
            async for role, delta in stream_openai(
                base_url=engine.api_base,
                api_key=engine.api_key or "",
                model=http_model or (
                    model_name.split("/", 1)[-1] if model_name.startswith("openai/") else model_name
                ),
                messages=messages,
                extra_headers=engine.extra_headers,
            ):
                yield role, delta
            return
        except ValueError:
            pass
        except Exception:
            logger.exception("protocol adapter failed; trying LiteLLM")

    kwargs: dict[str, object] = {
        "model": litellm_model or engine.litellm_model,
        "messages": messages,
        "stream": True,
        "timeout": timeout if timeout is not None else 45,
    }
    if engine.api_key:
        kwargs["api_key"] = engine.api_key
    if engine.api_base:
        kwargs["api_base"] = engine.api_base
    if engine.extra_headers:
        kwargs["extra_headers"] = engine.extra_headers

    stream = await acompletion(**kwargs)
    async for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            continue
        reasoning = _delta_text(delta, "reasoning_content") or _delta_text(
            delta, "thinking"
        )
        content = _delta_text(delta, "content")
        if reasoning:
            yield "reasoning", reasoning
        if content:
            yield "assistant", content
