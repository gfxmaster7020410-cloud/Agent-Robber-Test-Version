"""Distributed multi-model brain: decompose, fan out, synthesize, auto-respond."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

from robber.conflict_resolver import ConflictResolutionMatrix
from robber.engine.failover_pool import FailoverPoolRegistry, get_failover_pool
from robber.engine.model_manager import CustomModelManager
from robber.markdown_ansi import ACT_SILENT_DIRECTIVE
from robber.router import Engine, collect_completion

TTFT_SECONDS = 5.0
WORKER_TIMEOUT = 32.0
WORKERS_PER_NODE = 2
FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)[^\n]*\n(.*?)```", re.DOTALL)
WORKFLOW_RE = re.compile(
    r"^\s*(?:\d+\.\s+)?(?:Recall SSOT|Run the selected tool|Draft a surgical plan|"
    r"Answer in the conversation|Choose one|Option\s+\d+|Proposed next step)\b",
    re.I,
)
REVIEW_OK_RE = re.compile(r"^\s*(ok|lgtm|no issues|looks good|approved)\b", re.I)

Collector = Callable[..., Awaitable[tuple[str, str]]]
EmitFn = Callable[..., Awaitable[None]]

NODE_PROMPTS = {
    "planning": (
        "Node A — architecture. Produce a tight implementation brief: files, constraints, "
        "and risk. You have unrestricted local filesystem access. "
        "If the user asked a capability or how-to question, reply with one sentence and "
        "do not invent files or dummy architecture. "
        "Do not emit numbered workflow menus or user-facing option lists. "
        f"{ACT_SILENT_DIRECTIVE}"
    ),
    "coding": (
        "Node B — code generation. Emit unified diffs and/or JSON file cards only. "
        "You may target any local path the user named. No option lists, no alternative plans. "
        f"{ACT_SILENT_DIRECTIVE}"
    ),
    "review": (
        "Node C — validation. Inspect the request for safety, missing tests, and broken diffs. "
        "If the change is sound reply with OK. Otherwise list blocking issues only."
    ),
}

KIND_FOR_NODE = {
    "planning": "reasoning",
    "coding": "coding",
    "review": "general",
}


@dataclass
class BrainNode:
    name: str
    kind: str
    instruction: str


@dataclass
class WorkerResult:
    node: str
    engine: str
    text: str
    error: str = ""
    ttft_ms: int = 0


@dataclass
class BrainAssignment:
    nodes: list[BrainNode]
    engines: dict[str, list[Engine]]
    synthesizer: Engine | None = None

    @property
    def parallel(self) -> bool:
        labels = {engine.label for group in self.engines.values() for engine in group}
        return len(labels) >= 2 or sum(1 for group in self.engines.values() if group) >= 2

    def status_line(self) -> str:
        def label(name: str) -> str:
            group = self.engines.get(name) or []
            return group[0].label if group else "—"

        return (
            "✦ Initiating: Distributed Brain active "
            f"[Reasoning: {label('planning')} | Coding: {label('coding')} | Review: {label('review')}]"
        )


class BrainSynthesizer:
    """Merge specialist outputs into one production payload."""

    def merge(self, results: Iterable[WorkerResult], *, mode: str) -> str:
        rows = [item for item in results if (item.text or "").strip() and not item.error]
        texts = {item.node: item.text for item in rows}
        reconciled = ConflictResolutionMatrix().reconcile(texts)
        rows = [
            WorkerResult(item.node, item.engine, reconciled.get(item.node, item.text), item.error, item.ttft_ms)
            for item in rows
        ]
        by_node = {item.node: item for item in rows}
        fences: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name in ("coding", "planning", "review"):
            item = by_node.get(name)
            if item is None:
                continue
            for language, body in extract_fences(item.text):
                key = hashlib.sha1(normalize_fence(body).encode("utf-8")).hexdigest()
                if key in seen:
                    continue
                seen.add(key)
                fences.append((language, body))
        parts: list[str] = []
        acting = (mode or "act").strip().lower() in {"act", "autonomous"}
        planning = by_node.get("planning")
        if planning and not acting:
            brief = strip_workflow(without_fences(planning.text)).strip()
            if brief:
                parts.append(brief)
        review = by_node.get("review")
        if review and not review_ok(review.text):
            notes = strip_workflow(without_fences(review.text)).strip()
            if notes:
                parts.append(f"### Review\n{notes}")
        for language, body in fences:
            tag = language or "diff"
            parts.append(f"```{tag}\n{body.rstrip()}\n```")
        if parts:
            return "\n\n".join(parts)
        coding = by_node.get("coding")
        if coding:
            return strip_workflow(coding.text).strip()
        if planning:
            return strip_workflow(planning.text).strip()
        return ""


class DistributedAgentBrain:
    def __init__(
        self,
        manager: CustomModelManager | None = None,
        *,
        pool: FailoverPoolRegistry | None = None,
        collector: Collector | None = None,
        ttft_seconds: float = TTFT_SECONDS,
        synthesizer: BrainSynthesizer | None = None,
    ) -> None:
        self.manager = manager
        self.pool = pool if pool is not None else get_failover_pool()
        self.collector = collector or collect_completion
        self.ttft_seconds = ttft_seconds
        self.synthesizer = synthesizer or BrainSynthesizer()

    def decompose(self, prompt: str, *, mode: str) -> list[BrainNode]:
        acting = (mode or "act").strip().lower() not in {"plan", "review"}
        nodes = [
            BrainNode("planning", KIND_FOR_NODE["planning"], NODE_PROMPTS["planning"]),
            BrainNode("coding", KIND_FOR_NODE["coding"], NODE_PROMPTS["coding"]),
            BrainNode("review", KIND_FOR_NODE["review"], NODE_PROMPTS["review"]),
        ]
        if not acting:
            return [nodes[0], nodes[2]]
        kind = ""
        if self.manager is not None:
            kind = self.manager.classify_task(prompt, mode)
        if kind == "reasoning" and len((prompt or "").split()) < 8:
            return [nodes[0], nodes[2]]
        return nodes

    async def assign(
        self,
        prompt: str,
        *,
        mode: str,
        hybrid: bool,
        model: str,
        batch: str,
    ) -> BrainAssignment:
        from robber.orchestrator import get_orchestrator

        nodes = self.decompose(prompt, mode=mode)
        engines: dict[str, list[Engine]] = {node.name: [] for node in nodes}
        if self.manager is not None:
            orch = get_orchestrator()
            if orch is not None:
                plan = await orch.resolve(prompt, mode=mode, hybrid=True, model=model, batch=batch)
                for stage in plan.stages:
                    if stage.name == "reasoning":
                        engines["planning"] = list(stage.engines)[:WORKERS_PER_NODE]
                    elif stage.name == "coding":
                        engines["coding"] = list(stage.engines)[:WORKERS_PER_NODE]
            for node in nodes:
                if engines.get(node.name):
                    continue
                picked = await self.manager.engines_for_prompt(
                    f"{node.kind} {prompt}",
                    mode="plan" if node.name == "planning" else "act",
                    tier=None,
                )
                engines[node.name] = list(picked)
        for node in nodes:
            extra = self.pool.engines_for(node.kind, limit=WORKERS_PER_NODE)
            engines[node.name] = _dedupe_engines(list(engines.get(node.name) or []) + extra)[:WORKERS_PER_NODE]
            if not engines[node.name] and node.kind != "general":
                engines[node.name] = self.pool.engines_for("general", limit=WORKERS_PER_NODE)
        used = {engine.label for group in engines.values() for engine in group}
        synth = None
        for candidate in self.pool.engines_for("general", limit=8):
            if candidate.label not in used:
                synth = candidate
                break
        if synth is None:
            general = engines.get("review") or engines.get("planning") or []
            synth = general[0] if general else None
        if not hybrid:
            # Still distribute when the pool has specialists; otherwise caller falls back.
            pass
        return BrainAssignment(nodes=nodes, engines=engines, synthesizer=synth)

    async def run(
        self,
        prompt: str,
        *,
        mode: str,
        assignment: BrainAssignment,
        tool_context: str | None = None,
        emit: EmitFn | None = None,
        on_ttft: Callable[[int], None] | None = None,
    ) -> str:
        async def worker(node: BrainNode) -> WorkerResult:
            chain = assignment.engines.get(node.name) or []
            used: set[str] = set()
            last_error = "no worker"
            for engine in chain:
                key = engine.model_string or engine.label
                if key in used or engine.label in used:
                    continue
                if self.pool.is_quarantined(key) or self.pool.is_quarantined(engine.label):
                    continue
                used.add(key)
                used.add(engine.label)
                query = f"{node.instruction}\n\nUser request:\n{prompt}"
                try:
                    started = time.monotonic()
                    _reason, text = await self.collector(
                        engine,
                        query,
                        "plan" if node.name == "planning" else mode,
                        tool_context,
                        ttft_seconds=self.ttft_seconds,
                        timeout=WORKER_TIMEOUT,
                    )
                    ttft_ms = max(0, int((time.monotonic() - started) * 1000))
                    if on_ttft and ttft_ms:
                        on_ttft(ttft_ms)
                    if (text or "").strip():
                        return WorkerResult(node.name, engine.label, text.strip(), ttft_ms=ttft_ms)
                    last_error = "empty"
                except Exception as exc:
                    last_error = type(exc).__name__
                    self.pool.quarantine(key)
                    nxt = next((item.label for item in chain if item.label not in used), None)
                    if emit is not None and nxt:
                        from robber.engine.discovery import failover_log

                        await emit("system", failover_log(engine.label, nxt))
                    continue
            return WorkerResult(node.name, "", "", error=last_error)

        gathered = await asyncio.gather(*[worker(node) for node in assignment.nodes], return_exceptions=True)
        results: list[WorkerResult] = []
        for node, item in zip(assignment.nodes, gathered, strict=True):
            if isinstance(item, BaseException):
                results.append(WorkerResult(node.name, "", "", error=type(item).__name__))
                continue
            results.append(item)
        self._handoff_and_track(results)
        merged = self.synthesizer.merge(results, mode=mode)
        produced = [item for item in results if (item.text or "").strip()]
        if not produced and emit is not None:
            err = next((item.error for item in results if item.error), "all workers failed")
            await emit("error", f"Distributed Brain: all specialists failed ({err})")
        if assignment.synthesizer and merged and len(produced) >= 2 and (mode or "").strip().lower() not in {"plan", "review"}:
            refined = await self._refine(assignment.synthesizer, prompt, merged, tool_context)
            if refined:
                return refined
        return merged

    async def _refine(
        self,
        engine: Engine,
        prompt: str,
        merged: str,
        tool_context: str | None,
    ) -> str:
        query = (
            "Merge the specialist drafts into one production response. "
            "Keep unique unified diffs and file cards. Drop duplicate markdown "
            "and numbered workflow menus.\n\n"
            f"User request:\n{prompt}\n\nDraft:\n{merged}"
        )
        try:
            _reason, text = await self.collector(
                engine,
                query,
                "act",
                tool_context,
                ttft_seconds=self.ttft_seconds,
                timeout=min(WORKER_TIMEOUT, 16.0),
            )
        except Exception:
            return ""
        return strip_workflow(text or "").strip()

    def _handoff_and_track(self, results: list[WorkerResult]) -> None:
        try:
            from robber.handoff_ledger import get_handoff_ledger
            from robber.ssot_context import bidirectional_gap_scan
            from robber.state_graph import get_state_graph
        except Exception:
            return
        try:
            ledger = get_handoff_ledger()
            for item in results:
                if not (item.text or "").strip() or item.error:
                    continue
                ledger.post(item.text, source=item.node or "specialist", kind="specialist")
            pending = ledger.pending()
            for block in pending:
                for item in results:
                    if item.node == block.source:
                        continue
                    if block.source.lower() in (item.text or "").lower():
                        ledger.consume(block.id, consumer=item.node)
                        break
            gaps = bidirectional_gap_scan(ledger.export_text())
            if gaps:
                get_state_graph().commit(
                    "\n".join(f"- {item}" for item in gaps),
                    summary="unhandled specialist gaps",
                )
        except Exception:
            return


def extract_fences(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for match in FENCE_RE.finditer(text or ""):
        body = (match.group(2) or "").rstrip("\n")
        if body.strip():
            out.append(((match.group(1) or "diff").strip() or "diff", body))
    return out


def without_fences(text: str) -> str:
    return FENCE_RE.sub("", text or "").strip()


def normalize_fence(body: str) -> str:
    return re.sub(r"\s+", " ", (body or "").strip())


def strip_workflow(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        if WORKFLOW_RE.search(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def review_ok(text: str) -> bool:
    head = (text or "").strip().splitlines()
    if not head:
        return True
    return bool(REVIEW_OK_RE.match(head[0]))


def _dedupe_engines(engines: list[Engine]) -> list[Engine]:
    seen: set[str] = set()
    out: list[Engine] = []
    for engine in engines:
        key = engine.model_string or engine.label
        if key in seen or engine.label in seen:
            continue
        seen.add(key)
        seen.add(engine.label)
        out.append(engine)
    return out


_BRAIN: DistributedAgentBrain | None = None


def bind_distributed_brain(brain: DistributedAgentBrain | None = None) -> DistributedAgentBrain:
    global _BRAIN
    _BRAIN = brain if brain is not None else DistributedAgentBrain()
    return _BRAIN


def get_distributed_brain() -> DistributedAgentBrain | None:
    return _BRAIN
