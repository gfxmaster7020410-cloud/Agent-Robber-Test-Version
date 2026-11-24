"""Cost-aware rate limiter with SQLite token_usage ledger and budget guard.

Tracks per-request token consumption and estimated USD cost. Enforces daily
and monthly spend caps by pivoting all worker tasks to zero-cost local models
when the budget is exhausted.
"""

from __future__ import annotations

import json
import logging
import os
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from robber.config import (
    COST_AUTO_FALLBACK_LOCAL,
    COST_DAILY_LIMIT_USD,
    COST_MONTHLY_LIMIT_USD,
    COST_WARNING_PCT,
    COST_LIMITS_PATH,
    LOCAL_OLLAMA_HOST,
    LOCAL_LMSTUDIO_HOST,
)
from robber.jail import workspace_root
from robber.ssot_db import get_ssot_db

logger = logging.getLogger(__name__)

_MINUTE = 60.0
_HOUR = 3600.0
_DAY = 86400.0
_MONTH = 30 * _DAY


def _load_cost_config() -> dict[str, Any]:
    root = workspace_root()
    for candidate in [
        root / "robber" / "config" / "cost_limits.json",
        root / COST_LIMITS_PATH,
    ]:
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
    return {}


@dataclass
class PricingMatrix:
    """Provider -> per-million-token pricing in USD."""

    matrix: dict[str, dict[str, float]] = field(default_factory=dict)

    def cost_usd(self, provider: str, input_tokens: int, output_tokens: int) -> float:
        rates = self.matrix.get(provider, self.matrix.get("default", {}))
        input_rate = float(rates.get("input", 0.0))
        output_rate = float(rates.get("output", 0.0))
        return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000.0

    @classmethod
    def from_config(cls) -> "PricingMatrix":
        config = _load_cost_config()
        raw = config.get("pricing_per_million_tokens", {})
        matrix: dict[str, dict[str, float]] = {}
        for provider, rates in raw.items():
            if isinstance(rates, dict):
                matrix[provider] = {
                    "input": float(rates.get("input", 0.0)),
                    "output": float(rates.get("output", 0.0)),
                }
        return cls(matrix=matrix)


@dataclass
class BudgetStatus:
    daily_used: float = 0.0
    daily_limit: float = 0.0
    monthly_used: float = 0.0
    monthly_limit: float = 0.0
    warning_pct: int = COST_WARNING_PCT
    auto_fallback: bool = COST_AUTO_FALLBACK_LOCAL

    @property
    def daily_pct(self) -> float:
        return (self.daily_used / self.daily_limit * 100.0) if self.daily_limit > 0 else 0.0

    @property
    def monthly_pct(self) -> float:
        return (self.monthly_used / self.monthly_limit * 100.0) if self.monthly_limit > 0 else 0.0

    @property
    def should_fallback(self) -> bool:
        if not self.auto_fallback:
            return False
        return self.daily_pct >= 100.0 or self.monthly_pct >= 100.0

    @property
    def warning(self) -> bool:
        return self.daily_pct >= self.warning_pct or self.monthly_pct >= self.warning_pct

    def public(self) -> dict[str, Any]:
        return {
            "daily_used": round(self.daily_used, 6),
            "daily_limit": round(self.daily_limit, 2),
            "daily_pct": round(self.daily_pct, 1),
            "monthly_used": round(self.monthly_used, 6),
            "monthly_limit": round(self.monthly_limit, 2),
            "monthly_pct": round(self.monthly_pct, 1),
            "warning": self.warning,
            "should_fallback": self.should_fallback,
            "warning_pct": self.warning_pct,
            "auto_fallback": self.auto_fallback,
        }


