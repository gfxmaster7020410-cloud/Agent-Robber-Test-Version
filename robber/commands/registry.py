"""Slash command registry — names, aliases, fuzzy match, dispatch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

CATEGORIES = ("System", "Execution", "Context", "Planning", "Workspace")

Handler = Callable[["ExecutionContext"], Awaitable["CommandResult"]]


class CommandHost(Protocol):
    """Runtime hooks supplied by the daemon. Handlers never import server.py."""

    async def emit(self, role: str, content: str) -> None: ...

    async def broadcast(self) -> None: ...

    async def set_turn_mode(self, mode: str) -> str: ...

    async def set_control_mode(self, mode: str) -> str: ...

    async def set_model(self, label: str) -> str: ...

    async def set_route(self, mode: str) -> str: ...

    def inject(self, instruction: str) -> None: ...

    def list_models(self) -> list[str]: ...

    def mcp_catalog(self) -> str: ...

    async def mcp_enable(self, server_id: str) -> None: ...

    async def mcp_disable(self, server_id: str) -> None: ...

    def pack_digest(self, query: str, window: int) -> str: ...

    async def persist_window(self, raw: object) -> int: ...

    async def undo_step(self) -> str: ...

    async def redo_step(self) -> str: ...

    async def accept_all(self) -> str: ...

    async def revert_all(self) -> str: ...

    async def run_argv(self, argv: list[str], timeout: float = 30.0) -> tuple[int, str]: ...

    async def start_argv(self, argv: list[str]) -> int: ...


@dataclass
class CommandResult:
    ok: bool
    content: str
    ephemeral: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SlashCommand:
    name: str
    description: str
    category: str
    usage: str
    handler: Handler
    aliases: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "description": self.description,
            "category": self.category,
            "usage": self.usage,
        }


@dataclass
class ExecutionContext:
    args: list[str]
    session_id: str
    workspace: Path
    state: dict[str, Any]
    host: CommandHost

    @property
    def argline(self) -> str:
        return " ".join(self.args).strip()


class CommandRegistry:
    def __init__(self) -> None:
        self._items: dict[str, SlashCommand] = {}
        self._aliases: dict[str, str] = {}

    def register(self, command: SlashCommand) -> SlashCommand:
        key = command.name.strip().lower().lstrip("/")
        if not key:
            raise ValueError("command name is empty")
        if key in self._items:
            raise ValueError(f"duplicate command {key}")
        self._items[key] = command
        self._aliases[key] = key
        for alias in command.aliases:
            alias_key = alias.strip().lower().lstrip("/")
            if alias_key:
                self._aliases[alias_key] = key
        return command

    def get(self, name: str) -> SlashCommand | None:
        key = (name or "").strip().lower().lstrip("/")
        resolved = self._aliases.get(key)
        if resolved is None:
            return None
        return self._items.get(resolved)

    def all(self) -> list[SlashCommand]:
        return sorted(self._items.values(), key=lambda item: (item.category, item.name))

    def catalog(self) -> list[dict[str, Any]]:
        return [item.public() for item in self.all()]

    def help_text(self) -> str:
        lines = ["Slash commands (type / in the prompt for autocomplete):"]
        current = ""
        for item in self.all():
            if item.category != current:
                current = item.category
                lines.append(f"  [{current}]")
            alias = f" ({', '.join('/' + a for a in item.aliases)})" if item.aliases else ""
            lines.append(f"  /{item.name:<22} {item.description}{alias}")
        return "\n".join(lines)

    def suggest(self, query: str, limit: int = 8) -> list[tuple[SlashCommand, int]]:
        needle = (query or "").strip().lstrip("/").lower()
        scored: list[tuple[SlashCommand, int]] = []
        for item in self.all():
            score = _fuzzy(needle, item)
            if score > 0 or not needle:
                scored.append((item, score if needle else 1))
        scored.sort(key=lambda pair: (-pair[1], pair[0].name))
        return scored[: max(1, limit)]

    async def dispatch(self, name: str, ctx: ExecutionContext) -> CommandResult:
        command = self.get(name)
        if command is None:
            return CommandResult(False, f"Unknown command: /{name}. Type /help for the list.")
        try:
            return await command.handler(ctx)
        except Exception as exc:
            return CommandResult(False, f"/{command.name} failed: {type(exc).__name__}: {exc}")


_REGISTRY = CommandRegistry()


def get_registry() -> CommandRegistry:
    return _REGISTRY


def parse_slash(raw: str) -> tuple[str, list[str]]:
    text = (raw or "").strip()
    if text.startswith("/"):
        text = text[1:]
    parts = [part for part in text.split() if part]
    if not parts:
        return "", []
    return parts[0].lower().lstrip("/"), parts[1:]


def _fuzzy(needle: str, item: SlashCommand) -> int:
    haystacks = [item.name, *item.aliases, item.description.lower(), item.category.lower()]
    best = 0
    for hay in haystacks:
        hay = hay.lower()
        if hay.startswith(needle):
            best = max(best, 100 - (len(hay) - len(needle)))
        elif needle in hay:
            best = max(best, 70 - hay.find(needle))
        elif _subsequence(needle, hay):
            best = max(best, 40)
    return best


def _subsequence(needle: str, hay: str) -> bool:
    index = 0
    for char in needle:
        index = hay.find(char, index)
        if index < 0:
            return False
        index += 1
    return True


def command(
    *,
    name: str,
    description: str,
    category: str,
    usage: str,
    aliases: Sequence[str] = (),
) -> Callable[[Handler], Handler]:
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}")

    def wrap(fn: Handler) -> Handler:
        get_registry().register(
            SlashCommand(
                name=name.strip().lower().lstrip("/"),
                description=description,
                category=category,
                usage=usage,
                handler=fn,
                aliases=tuple(a.strip().lower().lstrip("/") for a in aliases if a.strip()),
            )
        )
        return fn

    return wrap
