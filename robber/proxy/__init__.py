"""Multi-model proxy routing and cost-aware rate limiting.

Phase 2 Part 4 adds PersonaProxyRouter and CostAwareRateLimiter on top of
the existing ModelRouter / FailoverMatrix / UnifiedProviderProxy stack.
"""

from __future__ import annotations

from robber.proxy.rate_limiter import CostAwareRateLimiter, PricingMatrix, budget_status
from robber.proxy.router import PersonaProxyRouter, PersonaRoute

__all__ = [
    "CostAwareRateLimiter",
    "PersonaProxyRouter",
    "PersonaRoute",
    "PricingMatrix",
    "budget_status",
]
