"""Policy-based model routing from planner task_type tags."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from robber.context_assembly import merge_payload
from robber.planner import CODE_GEN, FAST_DRAFT, REASONING, task_type_for
from robber.router.session import MODEL_META, MODEL_ROUTES, STRENGTH_LABELS

TASK_TYPE_STRENGTH = {
    REASONING: "reason",
    FAST_DRAFT: "research",
    CODE_GEN: "codegen",
}


@dataclass
class RouteTarget:
    label: str
    provider: str
    model: str
    api_base: str = ""
    context_length: int = 8192
    cost_per_m: float = 0.0
    protocol: str = "NATIVE"
    batch_class: str = "normal"

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RouteDecision:
    task_type: str
    primary: RouteTarget
    fallbacks: list[RouteTarget] = field(default_factory=list)
    prompt: str = ""
    tokens: int = 0
    clipped: bool = False
    cost_score: float = 0.0

    @property
    def chain(self) -> list[RouteTarget]:
        return [self.primary, *self.fallbacks]

    def public(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "primary": self.primary.public(),
            "fallbacks": [item.public() for item in self.fallbacks],
            "tokens": self.tokens,
            "clipped": self.clipped,
            "cost_score": self.cost_score,
        }


class ModelRouter:
    """Pick a primary model and ordered fallbacks from task_type + window policy."""

    def choose(
        self,
        task: dict[str, Any] | None = None,
        *,
        prompt: str = "",
        task_type: str | None = None,
        prefer: str | None = None,
    ) -> RouteDecision:
        payload = merge_payload((task or {}).get("payload_json")) if task else {}
        kind = (task_type or payload.get("task_type") or "").strip().upper()
        if kind not in {REASONING, FAST_DRAFT, CODE_GEN}:
            kind = task_type_for(str(payload.get("tag") or ""), str(payload.get("category") or ""))
        labels = self._labels(kind, prefer)
        targets = [item for item in (self.target(name) for name in labels) if item is not None]
        if not targets:
            hybrid = self.target("Hybrid Multi-LLM")
            if hybrid is not None and (hybrid.model or "").strip():
                targets = [hybrid]
        if not targets:
            packed, tokens, clipped = self.fit_prompt(prompt, 1024)
            return RouteDecision(
                task_type=kind,
                primary=RouteTarget("none", "none", "none"),
                fallbacks=[],
                prompt=packed,
                tokens=tokens,
                clipped=clipped,
                cost_score=0.0,
            )
        packed, tokens, clipped = self.fit_prompt(prompt, targets[0].context_length)
        if kind == FAST_DRAFT:
            targets = sorted(targets, key=lambda item: (item.cost_per_m, -item.context_length))
        primary = targets[0]
        fallbacks = [item for item in targets[1:] if item.label != primary.label][:3]
        packed, tokens, clipped = self.fit_prompt(prompt, primary.context_length)
        return RouteDecision(
            task_type=kind,
            primary=primary,
            fallbacks=fallbacks,
            prompt=packed,
            tokens=tokens,
            clipped=clipped,
            cost_score=float(primary.cost_per_m),
        )

    def target(self, label: str) -> RouteTarget | None:
        name = (label or "").strip()
        routes = MODEL_ROUTES.get(name)
        if not routes:
            return None
        model_id, provider = routes[0]
        meta = MODEL_META.get(name, {})
        ctx = int(meta.get("context_length") or 8192)
        ptype = str(meta.get("provider_type") or "native")
        protocol = "NATIVE"
        if ptype == "custom_base":
            protocol = "OPENAI_COMPATIBLE"
        elif ptype == "local_ollama":
            protocol = "OPENAI_COMPATIBLE"
        elif provider == "anthropic":
            protocol = "ANTHROPIC_COMPATIBLE"
        if ptype == "local_ollama" or provider in {"ollama", "vllm", "lmstudio", "local"}:
            from robber.config import LOCAL_CONTEXT_WINDOW

            ctx = min(ctx, LOCAL_CONTEXT_WINDOW)
        return RouteTarget(
            label=name,
            provider=provider,
            model=model_id,
            api_base=str(meta.get("api_base") or ""),
            context_length=max(1024, ctx),
            cost_per_m=float(meta.get("cost_per_m") or 0.0),
            protocol=protocol,
            batch_class=str(meta.get("batch_class") or "normal"),
        )

    def fit_prompt(self, prompt: str, context_length: int) -> tuple[str, int, bool]:
        from robber.config import WORKER_PROMPT_TOKEN_CAP
        from robber.memory.token_budgeter import TokenBudgeter

        usable = max(256, int(context_length * 0.9) - 256)
        return TokenBudgeter(usable, hard_cap=WORKER_PROMPT_TOKEN_CAP).fit(prompt or "")

    def _labels(self, task_type: str, prefer: str | None) -> list[str]:
        strength = TASK_TYPE_STRENGTH.get(task_type, "research")
        names: list[str] = []
        wanted = (prefer or "").strip()
        if wanted:
            names.append(wanted)
        names.extend(STRENGTH_LABELS.get(strength, ()))
        names.append("GPT-X / Local")
        seen: set[str] = set()
        out: list[str] = []
        for name in names:
            if not name or name in seen or name not in MODEL_ROUTES:
                continue
            seen.add(name)
            out.append(name)
        return out
