"""Self-healing loop: intercept failures, generate a jailed patch, retry.

Implementation lives in robber.diagnostics.self_healing. This module stays
import-compatible with server.py and the agent loop.
"""

from __future__ import annotations

from robber.diagnostics.error_parser import HEALABLE_SUFFIXES, TRACE_FILE
from robber.diagnostics.self_healing import (
    MAX_ATTEMPTS,
    SelfHealingEngine,
    bind_healer,
    get_healer,
)

__all__ = [
    "HEALABLE_SUFFIXES",
    "MAX_ATTEMPTS",
    "TRACE_FILE",
    "SelfHealingEngine",
    "bind_healer",
    "get_healer",
]
