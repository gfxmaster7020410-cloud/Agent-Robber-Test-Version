"""Frontier tool dispatcher — sandbox handlers gated by ACRM + mutations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robber.conflict_resolver import ConflictResolutionMatrix
from robber.jail import JailError, workspace_root
from robber.mutations import FileMutationManager, get_mutations
from robber.tools.workspace import ToolResult, clip, execute_tool, select_tool

COMPILER_RE = re.compile(
    r"(SyntaxError|IndentationError|Traceback \(most recent call last\)|"
    r"FAILED|error CS\d+|cannot find symbol|panic:)",
    re.IGNORECASE,
)
ANALYZE_INTENT = re.compile(r"\b(analyze code|ast scan|list symbols|code analysis)\b", re.I)
AST_EDIT_INTENT = re.compile(r"\b(replace (function|class|symbol)|rewrite (def|function|class))\b", re.I)
SCRAPE_INTENT = re.compile(r"\b(scrape|fetch url|fetch page|download page)\b", re.I)


@dataclass
class DispatchReport:
    name: str
    output: str
    ok: bool
    conflicts: dict[str, Any] = field(default_factory=dict)
    mutations: list[dict[str, Any]] = field(default_factory=list)

    def as_tool(self) -> ToolResult:
        return ToolResult(self.name, self.output, self.ok)


def looks_like_compiler_error(text: str) -> bool:
    return bool(COMPILER_RE.search(text or ""))


def select_frontier_tool(query: str) -> str:
    if AST_EDIT_INTENT.search(query or ""):
        return "AST Edit"
    if ANALYZE_INTENT.search(query or ""):
        return "AST Analyze"
    if SCRAPE_INTENT.search(query or ""):
        return "Web Scrape"
    return select_tool(query)


class FrontierToolDispatcher:
    """Run one sandboxed tool and verify outputs before they stick."""

    def __init__(self, root: Path | None = None, mutations: FileMutationManager | None = None) -> None:
        self.root = (root or workspace_root()).resolve()
        self.mutations = mutations

    async def dispatch_query(self, query: str, *, name: str | None = None) -> DispatchReport:
        tool = name or select_frontier_tool(query)
        try:
            result = await self._run(tool, query)
        except JailError as exc:
            result = ToolResult(tool, str(exc), False)
        except Exception as exc:
            result = ToolResult(tool, f"{type(exc).__name__}: {exc}", False)
        manager = self.mutations or get_mutations()
        cards = list(manager.turn_cards())
        conflicts = self._verify(tool, result.output, cards)
        ok = result.ok and not looks_like_compiler_error(result.output)
        if not ok and cards:
            try:
                manager.rollback_turn()
                cards = []
            except Exception:
                pass
        return DispatchReport(
            name=result.name,
            output=clip(result.output),
            ok=ok,
            conflicts=conflicts,
            mutations=cards,
        )

    async def _run(self, name: str, query: str) -> ToolResult:
        try:
            from robber.plugins.plugin_loader import get_loader

            loader = get_loader(self.root)
            plugin = loader.get(name) or loader.match_query(query)
            if plugin is not None:
                result = loader.execute(plugin.name, {"query": query})
                return ToolResult(plugin.name, result.output, result.ok)
        except Exception as exc:
            from robber.plugins.base_plugin import PluginError

            if isinstance(exc, PluginError):
                return ToolResult(name, str(exc), False)
        if name == "AST Analyze":
            from robber.tools.analyze import analyze_file

            return ToolResult(name, analyze_file(query, root=self.root), True)
        if name == "AST Edit":
            from robber.tools.ast_edit import replace_symbol

            return ToolResult(name, replace_symbol(query, root=self.root), True)
        if name == "Web Scrape":
            from robber.tools.scrape import fetch_page

            return ToolResult(name, fetch_page(query), True)
        from robber.terminal import TerminalSupervisor

        return await execute_tool(query, TerminalSupervisor(self.root))

    def _verify(self, name: str, output: str, cards: list[dict[str, Any]]) -> dict[str, Any]:
        sources: dict[str, str] = {name: output or ""}
        if cards:
            sources["mutations"] = json.dumps(cards, ensure_ascii=False)
        try:
            from robber.handoff_ledger import HandoffLedger

            pending = HandoffLedger(self.root).dump_for()
            if pending:
                sources["handoff"] = pending[0].body
        except Exception:
            pass
        if len([value for value in sources.values() if value.strip()]) < 2:
            return {"count": 0, "conflicts": [], "kinds": []}
        return ConflictResolutionMatrix().report(sources)
