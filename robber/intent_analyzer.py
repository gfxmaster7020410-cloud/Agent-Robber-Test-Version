"""User intent and scope classification for the Python brain core.

Builds on robber.intent (inquiry vs action) without replacing it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from robber.intent import ACTION, FILE_HINT, classify_intent
from robber.jail import workspace_root
from robber.state_manager import StateManager

CODE_MUTATION = "CODE_MUTATION"
RESEARCH_ANALYSIS = "RESEARCH_ANALYSIS"
BUG_FIX = "BUG_FIX"
ARCHITECTURAL_AUDIT = "ARCHITECTURAL_AUDIT"
INQUIRY = "INQUIRY"

CATEGORIES = (CODE_MUTATION, RESEARCH_ANALYSIS, BUG_FIX, ARCHITECTURAL_AUDIT, INQUIRY)

_TOOLS = {
    CODE_MUTATION: ("ast_edit", "patch_applier", "workspace"),
    BUG_FIX: ("analyze", "ast_edit", "patch_applier"),
    RESEARCH_ANALYSIS: ("mmap", "horizon", "web_search"),
    ARCHITECTURAL_AUDIT: ("ssot", "look-deep", "graph"),
    INQUIRY: ("conversation",),
}

_BUG = re.compile(r"\b(fix|bug|error|crash|traceback|regression|broken)\b", re.I)
_RESEARCH = re.compile(
    r"\b(research|paper|horizon|micro-batch|100\+?\s*pages?|200\+?\s*pages?|literature)\b",
    re.I,
)
_AUDIT = re.compile(r"\b(audit|architecture|ssot|inspect codebase|map the repo)\b", re.I)
_MUTATE = re.compile(
    r"\b(refactor|implement|rewrite|create|edit|update|scaffold|migrate|rename)\b",
    re.I,
)
_TEST = re.compile(r"\b(tests?|pytest|coverage|unit test)\b", re.I)
_CONSTRAINT = re.compile(
    r"\b((?:do not|don't|without|keep|preserve|must not|never)[^.!?\n]{0,80})",
    re.I,
)
_SUCCESS = re.compile(
    r"\b((?:pass|green|succeed|done when|success(?: criterion| criteria)?)[^.!?\n]{0,80})",
    re.I,
)

_MODULE_HINTS = (
    (re.compile(r"\bstate graph\b", re.I), ("robber/state_graph.py", "tests/test_state_graph.py")),
    (re.compile(r"\bplanner\b", re.I), ("robber/planner.py", "tests/test_planner.py")),
    (re.compile(r"\bssot\b", re.I), ("robber/ssot_db.py", "robber/ssot.py")),
    (re.compile(r"\bhandoff\b", re.I), ("robber/handoff_ledger.py", "tests/test_handoff.py")),
)


@dataclass
class IntentPayload:
    query: str
    category: str
    action: str
    files: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.5
    ssot: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        row = asdict(self)
        row["query"] = (self.query or "")[:400]
        return row


class IntentAnalyzer:
    """Classify a prompt and pull a light SSOT snapshot for the planner."""

    def __init__(self, root: Path | str | None = None, *, manager: StateManager | None = None) -> None:
        self.root = Path(root).resolve() if root else workspace_root()
        self.manager = manager or StateManager(self.root)

    def analyze(self, query: str) -> IntentPayload:
        text = (query or "").strip()
        action = classify_intent(text)
        category = self._category(text, action)
        files = self._files(text)
        tags = [category.lower()]
        if _TEST.search(text):
            tags.append("tests")
        criteria = self._criteria(text, files, category)
        constraints = [item.strip() for item in _CONSTRAINT.findall(text) if item.strip()][:8]
        tools = list(_TOOLS.get(category, ("conversation",)))
        if "tests" in tags and "pytest" not in tools:
            tools.append("pytest")
        try:
            from robber.plugins.plugin_loader import get_loader

            for plugin in get_loader(self.root).enabled():
                if plugin.matches(text, category) and plugin.name not in tools:
                    tools.append(plugin.name)
        except Exception:
            pass
        confidence = 0.45
        if files:
            confidence += 0.2
        if category != INQUIRY:
            confidence += 0.15
        if criteria:
            confidence += 0.1
        return IntentPayload(
            query=text,
            category=category,
            action=action,
            files=files,
            tools=tools,
            constraints=constraints,
            success_criteria=criteria,
            tags=tags,
            confidence=min(0.95, confidence),
            ssot=self._snapshot(),
        )

    def _category(self, text: str, action: str) -> str:
        if action != ACTION and not _MUTATE.search(text):
            if _RESEARCH.search(text):
                return RESEARCH_ANALYSIS
            if _AUDIT.search(text):
                return ARCHITECTURAL_AUDIT
            return INQUIRY
        scores = {
            BUG_FIX: 2 if _BUG.search(text) else 0,
            RESEARCH_ANALYSIS: 2 if _RESEARCH.search(text) else 0,
            ARCHITECTURAL_AUDIT: 2 if _AUDIT.search(text) else 0,
            CODE_MUTATION: 1 if _MUTATE.search(text) else 0,
        }
        if FILE_HINT.search(text):
            scores[CODE_MUTATION] += 1
        best = max(scores, key=lambda key: scores[key])
        if scores[best] == 0:
            return CODE_MUTATION if action == ACTION else INQUIRY
        return best

    def _files(self, text: str) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for match in FILE_HINT.findall(text or ""):
            path = match.replace("\\", "/")
            if path not in seen:
                seen.add(path)
                found.append(path)
        for pattern, paths in _MODULE_HINTS:
            if pattern.search(text or ""):
                for path in paths:
                    if path not in seen:
                        seen.add(path)
                        found.append(path)
        return found[:16]

    def _criteria(self, text: str, files: list[str], category: str) -> list[str]:
        items = [item.strip() for item in _SUCCESS.findall(text) if item.strip()][:6]
        if _TEST.search(text) and not any("test" in item.lower() for item in items):
            items.append("tests updated and passing")
        if files and not items:
            items.append("targeted files compiled or saved")
        if category == RESEARCH_ANALYSIS and not items:
            items.append("research synthesis stored in SSOT")
        return items

    def _snapshot(self) -> dict[str, Any]:
        queued = self.manager.list_tasks(status="queued", limit=8)
        return {
            "project_id": self.manager.project_id,
            "root": str(self.manager.root),
            "queued_tasks": len(queued),
        }
