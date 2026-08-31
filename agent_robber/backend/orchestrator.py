"""Agent Robber — Brain Orchestrator Engine (Module 03).

Decomposes high-level prompts into discrete sub-tasks and assigns
each to the most suitable active model via a priority-based scheduler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agent_robber.db.database import Database


# ---------------------------------------------------------------------------
# Decomposition heuristics
# ---------------------------------------------------------------------------

# Pattern blocks — each tuple: (keyword_pattern, subtask_title_template).
# The brain scans the prompt for these keywords and generates matching
# sub-tasks.  If nothing matches, a generic 3-step decomposition is used.

_DECOMPOSITION_RULES: list[tuple[str, str]] = [
    (r"\b(architect|design|structure|schema|plan)\b",       "Architecture & Design Planning"),
    (r"\b(implement|build|code|write|create|develop)\b",    "Core Implementation"),
    (r"\b(test|verify|validate|qa|check)\b",                "Testing & Validation"),
    (r"\b(deploy|release|ship|publish)\b",                  "Deployment & Release"),
    (r"\b(refactor|optimize|clean|improve)\b",              "Refactoring & Optimization"),
    (r"\b(document|readme|docs|comment)\b",                 "Documentation"),
    (r"\b(security|auth|encrypt|audit)\b",                  "Security Review"),
    (r"\b(debug|fix|bug|issue|error)\b",                    "Bug Fix & Debugging"),
    (r"\b(review|feedback|pr|pull.request)\b",              "Code Review"),
    (r"\b(research|investigate|explore|compare)\b",         "Research & Investigation"),
]

_FALLBACK_STEPS = [
    "Analyze requirements and plan approach",
    "Core implementation",
    "Review and validation",
]


# ---------------------------------------------------------------------------
# Model assignment
# ---------------------------------------------------------------------------

# Keyword-to-provider affinity: subtask keywords hint at which provider
# might be a good fit.  This is a soft preference — the scheduler falls
# back to priority ordering when no affinity matches.

_PROVIDER_AFFINITY: dict[str, list[str]] = {
    "architecture":  ["openai", "anthropic"],
    "design":        ["openai", "anthropic"],
    "implement":     ["openai", "deepseek", "groq"],
    "build":         ["openai", "deepseek", "groq"],
    "code":          ["openai", "deepseek", "groq"],
    "test":          ["openai", "groq"],
    "deploy":        ["openai"],
    "refactor":      ["deepseek", "openai"],
    "security":      ["anthropic", "openai"],
    "debug":         ["deepseek", "openai"],
    "document":      ["openai", "groq"],
    "review":        ["anthropic", "openai"],
    "research":      ["openai", "anthropic"],
}


@dataclass
class SubTaskPlan:
    title: str
    order_index: int
    assigned_model_alias: str = ""
    keywords: list[str] = field(default_factory=list)


@dataclass
class DecompositionResult:
    task_id: str
    title: str
    prompt: str
    subtasks: list[SubTaskPlan]


class BrainOrchestrator:
    """Decomposes prompts and assigns sub-tasks to models."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def decompose(self, prompt: str) -> DecompositionResult:
        """Break a prompt into sub-tasks and persist everything to DB."""
        title = self._derive_title(prompt)
        subtask_plans = self._plan_subtasks(prompt)

        # Persist parent task
        task_id = await self.db.create_task(title=title, prompt=prompt)

        # Fetch active models for assignment
        models = await self.db.get_models(active_only=True)
        model_map = {m["alias"]: m for m in models}

        # Assign models and persist subtasks
        for plan in subtask_plans:
            plan.assigned_model_alias = self._pick_model(plan, model_map)
            await self.db.add_subtask(
                task_id=task_id,
                title=plan.title,
                assigned_model_alias=plan.assigned_model_alias,
                order_index=plan.order_index,
            )

        return DecompositionResult(
            task_id=task_id,
            title=title,
            prompt=prompt,
            subtasks=subtask_plans,
        )

    # -- private helpers ----------------------------------------------------

    def _derive_title(self, prompt: str) -> str:
        """Extract a short title from the prompt (first sentence or first 60 chars)."""
        first_line = prompt.strip().split("\n")[0]
        # Strip markdown heading markers
        first_line = re.sub(r"^#+\s*", "", first_line)
        if len(first_line) > 60:
            return first_line[:57] + "..."
        return first_line or "Untitled Task"

    def _plan_subtasks(self, prompt: str) -> list[SubTaskPlan]:
        """Match decomposition rules against the prompt to generate sub-tasks."""
        lower = prompt.lower()
        matched: list[tuple[int, str]] = []

        for pattern, title in _DECOMPOSITION_RULES:
            if re.search(pattern, lower):
                matched.append((len(matched), title))

        if not matched:
            matched = list(enumerate(_FALLBACK_STEPS))

        return [
            SubTaskPlan(title=title, order_index=idx)
            for idx, title in matched
        ]

    def _pick_model(self, plan: SubTaskPlan, model_map: dict[str, Any]) -> str:
        """Pick the best model alias for a subtask based on affinity + priority."""
        if not model_map:
            return ""

        lower_title = plan.title.lower()

        # Find matching provider affinity
        best_provider: str | None = None
        for keyword, providers in _PROVIDER_AFFINITY.items():
            if keyword in lower_title:
                best_provider = providers[0]
                break

        # If affinity found, try to pick a model from that provider
        if best_provider:
            for alias, model in model_map.items():
                if model.get("provider", "").lower() == best_provider:
                    return alias

        # Fallback: pick by priority (lowest number = highest priority)
        sorted_models = sorted(model_map.values(), key=lambda m: m.get("priority", 99))
        return str(sorted_models[0]["alias"]) if sorted_models else ""
