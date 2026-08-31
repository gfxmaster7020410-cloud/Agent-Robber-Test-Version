"""Slash command registry — names, aliases, fuzzy match, dispatch."""

from .registry import (
    CATEGORIES,
    CommandResult,
    ExecutionContext,
    SlashCommand,
    command,
    get_registry,
    parse_slash,
)

__all__ = [
    "CATEGORIES",
    "CommandResult",
    "ExecutionContext",
    "SlashCommand",
    "command",
    "get_registry",
    "parse_slash",
]
