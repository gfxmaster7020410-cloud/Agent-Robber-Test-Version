"""BYOK model router.

Tries LiteLLM against the first provider that has a key. If nothing is
configured, or the call fails, the caller falls back to the canned reply so
the TUI keeps working offline.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

from robber.vault import get_key, get_named

logger = logging.getLogger(__name__)

# Display name -> ordered (litellm_model, provider) candidates.
MODEL_ROUTES: dict[str, tuple[tuple[str, str], ...]] = {
    "Hybrid Multi-LLM": (
        ("groq/openai/gpt-oss-20b", "groq"),
        ("gemini/gemini-3.6-flash", "gemini"),
        ("openai/gpt-4o-mini", "openai"),
    ),
    "GPT-X / Local": (("ollama/llama3.2", "ollama"),),
    "Claude Opus 4": (("anthropic/claude-sonnet-4-20250514", "anthropic"),),
    "Gemini Ultra": (("gemini/gemini-3.6-flash", "gemini"),),
}

_DEFAULT_HYBRID: tuple[tuple[str, str], ...] = MODEL_ROUTES["Hybrid Multi-LLM"]
DISPLAY_MODELS: tuple[str, ...] = tuple(MODEL_ROUTES.keys())
HYBRID_LABEL = "Hybrid Multi-LLM"
MAX_CONSENSUS = 10
BATCH_CLASSES = ("eco", "normal", "heavy")
PROVIDER_TYPES = ("native", "custom_base", "local_ollama")

# Per-label routing metadata. Secrets never live here.
MODEL_META: dict[str, dict[str, object]] = {}


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
            8192,
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
            "key_env": "",
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
        if self.provider == "ollama":
            return True
        if self.api_base:
            return True
        return self.api_key is not None


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
    fallback = _fallback_from_meta(meta)
    batch_class = str(meta.get("batch_class") or "normal")
    if batch_class not in BATCH_CLASSES:
        batch_class = "normal"
    active = meta.get("active", True) is not False
    key_env = str(meta.get("key_env") or "").strip()
    provider_type = str(meta.get("provider_type") or "native")

    if not active:
        return Engine(display, None, provider, None, api_base, headers, fallback, batch_class)

    key: str | None
    if key_env:
        key = get_named(key_env)
    elif provider == "ollama":
        key = None
    else:
        key = get_key(provider)

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
        return Engine(
            display,
            model_id,
            provider,
            key or "sk-local",
            api_base,
            headers,
            fallback,
            batch_class,
        )

    if key:
        return Engine(display, model_id, provider, key, api_base, headers, fallback, batch_class)
    return Engine(display, None, provider, None, api_base, headers, fallback, batch_class)


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


async def collect_completion(
    engine: Engine,
    query: str,
    mode: str,
    tool_context: str | None = None,
) -> tuple[str, str]:
    reasoning: list[str] = []
    assistant: list[str] = []
    async for role, delta in stream_completion(engine, query, mode, tool_context):
        if role == "reasoning":
            reasoning.append(delta)
        else:
            assistant.append(delta)
    return "".join(reasoning), "".join(assistant)


def _delta_text(delta: object, name: str) -> str:
    value = getattr(delta, name, None)
    return value if isinstance(value, str) else ""


async def stream_completion(
    engine: Engine,
    query: str,
    mode: str,
    tool_context: str | None = None,
) -> AsyncIterator[tuple[str, str]]:
    """Yield `(role, text)` chunks. `role` is `reasoning` or `assistant`."""
    if not engine.live or engine.litellm_model is None:
        return

    try:
        from litellm import acompletion
    except ImportError:
        logger.error("litellm not installed, falling back to Gemini")
        engine = Engine(engine.label, "gemini/gemini-3.6-flash", "gemini", os.environ.get("GEMINI_API_KEY"))
        from litellm import acompletion

    # Check for specific provider initialization if needed,
    # though litellm usually handles this via environment variables.
    if engine.provider == "anthropic" and not engine.api_key:
        logger.error("Anthropic API key missing, falling back to Gemini")
        engine = Engine(engine.label, "gemini/gemini-3.6-flash", "gemini", os.environ.get("GEMINI_API_KEY"))

    messages = [
        {
            "role": "system",
            "content": (
                "You are The Robber Agent, a local coding assistant. "
                f"Operating mode: {mode}. Be concise. Prefer plans and diffs "
                "over rewriting whole files. If you show code, wrap it in a "
                "fenced markdown block with a language tag. Tool output below "
                "was gathered inside a workspace jail — treat it as ground truth. "
                "SSOT hits are local index matches, not secrets."
            ),
        },
    ]
    if tool_context:
        messages.append({"role": "system", "content": f"Tool result:\n{tool_context}"})
    messages.append({"role": "user", "content": query})

    protocol = (engine.protocol_type or "NATIVE").upper()
    if engine.api_base and protocol in {"OPENAI_COMPATIBLE", "ANTHROPIC_COMPATIBLE"}:
        try:
            from robber.engine.protocols import PROTOCOL_ANTHROPIC, stream_anthropic, stream_openai

            model_name = engine.model_string or (engine.litellm_model or "")
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
                model=model_name.split("/", 1)[-1] if model_name.startswith("openai/") else model_name,
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
        "model": engine.litellm_model,
        "messages": messages,
        "stream": True,
        "timeout": 45,
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
