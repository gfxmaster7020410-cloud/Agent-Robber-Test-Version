"""Hard token quotas for worker prompts and LLM provider routes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from robber.config import (
    BUDGET_GOAL_RATIO,
    BUDGET_GRAPH_RATIO,
    BUDGET_HISTORY_RATIO,
    BUDGET_SYSTEM_RATIO,
    WORKER_PROMPT_TOKEN_CAP,
)
from robber.ssot_context import CHARS_PER_TOKEN, MIN_CONTEXT_WINDOW, estimate_tokens

SECTION_ORDER = ("system", "goal", "graph", "history")


@dataclass(frozen=True)
class TokenQuotas:
    window: int
    system: int
    goal: int
    graph: int
    history: int

    @property
    def total(self) -> int:
        return int(self.system + self.goal + self.graph + self.history)

    def public(self) -> dict[str, int]:
        row = asdict(self)
        row["total"] = self.total
        return row


@dataclass
class BudgetPack:
    text: str
    tokens: int
    clipped: bool
    quotas: TokenQuotas
    sections: dict[str, str] = field(default_factory=dict)
    section_tokens: dict[str, int] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens,
            "clipped": self.clipped,
            "quotas": self.quotas.public(),
            "section_tokens": dict(self.section_tokens),
        }


class TokenBudgeter:
    """Split an LLM window into persona / goal / AST graph / history quotas."""

    WORKER_CAP = WORKER_PROMPT_TOKEN_CAP

    def __init__(
        self,
        window: int | None = None,
        *,
        hard_cap: int | None = None,
    ) -> None:
        cap = int(self.WORKER_CAP if hard_cap is None else hard_cap)
        raw = int(self.WORKER_CAP if window is None else window)
        self.window = max(MIN_CONTEXT_WINDOW, raw)
        self.hard_cap = max(MIN_CONTEXT_WINDOW, cap)
        self.usable = min(self.window, self.hard_cap)

    def quotas(self) -> TokenQuotas:
        total = self.usable
        system = max(1, int(total * BUDGET_SYSTEM_RATIO))
        goal = max(1, int(total * BUDGET_GOAL_RATIO))
        graph = max(1, int(total * BUDGET_GRAPH_RATIO))
        history = max(1, total - system - goal - graph)
        return TokenQuotas(
            window=total,
            system=system,
            goal=goal,
            graph=graph,
            history=history,
        )

    def allocate(self, sections: dict[str, str]) -> dict[str, str]:
        quotas = self.quotas()
        out: dict[str, str] = {}
        for name in SECTION_ORDER:
            body = str(sections.get(name) or "")
            limit = int(getattr(quotas, name))
            out[name] = _clip(body, limit) if body else ""
        return out

    def pack(
        self,
        *,
        system: str = "",
        goal: str = "",
        graph: str = "",
        history: str = "",
    ) -> BudgetPack:
        quotas = self.quotas()
        allocated = self.allocate(
            {"system": system, "goal": goal, "graph": graph, "history": history}
        )
        parts = [allocated[name] for name in SECTION_ORDER if (allocated.get(name) or "").strip()]
        joined = "\n\n".join(parts).strip()
        text, tokens, clipped = self.fit(joined)
        raw_over = any(
            estimate_tokens(item) > int(getattr(quotas, name))
            for name, item in (
                ("system", system),
                ("goal", goal),
                ("graph", graph),
                ("history", history),
            )
            if item
        )
        return BudgetPack(
            text=text,
            tokens=tokens,
            clipped=clipped or raw_over,
            quotas=quotas,
            sections=allocated,
            section_tokens={
                name: estimate_tokens(allocated[name])
                for name in SECTION_ORDER
                if allocated.get(name)
            },
        )

    def fit(self, prompt: str) -> tuple[str, int, bool]:
        text = prompt or ""
        tokens = estimate_tokens(text)
        if tokens <= self.usable:
            return text, tokens, False
        clipped = _clip(text, self.usable)
        return clipped, estimate_tokens(clipped), True

    def enforce(self, prompt: str) -> str:
        return self.fit(prompt)[0]


def _clip(text: str, budget: int) -> str:
    blob = text or ""
    limit = max(MIN_CONTEXT_WINDOW, int(budget))
    if estimate_tokens(blob) <= limit:
        return blob
    suffix = "\n... (context clipped at token budget)"
    char_budget = max(4, limit * CHARS_PER_TOKEN - len(suffix))
    clipped = blob[:char_budget].rstrip()
    packed = clipped + suffix
    while estimate_tokens(packed) > limit and len(clipped) > 4:
        clipped = clipped[: max(4, len(clipped) - CHARS_PER_TOKEN)].rstrip()
        packed = clipped + suffix
    return packed
