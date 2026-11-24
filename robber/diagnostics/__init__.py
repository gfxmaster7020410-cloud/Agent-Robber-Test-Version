"""Diagnostics package: error parse + self-healing loop."""

from robber.diagnostics.error_parser import ParsedError, parse_error_log
from robber.diagnostics.lsp_security import AstSecurityAuditor, LspClient
from robber.diagnostics.profiler import ExecutionProfiler
from robber.diagnostics.self_healing import SelfHealingEngine, bind_healer, get_healer
from robber.diagnostics.test_parser import TestFailure, TracebackParser

__all__ = [
    "AstSecurityAuditor",
    "ExecutionProfiler",
    "LspClient",
    "ParsedError",
    "SelfHealingEngine",
    "TestFailure",
    "TracebackParser",
    "bind_healer",
    "get_healer",
    "parse_error_log",
]