class CostAwareRateLimiter:
    """Track token usage in SQLite SSOT and enforce budget caps."""

    def __init__(
        self,
        *,
        pricing: PricingMatrix | None = None,
        ssot_path: Path | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._pricing = pricing or PricingMatrix.from_config()
        self._ssot_path = ssot_path
        self._config = config or _load_cost_config()
        self._daily_limit = float(self._config.get("daily_limit_usd", COST_DAILY_LIMIT_USD))
        self._monthly_limit = float(self._config.get("monthly_limit_usd", COST_MONTHLY_LIMIT_USD))
        self._warning_pct = int(self._config.get("warning_pct", COST_WARNING_PCT))
        self._auto_fallback = bool(self._config.get("auto_fallback_local", COST_AUTO_FALLBACK_LOCAL))
        self._lock = threading.Lock()
        self._last_warning: float = 0.0
        self._callbacks: list[Callable[[str, dict[str, Any]], None]] = []

    @property
    def pricing(self) -> PricingMatrix:
        return self._pricing

    def on_event(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        self._callbacks.append(callback)

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        for cb in self._callbacks:
            try:
                cb(event, data)
            except Exception:
                pass

    def _db(self):
        return get_ssot_db(path=self._ssot_path) if self._ssot_path else get_ssot_db()

    def record_usage(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        worker_role: str = "",
    ) -> dict[str, Any]:
        cost = self._pricing.cost_usd(provider, input_tokens, output_tokens)
        db = self._db()
        row = db.record_token_usage(
            provider=provider,
            model=model,
            worker_role=worker_role,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
        )
        status = self.budget_status()
        if status.warning:
            now = time.time()
            if now - self._last_warning > 300.0:
                self._last_warning = now
                self._emit("budget_warning", status.public())
                logger.warning(
                    "[COST] Budget warning: daily=%.4f/%.2f (%.0f%%) monthly=%.4f/%.2f (%.0f%%)",
                    status.daily_used, status.daily_limit, status.daily_pct,
                    status.monthly_used, status.monthly_limit, status.monthly_pct,
                )
        if status.should_fallback:
            self._emit("budget_exceeded", status.public())
            logger.warning(
                "[COST] Budget exhausted — pivoting ALL tasks to local Ollama/LM Studio"
            )
        return {
            "cost_usd": cost,
            "budget": status.public(),
        }

    def budget_status(self) -> BudgetStatus:
        db = self._db()
        now = time.time()
        day_start = now - _DAY
        month_start = now - _MONTH
        daily = db.sum_token_usage(since=day_start)
        monthly = db.sum_token_usage(since=month_start)
        return BudgetStatus(
            daily_used=float(daily.get("cost_usd", 0.0)),
            daily_limit=self._daily_limit,
            monthly_used=float(monthly.get("cost_usd", 0.0)),
            monthly_limit=self._monthly_limit,
            warning_pct=self._warning_pct,
            auto_fallback=self._auto_fallback,
        )

    def usage_by_provider(
        self,
        *,
        since: float | None = None,
    ) -> dict[str, dict[str, Any]]:
        db = self._db()
        now = time.time()
        day_start = now - _DAY
        effective_since = since if since is not None else day_start
        rows = db.list_token_usage(since=effective_since, limit=10000)
        by_provider: dict[str, dict[str, Any]] = {}
        for row in rows:
            prov = str(row.get("provider") or "unknown")
            if prov not in by_provider:
                by_provider[prov] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                    "requests": 0,
                }
            entry = by_provider[prov]
            entry["input_tokens"] += int(row.get("input_tokens") or 0)
            entry["output_tokens"] += int(row.get("output_tokens") or 0)
            entry["cost_usd"] += float(row.get("estimated_cost_usd") or 0.0)
            entry["requests"] += 1
        for entry in by_provider.values():
            entry["cost_usd"] = round(entry["cost_usd"], 6)
        return by_provider

    def should_use_local(self) -> bool:
        return self.budget_status().should_fallback

    def local_fallback_url(self) -> str:
        return LOCAL_OLLAMA_HOST


def budget_status(ssot_path: Path | None = None) -> dict[str, Any]:
    limiter = CostAwareRateLimiter(ssot_path=ssot_path)
    return limiter.budget_status().public()
