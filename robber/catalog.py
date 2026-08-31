"""User-added model catalog — persisted under .robber, no secrets stored."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from robber.jail import workspace_root
from robber.router import (
    BATCH_CLASSES,
    MODEL_META,
    MODEL_ROUTES,
    PROVIDER_TYPES,
    merge_meta,
    register_route,
    set_hybrid_from_labels,
)
from robber.vault import PROVIDER_ENV, get_key, get_named

ALLOWED_PROVIDERS = set(PROVIDER_ENV) | {"ollama"}
CATALOG_NAME = "models.json"
COST_BY_BATCH = {"eco": 0.05, "normal": 0.35, "heavy": 5.0}
LATENCY_BY_BATCH = {"eco": 45, "normal": 110, "heavy": 240}
LITELLM_PREFIX = {
    "openai": "openai",
    "anthropic": "anthropic",
    "gemini": "gemini",
    "mistral": "mistral",
    "cohere": "cohere",
    "groq": "groq",
    "together": "together_ai",
    "deepseek": "deepseek",
    "opencode": "openai",
    "ollama": "ollama",
}


def catalog_path() -> Path:
    return workspace_root() / ".robber" / CATALOG_NAME


def _read() -> dict[str, Any]:
    path = catalog_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(payload: dict[str, Any]) -> None:
    path = catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _as_headers(value: object) -> dict[str, str]:
    raw: object = value
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("custom headers must be valid JSON") from exc
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise ValueError("custom headers must be a JSON object")
    if len(raw) > 16:
        raise ValueError("too many custom headers")
    out: dict[str, str] = {}
    for key, item in raw.items():
        name = str(key).strip()
        if not name:
            continue
        out[name] = str(item)
    return out


def _normalize_row(row: dict[str, Any]) -> dict[str, Any] | None:
    label = str(row.get("label") or "").strip()
    provider = str(row.get("provider") or "").strip().lower()
    if not label or not provider:
        return None
    batch = str(row.get("batch_class") or "normal").strip().lower()
    if batch not in BATCH_CLASSES:
        batch = "normal"
    ptype = str(row.get("provider_type") or "native").strip().lower()
    if ptype not in PROVIDER_TYPES:
        ptype = "local_ollama" if provider == "ollama" else "native"
    try:
        context_length = int(row.get("context_length") or 8192)
    except (TypeError, ValueError):
        context_length = 8192
    context_length = max(1024, min(context_length, 2_000_000))
    try:
        cost_per_m = float(row.get("cost_per_m") or COST_BY_BATCH[batch])
    except (TypeError, ValueError):
        cost_per_m = COST_BY_BATCH[batch]
    try:
        latency_ms = int(row.get("latency_ms") or LATENCY_BY_BATCH[batch])
    except (TypeError, ValueError):
        latency_ms = LATENCY_BY_BATCH[batch]
    entry_id = str(row.get("id") or "").strip() or str(uuid.uuid5(uuid.NAMESPACE_DNS, label))
    try:
        headers = _as_headers(row.get("custom_headers"))
    except ValueError:
        headers = {}
    return {
        "id": entry_id,
        "label": label,
        "name": str(row.get("name") or label).strip() or label,
        "provider": provider,
        "litellm_model": str(row.get("litellm_model") or "").strip(),
        "provider_type": ptype,
        "base_url": str(row.get("base_url") or row.get("api_base") or "").strip(),
        "batch_class": batch,
        "context_length": context_length,
        "fallback_label": str(row.get("fallback_label") or "").strip(),
        "fallback_model_id": str(row.get("fallback_model_id") or "").strip(),
        "custom_headers": headers,
        "active": row.get("active", True) is not False,
        "key_env": str(row.get("key_env") or "").strip(),
        "cost_per_m": round(cost_per_m, 4),
        "latency_ms": max(0, latency_ms),
    }


def extra_rows() -> list[dict[str, Any]]:
    rows = _read().get("extra", [])
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            normalized = _normalize_row(row)
            if normalized:
                out.append(normalized)
    return out


def consensus_labels() -> list[str]:
    raw = _read().get("consensus", [])
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item.strip()]


def save_extra(rows: list[dict[str, Any]]) -> None:
    payload = _read()
    payload["extra"] = rows
    _write(payload)


def save_consensus(labels: list[str]) -> list[str]:
    used = set_hybrid_from_labels(labels)
    payload = _read()
    payload["consensus"] = used
    _write(payload)
    return used


def _apply_row(row: dict[str, Any]) -> None:
    provider = row["provider"]
    if provider not in ALLOWED_PROVIDERS:
        return
    register_route(
        str(row["label"]),
        provider,
        str(row["litellm_model"]),
        id=row["id"],
        batch_class=row["batch_class"],
        context_length=row["context_length"],
        provider_type=row["provider_type"],
        api_base=row["base_url"],
        custom_headers=row["custom_headers"],
        fallback_label=row["fallback_label"],
        fallback_model_id=row["fallback_model_id"],
        active=row["active"],
        key_env=row["key_env"],
        cost_per_m=row["cost_per_m"],
        latency_ms=row["latency_ms"],
        builtin=False,
    )


def _apply_overrides() -> None:
    raw = _read().get("overrides", {})
    if not isinstance(raw, dict):
        return
    for label, fields in raw.items():
        if not isinstance(fields, dict) or label not in MODEL_ROUTES:
            continue
        allowed = {
            key: fields[key]
            for key in (
                "batch_class",
                "context_length",
                "fallback_label",
                "fallback_model_id",
                "active",
                "cost_per_m",
                "latency_ms",
            )
            if key in fields
        }
        if allowed:
            merge_meta(str(label), **allowed)


def load_catalog() -> None:
    for row in extra_rows():
        _apply_row(row)
    _apply_overrides()
    labels = consensus_labels()
    if labels:
        set_hybrid_from_labels(labels)


def infer_litellm_model(provider: str, model_id: str, provider_type: str) -> str:
    text = model_id.strip()
    if not text:
        raise ValueError("model id is empty")
    if "/" in text:
        return text
    if provider_type == "local_ollama":
        return f"ollama/{text}"
    if provider_type == "custom_base":
        return f"openai/{text}"
    prefix = LITELLM_PREFIX.get(provider, provider)
    return f"{prefix}/{text}"


def _secret_env_name(entry_id: str) -> str:
    compact = entry_id.replace("-", "").upper()[:12]
    if not compact or not compact.isalnum():
        compact = uuid.uuid4().hex[:12].upper()
    return f"ROBBER_MODEL_{compact}_KEY"


def add_model(
    label: str,
    provider: str,
    litellm_model: str,
    *,
    provider_type: str = "native",
    base_url: str = "",
    batch_class: str = "normal",
    context_length: int = 8192,
    fallback_label: str = "",
    fallback_model_id: str = "",
    custom_headers: object = None,
    active: bool = True,
    entry_id: str = "",
    key_env: str = "",
) -> str:
    label = label.strip()
    provider = provider.strip().lower()
    provider_type = provider_type.strip().lower() or "native"
    batch_class = batch_class.strip().lower() or "normal"
    if provider_type not in PROVIDER_TYPES:
        raise ValueError("providerType must be native, custom_base, or local_ollama")
    if batch_class not in BATCH_CLASSES:
        raise ValueError("batchClass must be eco, normal, or heavy")
    if provider_type == "local_ollama":
        provider = "ollama"
    if not label:
        raise ValueError("model label is empty")
    if provider not in ALLOWED_PROVIDERS:
        raise ValueError(f"unknown provider '{provider}'")
    if not litellm_model.strip():
        raise ValueError("litellm_model is empty")
    if label in MODEL_ROUTES:
        raise ValueError(f"model '{label}' already exists")
    if provider_type == "custom_base" and not base_url.strip():
        raise ValueError("base URL is required for custom endpoints")
    headers = _as_headers(custom_headers)

    row = _normalize_row(
        {
            "id": entry_id or str(uuid.uuid4()),
            "label": label,
            "name": label,
            "provider": provider,
            "litellm_model": litellm_model.strip(),
            "provider_type": provider_type,
            "base_url": base_url,
            "batch_class": batch_class,
            "context_length": context_length,
            "fallback_label": fallback_label,
            "fallback_model_id": fallback_model_id,
            "custom_headers": headers,
            "active": active,
            "key_env": key_env,
            "cost_per_m": COST_BY_BATCH[batch_class],
            "latency_ms": LATENCY_BY_BATCH[batch_class],
        }
    )
    if row is None:
        raise ValueError("invalid model row")
    if provider_type == "custom_base" and not row["key_env"]:
        row["key_env"] = _secret_env_name(str(row["id"]))
    _apply_row(row)
    rows = extra_rows()
    rows.append(row)
    save_extra(rows)
    return str(row["label"])


def add_models_from_tags(
    tags: list[str],
    *,
    host: str,
    batch_class: str,
    context_length: int,
    fallback_label: str,
    display_prefix: str = "",
) -> list[str]:
    added: list[str] = []
    for tag in tags:
        name = tag.strip()
        if not name:
            continue
        label = f"{display_prefix.strip()} {name}".strip() if display_prefix.strip() else name
        suffix = 2
        original = label
        while label in MODEL_ROUTES:
            label = f"{original} ({suffix})"
            suffix += 1
        add_model(
            label,
            "ollama",
            infer_litellm_model("ollama", name, "local_ollama"),
            provider_type="local_ollama",
            base_url=host,
            batch_class=batch_class,
            context_length=context_length,
            fallback_label=fallback_label,
        )
        added.append(label)
    if not added:
        raise ValueError("no Ollama models selected")
    return added


def patch_model(
    label: str,
    *,
    batch_class: str | None = None,
    fallback_label: str | None = None,
    fallback_model_id: str | None = None,
    active: bool | None = None,
    context_length: int | None = None,
) -> str:
    label = label.strip()
    if label not in MODEL_ROUTES:
        raise ValueError(f"unknown model '{label}'")
    updates: dict[str, Any] = {}
    if batch_class is not None:
        value = batch_class.strip().lower()
        if value not in BATCH_CLASSES:
            raise ValueError("batchClass must be eco, normal, or heavy")
        updates["batch_class"] = value
    if fallback_label is not None:
        updates["fallback_label"] = fallback_label.strip()
    if fallback_model_id is not None:
        updates["fallback_model_id"] = fallback_model_id.strip()
    if active is not None:
        updates["active"] = active
    if context_length is not None:
        updates["context_length"] = max(1024, min(int(context_length), 2_000_000))
    if not updates:
        return label

    extras = extra_rows()
    found = False
    for row in extras:
        if row["label"] == label:
            row.update(updates)
            found = True
            break
    if found:
        save_extra(extras)
        for row in extras:
            if row["label"] == label:
                _apply_row(row)
                break
        return label

    payload = _read()
    overrides = payload.get("overrides")
    if not isinstance(overrides, dict):
        overrides = {}
    current = overrides.get(label)
    if not isinstance(current, dict):
        current = {}
    current.update(updates)
    overrides[label] = current
    payload["overrides"] = overrides
    _write(payload)
    merge_meta(label, **updates)
    return label


def _key_ready(meta: dict[str, object], provider: str) -> bool:
    ptype = str(meta.get("provider_type") or "native")
    if ptype == "local_ollama" or provider == "ollama":
        return True
    key_env = str(meta.get("key_env") or "")
    if key_env:
        return get_named(key_env) is not None
    if ptype == "custom_base":
        return True
    return get_key(provider) is not None


def providers_status() -> dict[str, bool]:
    """Check availability status for key providers."""
    status: dict[str, bool] = {}
    for provider in ALLOWED_PROVIDERS:
        if provider == "ollama":
            status[provider] = True
        else:
            status[provider] = get_key(provider) is not None
    return status


def configured_providers() -> list[str]:
    """Returns a list of providers that are fully configured."""
    return [provider for provider, ready in providers_status().items() if ready]


def upsert_key(provider: str, secret: str) -> str:
    """Save API key for a provider into environment/vault."""
    provider = provider.strip().lower()
    env_var = PROVIDER_ENV.get(provider, f"ROBBER_{provider.upper()}_KEY")
    os.environ[env_var] = secret
    try:
        from robber.vault import set_key
        set_key(provider, secret)
    except (ImportError, AttributeError):
        pass
    return env_var


def upsert_named(env_var: str, secret: str) -> None:
    """Save a named environment variable key."""
    if env_var:
        os.environ[env_var] = secret
        try:
            from robber.vault import set_named
            set_named(env_var, secret)
        except (ImportError, AttributeError):
            pass


def catalog_snapshot() -> list[dict[str, Any]]:
    extras = {row["label"]: row for row in extra_rows()}
    rows: list[dict[str, Any]] = []
    for label, routes in MODEL_ROUTES.items():
        first = routes[0]
        meta = MODEL_META.get(label, {})
        extra = extras.get(label)
        provider = first[1]
        rows.append(
            {
                "id": str(meta.get("id") or extra.get("id") if extra else f"route-{label}"),
                "label": label,
                "name": str((extra or {}).get("name") or label),
                "litellm_model": first[0],
                "provider": provider,
                "builtin": label not in extras,
                "provider_type": str(meta.get("provider_type") or "native"),
                "base_url": str(meta.get("api_base") or ""),
                "batch_class": str(meta.get("batch_class") or "normal"),
                "context_length": int(meta.get("context_length") or 8192),
                "fallback_label": str(meta.get("fallback_label") or ""),
                "fallback_model_id": str(meta.get("fallback_model_id") or ""),
                "custom_headers": meta.get("custom_headers")
                if isinstance(meta.get("custom_headers"), dict)
                else {},
                "active": meta.get("active", True) is not False,
                "key_ready": _key_ready(meta, provider),
                "health": str(meta.get("health", "unknown")),
                "cost_per_m": float(meta.get("cost_per_m") or COST_BY_BATCH.get(str(meta.get("batch_class") or "normal"), 0.35)),
                "latency_ms": int(meta.get("latency_ms") or LATENCY_BY_BATCH.get(str(meta.get("batch_class") or "normal"), 110)),
            }
        )
    return rows