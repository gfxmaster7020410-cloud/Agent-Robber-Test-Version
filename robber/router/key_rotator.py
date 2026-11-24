"""Key pool with RPM/TPM tracking and 429 cooldown queues.

Secrets stay in memory only. Snapshots expose slot index, never the key.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from robber.config import ROUTER_KEY_COOLDOWN, ROUTER_RPM, ROUTER_TPM
from robber.vault import LOCAL_PROVIDERS, PROVIDER_ENV, canonical_provider, iter_keys, load_dotenv

KeysFn = Callable[[str], list[str]]
ClockFn = Callable[[], float]

logger = logging.getLogger(__name__)

DUMMY_PROVIDERS = frozenset({"fallback"})
BOUND_CLOUD_PROVIDERS = ("gemini", "openrouter", "openai", "groq", "anthropic")
_DEAD_KEY_MARKERS = (
    "your-api-key",
    "your_api_key",
    "changeme",
    "replace-me",
    "sk-xxx",
    "<api_key>",
    "none",
    "null",
)


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


@dataclass
class KeyLease:
    provider: str
    index: int
    secret: str
    fingerprint: str


@dataclass
class KeyState:
    provider: str
    index: int
    fingerprint: str
    cool_until: float = 0.0
    window_start: float = 0.0
    hits: int = 0
    tokens: int = 0
    rate_limits: int = 0


class APIKeyRotator:
    """Rotate keys per provider and park exhausted slots in a cooldown queue."""

    def __init__(
        self,
        *,
        rpm: int = ROUTER_RPM,
        tpm: int = ROUTER_TPM,
        cooldown: float = ROUTER_KEY_COOLDOWN,
        keys_fn: KeysFn | None = None,
        clock: ClockFn | None = None,
    ) -> None:
        self.rpm = max(1, int(rpm))
        self.tpm = max(1, int(tpm))
        self.cooldown_seconds = max(0.1, float(cooldown))
        self._keys_fn = keys_fn or iter_keys
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._cursors: dict[str, int] = {}
        self._state: dict[tuple[str, int], KeyState] = {}
        if keys_fn is None:
            load_dotenv()
        self.refresh_keys()

    def _secrets(self, provider: str) -> list[str]:
        name = canonical_provider(provider)
        found: list[str] = []
        seen: set[str] = set()
        for item in self._keys_fn(name):
            secret = (item or "").strip()
            if not _usable_secret(secret) or secret in seen:
                continue
            seen.add(secret)
            found.append(secret)
        return found

    def refresh_keys(self) -> dict[str, int]:
        """Drop empty / placeholder env slots. Never logs secret values."""
        if self._keys_fn is iter_keys:
            load_dotenv()
        ready: dict[str, int] = {}
        names = list(dict.fromkeys((*BOUND_CLOUD_PROVIDERS, *PROVIDER_ENV)))
        for name in names:
            count = len(self._secrets(name))
            ready[name] = count
            if count == 0 and name in BOUND_CLOUD_PROVIDERS:
                logger.debug("skip %s - API key missing", name)
        self._ready = ready
        return ready

    def has_usable_key(self, provider: str) -> bool:
        name = canonical_provider(provider)
        if name in DUMMY_PROVIDERS or not name:
            return False
        if name in LOCAL_PROVIDERS:
            return True
        return bool(self._secrets(name))

    def get_active_keys(self) -> dict[str, Any]:
        """Public inventory: fingerprints and counts, never secret material."""
        self.refresh_keys()
        rows: dict[str, Any] = {}
        for name in list(PROVIDER_ENV):
            secrets = self._secrets(name)
            rows[name] = {
                "ready": bool(secrets),
                "count": len(secrets),
                "fingerprints": [_fingerprint(item) for item in secrets],
            }
        rows["ollama"] = {
            "ready": False,
            "count": 0,
            "local": True,
            "fingerprints": [],
        }
        try:
            from robber.local.ollama_adapter import get_adapter

            adapter = get_adapter()
            models = adapter.all_models() if any(row.ok for row in adapter.endpoints) else []
            rows["ollama"] = {
                "ready": bool(models),
                "count": len(models),
                "local": True,
                "fingerprints": [],
            }
        except Exception:
            pass
        return rows

    def acquire(self, provider: str) -> KeyLease | None:
        name = canonical_provider(provider)
        if name in DUMMY_PROVIDERS or not name:
            logger.debug("skip %s - route pruned", name or "unknown")
            return None
        if name in LOCAL_PROVIDERS:
            return KeyLease(name, 0, "", "local")
        keys = self._secrets(name)
        if not keys:
            logger.debug("skip %s - API key missing", name)
            return None
        now = self._clock()
        with self._lock:
            count = len(keys)
            start = self._cursors.get(name, 0) % count
            for offset in range(count):
                index = (start + offset) % count
                secret = keys[index]
                state = self._slot(name, index, secret, now)
                if state.cool_until > now:
                    continue
                if state.hits >= self.rpm or state.tokens >= self.tpm:
                    state.cool_until = now + self.cooldown_seconds
                    continue
                self._cursors[name] = (index + 1) % count
                return KeyLease(name, index, secret, state.fingerprint)
        return None

    def record(self, lease: KeyLease, tokens: int = 0, *, status: int = 200) -> None:
        if lease.provider in LOCAL_PROVIDERS or lease.fingerprint == "local":
            return
        now = self._clock()
        with self._lock:
            state = self._slot(lease.provider, lease.index, lease.secret, now)
            state.hits += 1
            state.tokens += max(0, int(tokens))
            if int(status) == 429 or state.hits >= self.rpm or state.tokens >= self.tpm:
                state.rate_limits += 1 if int(status) == 429 else 0
                state.cool_until = now + self.cooldown_seconds

    def cooldown(self, lease: KeyLease, seconds: float | None = None) -> None:
        now = self._clock()
        with self._lock:
            state = self._slot(lease.provider, lease.index, lease.secret, now)
            state.rate_limits += 1
            state.cool_until = now + (
                self.cooldown_seconds if seconds is None else max(0.1, float(seconds))
            )

    def cooling(self, provider: str) -> list[dict[str, Any]]:
        now = self._clock()
        name = (provider or "").strip().lower()
        rows = []
        with self._lock:
            for (prov, index), state in self._state.items():
                if prov != name or state.cool_until <= now:
                    continue
                rows.append(
                    {
                        "provider": prov,
                        "index": index,
                        "fingerprint": state.fingerprint,
                        "retry_in": round(state.cool_until - now, 3),
                    }
                )
        return rows

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            slots = [
                {
                    "provider": state.provider,
                    "index": state.index,
                    "fingerprint": state.fingerprint,
                    "cooling": state.cool_until > now,
                    "hits": state.hits,
                    "tokens": state.tokens,
                    "rate_limits": state.rate_limits,
                }
                for state in self._state.values()
            ]
        return {"rpm": self.rpm, "tpm": self.tpm, "slots": slots}

    def _slot(self, provider: str, index: int, secret: str, now: float) -> KeyState:
        key = (provider, index)
        state = self._state.get(key)
        mark = _fingerprint(secret) if secret else "empty"
        if state is None or state.fingerprint != mark:
            state = KeyState(provider=provider, index=index, fingerprint=mark, window_start=now)
            self._state[key] = state
        if now - state.window_start >= 60.0:
            state.window_start = now
            state.hits = 0
            state.tokens = 0
        return state


def _usable_secret(secret: str) -> bool:
    text = (secret or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered in _DEAD_KEY_MARKERS:
        return False
    if lowered.startswith("your-") or lowered.startswith("insert-"):
        return False
    return True
