"""Interactive slash-command loop for AMACE operations (no model turn).

Run:
    python -m robber.cli
    python -m robber.cli /audit
    python -m robber.cli /handoff coding
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from robber.commands.registry import ExecutionContext, get_registry, parse_slash
from robber.jail import workspace_root


def _safe_print(text: str) -> None:
    stream = sys.stdout
    payload = text if text.endswith("\n") else f"{text}\n"
    try:
        stream.write(payload)
        stream.flush()
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        buffer = getattr(stream, "buffer", None)
        data = payload.encode(encoding, errors="replace")
        if buffer is not None:
            buffer.write(data)
            buffer.flush()
        else:
            stream.write(data.decode(encoding, errors="replace"))


class LocalCommandHost:
    """Minimal host so AMACE slash commands run without the daemon."""

    def __init__(self) -> None:
        self.injected: list[str] = []

    async def emit(self, role: str, content: str) -> None:
        _safe_print(content)

    async def broadcast(self) -> None:
        return None

    async def set_turn_mode(self, mode: str) -> str:
        return mode

    async def set_control_mode(self, mode: str) -> str:
        return mode

    async def set_model(self, label: str) -> str:
        return label

    async def set_route(self, mode: str) -> str:
        return mode

    def inject(self, instruction: str) -> None:
        self.injected.append(instruction)

    def list_models(self) -> list[str]:
        return []

    def mcp_catalog(self) -> str:
        return ""

    async def mcp_enable(self, server_id: str) -> None:
        return None

    async def mcp_disable(self, server_id: str) -> None:
        return None

    def pack_digest(self, query: str, window: int) -> str:
        return ""

    async def persist_window(self, raw: object) -> int:
        from robber.ssot_context import parse_window

        return parse_window(raw)

    async def undo_step(self) -> str:
        return "Nothing to undo"

    async def redo_step(self) -> str:
        return "Nothing to redo"

    async def accept_all(self) -> str:
        return "Nothing pending to accept"

    async def revert_all(self) -> str:
        return "Nothing to revert"

    async def run_argv(self, argv: list[str], timeout: float = 30.0) -> tuple[int, str]:
        return 0, ""

    async def start_argv(self, argv: list[str]) -> int:
        return 0


async def dispatch_line(
    raw: str,
    *,
    workspace: Path,
    host: LocalCommandHost | None = None,
    state: dict[str, Any] | None = None,
) -> str:
    import robber.commands.handlers  # noqa: F401

    name, args = parse_slash(raw)
    if not name:
        return "Type /help, or /audit /heal /sync /handoff /graph /loop /horizon /resolve."
    ctx = ExecutionContext(
        args=args,
        session_id="cli",
        workspace=workspace,
        state=state or {},
        host=host or LocalCommandHost(),
    )
    result = await get_registry().dispatch(name, ctx)
    return result.content


async def repl(workspace: Path) -> int:
    host = LocalCommandHost()
    print(f"Agent Robber CLI  workspace={workspace}")
    print("Slash commands: /audit /heal /sync /handoff /graph /loop /horizon /resolve /help   (Ctrl+C to exit)")
    while True:
        try:
            line = input("robber> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.lower() in {"/exit", "/quit", "exit", "quit"}:
            return 0
        text = await dispatch_line(line, workspace=workspace, host=host)
        _safe_print(text)
    return 0


async def _run_offline_cycle(query: str, workspace: Path) -> str:
    from robber.agent_loop import AgentLoopManager, format_telemetry
    from robber.mutations import FileMutationManager, bind_mutations
    from robber.tools.dispatcher import FrontierToolDispatcher

    bind_mutations(FileMutationManager(workspace))
    dispatcher = FrontierToolDispatcher(workspace)
    loop = AgentLoopManager(
        hub=None,
        state={
            "amace_cycle": True,
            "workspace": str(workspace),
            "model": "GPT-X / Local",
            "mode": "act",
        },
    )

    async def turn(prompt: str) -> None:
        report = await dispatcher.dispatch_query(prompt)
        loop.last_tool = report.as_tool()
        if not report.ok:
            raise RuntimeError(report.output[:500] or report.name)

    async def dispatch(_sender: Any, _raw: str) -> None:
        return None

    loop.bind_runtime(run_turn=turn, dispatch=dispatch)
    await loop.execute_prompt(query)
    return format_telemetry(workspace, loop)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="robber.cli", description="Agent Robber AMACE slash CLI")
    parser.add_argument("command", nargs="*", help="Optional one-shot slash command, e.g. /audit")
    parser.add_argument(
        "--workspace",
        default="",
        help="Workspace root (default: ROBBER_WORKSPACE or cwd)",
    )
    parser.add_argument("--telemetry", action="store_true", help="Print loop telemetry and exit")
    parser.add_argument(
        "--cycle",
        default="",
        metavar="PROMPT",
        help="Run one offline perception-action cycle (tools + AMACE, no daemon)",
    )
    ns = parser.parse_args(argv)
    workspace = Path(ns.workspace).expanduser() if ns.workspace.strip() else workspace_root()
    if ns.telemetry:
        from robber.agent_loop import format_telemetry, get_loop

        _safe_print(format_telemetry(workspace, get_loop()))
        if not ns.cycle and not ns.command:
            return 0
    if ns.cycle:
        text = asyncio.run(_run_offline_cycle(ns.cycle, workspace))
        _safe_print(text)
        return 0
    if ns.command:
        raw = " ".join(ns.command)
        if not raw.startswith("/"):
            raw = "/" + raw.lstrip("/")
        text = asyncio.run(dispatch_line(raw, workspace=workspace))
        _safe_print(text)
        return 0
    return asyncio.run(repl(workspace))


if __name__ == "__main__":
    sys.exit(main())
