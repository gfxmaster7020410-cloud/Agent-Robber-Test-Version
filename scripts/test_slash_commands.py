"""Slash command registry and handler unit tests — never import server.py."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WORKSPACE = Path(tempfile.mkdtemp(prefix="robber-slash-"))
os.environ["ROBBER_WORKSPACE"] = str(WORKSPACE)


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def _ok(message: str) -> None:
    print(f"ok: {message}")


class FakeHost:
    def __init__(self) -> None:
        self.injected: list[str] = []
        self.turn: str | None = None
        self.control: str | None = None
        self.broadcasts = 0

    async def emit(self, role: str, content: str) -> None:
        return None

    async def broadcast(self) -> None:
        self.broadcasts += 1

    async def set_turn_mode(self, mode: str) -> str:
        self.turn = mode
        return mode

    async def set_control_mode(self, mode: str) -> str:
        self.control = mode
        return mode

    async def set_model(self, label: str) -> str:
        return label

    async def set_route(self, mode: str) -> str:
        return mode

    def inject(self, instruction: str) -> None:
        self.injected.append(instruction)

    def list_models(self) -> list[str]:
        return ["claude-3-5-sonnet"]

    def mcp_catalog(self) -> str:
        return "MCP catalog (fake)"

    async def mcp_enable(self, server_id: str) -> None:
        return None

    async def mcp_disable(self, server_id: str) -> None:
        return None

    def pack_digest(self, query: str, window: int) -> str:
        return f"digest:{query}:{window}"

    async def persist_window(self, raw: object) -> int:
        return 2048

    async def run_argv(self, argv: list[str], timeout: float = 30.0) -> tuple[int, str]:
        return 0, "ok"

    async def start_argv(self, argv: list[str]) -> int:
        return 4242


async def main() -> None:
    import robber.commands.handlers  # noqa: F401
    from robber.commands.registry import ExecutionContext, get_registry, parse_slash

    registry = get_registry()
    names = {item.name for item in registry.all()}
    for required in (
        "compact",
        "mode",
        "mcps",
        "fix-dependencies",
        "run",
        "notify",
        "sound-switch",
        "structure",
        "save-session",
        "save-output",
        "read-research",
        "btw",
        "look-deep",
        "model",
        "logic-check",
        "reasoning",
        "design-plan",
        "prototype",
        "save-plan",
        "output-analyze-plan",
        "ask-questions",
        "prepare-next-input",
        "test",
    ):
        if required not in names:
            _fail(f"missing command /{required}")
    _ok(f"registry has {len(names)} commands")

    name, args = parse_slash("/mode autonomous")
    if name != "mode" or args != ["autonomous"]:
        _fail(f"parse_slash: {name} {args}")
    _ok("parse_slash /mode autonomous")

    dep = [item.name for item, _score in registry.suggest("dep")]
    if "fix-dependencies" not in dep:
        _fail(f"suggest dep -> {dep}")
    _ok("fuzzy suggest dep -> fix-dependencies")

    host = FakeHost()
    state: dict[str, Any] = {
        "mode": "act",
        "harness": None,
        "control_mode": "autonomous",
        "model": "claude-3-5-sonnet",
        "engine": "fallback",
        "batch": "normal",
        "context_window": 1000,
        "last_output": "",
    }

    def ctx(argv: list[str]) -> ExecutionContext:
        return ExecutionContext(
            args=argv,
            session_id="current_session",
            workspace=WORKSPACE,
            state=state,
            host=host,
        )

    help_result = await registry.dispatch("help", ctx([]))
    if not help_result.ok or "Slash commands" not in help_result.content:
        _fail("help handler")
    _ok("/help")

    mode_missing = await registry.dispatch("mode", ctx([]))
    if not mode_missing.ok or "Usage" not in mode_missing.content:
        _fail(f"mode missing args: {mode_missing.content}")
    _ok("/mode without args shows usage")

    mode_set = await registry.dispatch("mode", ctx(["autonomous"]))
    if not mode_set.ok or host.control != "autonomous":
        _fail(f"mode autonomous: {mode_set.content} control={host.control}")
    _ok("/mode autonomous")

    btw = await registry.dispatch("btw", ctx(["side", "note"]))
    if not btw.ok or not btw.ephemeral:
        _fail("btw should be ephemeral")
    if not host.injected:
        _fail("btw did not inject")
    _ok("/btw ephemeral")

    unknown = await registry.dispatch("no-such-command", ctx([]))
    if unknown.ok:
        _fail("unknown command should fail")
    _ok("unknown command handled")

    organized = await registry.dispatch("structure", ctx(["organize"]))
    if not organized.ok:
        _fail(organized.content)
    for folder in ("exports", "plans", "research"):
        if not (WORKSPACE / folder).is_dir():
            _fail(f"missing {folder}")
    _ok("/structure organize")

    print("slash command tests passed")


if __name__ == "__main__":
    asyncio.run(main())
