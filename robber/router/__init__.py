"""BYOK model router package.

SessionFailover / assign_engine stay on robber.router for existing callers.
Phase 2 Part 3 adds ModelRouter, UnifiedProviderProxy, APIKeyRotator, and FailoverMatrix.
"""

from __future__ import annotations

from robber.router.session import (
    AGENT_PERSONA,
    BATCH_CLASSES,
    BEHAVIORAL_BOUNDARIES,
    DISPLAY_MODELS,
    HYBRID_LABEL,
    MAX_CONSENSUS,
    MODEL_META,
    MODEL_ROUTES,
    PROVIDER_TYPES,
    STRENGTH_LABELS,
    Engine,
    SessionFailover,
    assign_engine,
    bind_session_router,
    classify_strength,
    clip_tool_context,
    collect_completion,
    display_models,
    get_session_router,
    label_for_id,
    merge_meta,
    register_route,
    resolve_engine,
    resolve_live_engines,
    set_hybrid_from_labels,
    stream_completion,
    system_prompt_for,
    worker_rows,
)
from robber.router.failover import FailoverEvent, FailoverMatrix, FailoverResult, MAX_FALLBACK_DEPTH, ProviderExhaustionError
from robber.router.key_rotator import APIKeyRotator, KeyLease
from robber.router.model_router import ModelRouter, RouteDecision, RouteTarget
from robber.router.provider_proxy import ModelRequest, ModelResponse, ProviderError, UnifiedProviderProxy

__all__ = [
    "AGENT_PERSONA",
    "APIKeyRotator",
    "BATCH_CLASSES",
    "BEHAVIORAL_BOUNDARIES",
    "DISPLAY_MODELS",
    "Engine",
    "FailoverEvent",
    "FailoverMatrix",
    "FailoverResult",
    "HYBRID_LABEL",
    "KeyLease",
    "MAX_CONSENSUS",
    "MAX_FALLBACK_DEPTH",
    "MODEL_META",
    "MODEL_ROUTES",
    "PROVIDER_TYPES",
    "ModelRequest",
    "ModelResponse",
    "ModelRouter",
    "ProviderError",
    "ProviderExhaustionError",
    "RouteDecision",
    "RouteTarget",
    "STRENGTH_LABELS",
    "SessionFailover",
    "UnifiedProviderProxy",
    "assign_engine",
    "bind_session_router",
    "classify_strength",
    "clip_tool_context",
    "collect_completion",
    "display_models",
    "get_session_router",
    "label_for_id",
    "merge_meta",
    "register_route",
    "resolve_engine",
    "resolve_live_engines",
    "set_hybrid_from_labels",
    "stream_completion",
    "system_prompt_for",
    "worker_rows",
]
