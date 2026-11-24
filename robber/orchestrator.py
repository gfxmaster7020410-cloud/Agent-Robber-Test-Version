"""Provider-agnostic hybrid / single / failover model gateway."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from robber.config import STREAM_FLUSH_MAX_CHARS, STREAM_FLUSH_MS
from robber.engine.model_manager import CustomModelManager
from robber.engine.presets import batch_to_tier
from robber.router import Engine, HYBRID_LABEL, resolve_engine, resolve_live_engines

_ORCH: ModelOrchestrator | None = None


def http_status_of(exc: BaseException) -> int | None:
    for attr in ("status_code", "status", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            return code
    match = re.search(r"\b(400|401|403|404|408|409|422|429|500|502|503|504)\b", str(exc))
    return int(match.group(1)) if match else None


def is_rate_limited(exc: BaseException) -> bool:
    status = http_status_of(exc)
    text = str(exc).lower()
    return status == 429 or "429" in text or "rate limit" in text or "too many requests" in text


_FAILOVER_EXC_NAMES = {
    "APIError",
    "AuthenticationError",
    "BadRequestError",
    "ConnectTimeout",
    "HTTPStatusError",
    "InternalServerError",
    "NotFoundError",
    "RateLimitError",
    "ReadTimeout",
    "ServiceUnavailableError",
    "Timeout",
}

_TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504}
_TRANSIENT_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "ConnectError",
    "ConnectTimeout",
    "HTTPStatusError",
    "InternalServerError",
    "RateLimitError",
    "ReadError",
    "ReadTimeout",
    "ServiceUnavailableError",
    "Timeout",
    "TimeoutException",
}
_PERMANENT_STATUS = {400, 401, 403, 404, 422}


class StreamThrottle:
    """Coalesce LLM tokens into ~30ms WebSocket chunks."""

    def __init__(
        self,
        window_ms: float = STREAM_FLUSH_MS,
        max_chars: int = STREAM_FLUSH_MAX_CHARS,
    ) -> None:
        self.window = max(0.001, float(window_ms) / 1000.0)
        self.max_chars = max(32, int(max_chars))
        self._buf = ""
        self._opened = 0.0

    def feed(self, text: str, now: float | None = None) -> str:
        if not text:
            return ""
        clock = time.monotonic() if now is None else now
        if not self._buf:
            self._opened = clock
        self._buf += text
        elapsed = clock - self._opened
        if len(self._buf) >= self.max_chars or elapsed >= self.window:
            return self.flush()
        return ""

    def flush(self) -> str:
        chunk = self._buf
        self._buf = ""
        self._opened = 0.0
        return chunk


def is_transient_error(exc: BaseException) -> bool:
    """Network / 5xx / 429 failures that are worth a local retry before failover."""
    if isinstance(exc, asyncio.CancelledError):
        return False
    status = http_status_of(exc)
    if status in _PERMANENT_STATUS:
        return False
    if status in _TRANSIENT_STATUS:
        return True
    if isinstance(
        exc,
        (TimeoutError, asyncio.TimeoutError, ConnectionError, ConnectionResetError, BrokenPipeError),
    ):
        return True
    if type(exc).__name__ in _TRANSIENT_NAMES:
        return True
    text = str(exc).lower()
    return any(
        needle in text
        for needle in (
            "timeout",
            "timed out",
            "connection reset",
            "connection aborted",
            "temporarily unavailable",
            "econnreset",
            "network unreachable",
        )
    )


def is_failover_error(exc: BaseException) -> bool:
    if is_rate_limited(exc):
        return True
    if type(exc).__name__ in _FAILOVER_EXC_NAMES:
        return True
    status = http_status_of(exc)
    if status in {400, 401, 403, 404, 408, 409, 422, 429} or (
        status is not None and status >= 500
    ):
        return True
    text = str(exc).lower()
    return (
        isinstance(exc, (TimeoutError, asyncio.TimeoutError))
        or "timeout" in text
        or "timed out" in text
        or "unauthorized" in text
        or "bad request" in text
        or "not found" in text
    )


def step_action_command(raw: str) -> str | None:
    key = re_fold(raw).replace("step", "").replace("all", "")
    if key in {"undo", "undostep"}:
        return "undo"
    if key in {"redo", "redostep"}:
        return "redo"
    if key in {"accept", "acceptall"}:
        return "accept"
    if key in {"revert", "revertall"}:
        return "revert"
    return None


def parse_dashboard_mode(raw: str) -> dict[str, Any]:
    """Map dashboard labels onto turn mode, control suite, hybrid flag, and route tier."""
    key = re_fold(raw)
    if "plan" in key:
        return {"mode": "plan", "control": None, "hybrid": False, "tier": None, "beast": False}
    if "beast" in key:
        return {"mode": "act", "control": "autonomous", "hybrid": False, "tier": "BEST", "beast": True}
    if "hybrid" in key or key in {"multillm", "multi-llm"}:
        return {"mode": "act", "control": None, "hybrid": True, "tier": None, "beast": False, "model": HYBRID_LABEL}
    if "manual" in key:
        return {"mode": None, "control": "manual", "hybrid": False, "tier": None, "beast": False}
    if "autonomous" in key:
        return {"mode": "act", "control": "autonomous", "hybrid": False, "tier": None, "beast": False}
    if key in {"act", "review", "plan"}:
        return {"mode": key, "control": None, "hybrid": False, "tier": None, "beast": False}
    return {"mode": None, "control": None, "hybrid": None, "tier": None, "beast": False}


def re_fold(raw: str) -> str:
    return "".join(ch for ch in (raw or "").lower() if ch.isalnum())


@dataclass
class OrchestrationStage:
    name: str
    engines: list[Engine] = field(default_factory=list)


@dataclass
class OrchestrationPlan:
    hybrid: bool
    stages: list[OrchestrationStage]
    kind: str = "general"


class ModelOrchestrator:
    def __init__(self, manager: CustomModelManager) -> None:
        self.manager = manager

    async def resolve(
        self,
        prompt: str,
        *,
        mode: str,
        hybrid: bool,
        model: str,
        batch: str,
    ) -> OrchestrationPlan:
        kind = self.manager.classify_task(prompt, "plan" if hybrid else mode)
        tier = batch_to_tier(batch)
        combo = await self.manager.engines_for_prompt(prompt, mode=mode, tier=tier)
        if hybrid:
            reasoning = await self.manager.engines_for_prompt(
                "architecture design reason " + prompt, mode="plan", tier=tier
            )
            coding = await self.manager.engines_for_prompt(prompt, mode="act", tier=tier)
            if not reasoning:
                reasoning = combo[:1] or resolve_live_engines(model, batch=batch)[:1]
            if not coding:
                coding = combo or resolve_live_engines(model, batch=batch)
            if not reasoning:
                reasoning = [resolve_engine(model, batch=batch)]
            if not coding:
                coding = reasoning
            return OrchestrationPlan(
                hybrid=True,
                kind=kind,
                stages=[
                    OrchestrationStage("reasoning", reasoning[:2]),
                    OrchestrationStage("coding", coding[:2]),
                ],
            )
        engines = combo or resolve_live_engines(model, batch=batch)
        if not engines:
            engines = [resolve_engine(model, batch=batch)]
        return OrchestrationPlan(
            hybrid=False,
            kind=kind,
            stages=[OrchestrationStage("single", engines)],
        )


def bind_orchestrator(manager: CustomModelManager) -> ModelOrchestrator:
    global _ORCH
    _ORCH = ModelOrchestrator(manager)
    return _ORCH


def get_orchestrator() -> ModelOrchestrator | None:
    return _ORCH
