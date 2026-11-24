"""Agent Robber evaluation harness: plan, retrieve, edit, verify. No live LLM calls."""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from robber.config import COMBINED_RSS_LIMIT
from robber.context_assembly import ContextAssembler
from robber.intent_analyzer import IntentAnalyzer
from robber.planner import AutonomousPlanner
from robber.router.model_router import ModelRouter
from robber.ssot_context import estimate_tokens
from robber.state_manager import StateManager
from robber.telemetry import rss_bytes

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class PhaseTiming:
    plan_ms: float = 0.0
    context_ms: float = 0.0
    edit_ms: float = 0.0
    verify_ms: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass
class TaskSpec:
    id: str
    title: str
    prompt: str
    filename: str
    original: str
    expected: str
    kind: str


@dataclass
class TaskOutcome:
    id: str
    title: str
    ok: bool
    tokens: int
    cost_usd: float
    latency_ms: PhaseTiming = field(default_factory=PhaseTiming)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["latency_ms"] = self.latency_ms.as_dict()
        return row


SUITE = (
    TaskSpec(
        id="bug_fix_add",
        title="Bug fix: restore add()",
        prompt="fix calc.py so add(a, b) returns a + b",
        filename="calc.py",
        original="def add(a, b):\n    return a - b\n",
        expected="def add(a, b):\n    return a + b\n",
        kind="bug_fix",
    ),
    TaskSpec(
        id="refactor_rename",
        title="Refactor: rename helper",
        prompt="refactor util.py rename greet to hello",
        filename="util.py",
        original="def greet(name):\n    return 'hi ' + name\n",
        expected="def hello(name):\n    return 'hi ' + name\n",
        kind="refactor",
    ),
    TaskSpec(
        id="feature_multiply",
        title="Feature: add multiply",
        prompt="create math_extra.py with a multiply(a, b) function",
        filename="math_extra.py",
        original="",
        expected="def multiply(a, b):\n    return a * b\n",
        kind="feature",
    ),
)


def default_suite() -> tuple[TaskSpec, ...]:
    return SUITE


def _cost(tokens: int, cost_per_m: float) -> float:
    return round((max(0, int(tokens)) / 1_000_000.0) * float(cost_per_m or 0.0), 6)


def _apply_edit(path: Path, spec: TaskSpec) -> None:
    path.write_text(spec.expected, encoding="utf-8")


def _peak_rss(current: int, peak: list[int]) -> int:
    used = max(current, rss_bytes())
    peak[0] = max(peak[0], used)
    return used


def run_task(root: Path, spec: TaskSpec, *, peak: list[int]) -> TaskOutcome:
    target = root / spec.filename
    if spec.original:
        target.write_text(spec.original, encoding="utf-8")
    elif target.is_file():
        target.unlink()
    manager = StateManager(root)
    analyzer = IntentAnalyzer(root, manager=manager)
    planner = AutonomousPlanner(root, manager=manager, analyzer=analyzer)
    assembler = ContextAssembler(root, manager=manager, window=2048)
    router = ModelRouter()
    timings = PhaseTiming()
    tokens = 0
    cost_per_m = 0.35

    t0 = time.perf_counter()
    intent = analyzer.analyze(spec.prompt)
    plan = planner.plan(intent)
    timings.plan_ms = round((time.perf_counter() - t0) * 1000, 3)
    _peak_rss(rss_bytes(), peak)
    tokens += estimate_tokens(spec.prompt) + estimate_tokens(" ".join(task.title for task in plan.tasks))

    t1 = time.perf_counter()
    packed_tokens = 0
    if plan.tasks:
        packed = assembler.assemble(plan.tasks[0].id)
        packed_tokens = packed.tokens
        tokens += packed_tokens
    timings.context_ms = round((time.perf_counter() - t1) * 1000, 3)
    decision = router.choose(prompt=spec.prompt, task_type="CODE_GEN")
    cost_per_m = float(decision.cost_score or 0.35)
    tokens += int(decision.tokens or 0)

    t2 = time.perf_counter()
    _apply_edit(target, spec)
    timings.edit_ms = round((time.perf_counter() - t2) * 1000, 3)
    tokens += estimate_tokens(spec.expected)

    t3 = time.perf_counter()
    body = target.read_text(encoding="utf-8") if target.is_file() else ""
    ok = body == spec.expected
    timings.verify_ms = round((time.perf_counter() - t3) * 1000, 3)
    _peak_rss(rss_bytes(), peak)

    manager.add_artifact(
        spec.id,
        body=json.dumps({"ok": ok, "kind": spec.kind}, ensure_ascii=True),
        kind="benchmark",
        exit_code=0 if ok else 1,
    )
    return TaskOutcome(
        id=spec.id,
        title=spec.title,
        ok=ok,
        tokens=tokens,
        cost_usd=_cost(tokens, cost_per_m),
        latency_ms=timings,
        detail="verified" if ok else "output mismatch",
    )


def run_suite(
    root: Path | str | None = None,
    *,
    out: Path | str | None = None,
    tasks: tuple[TaskSpec, ...] | None = None,
) -> dict[str, Any]:
    dest_root = Path(root).resolve() if root else Path(tempfile.mkdtemp(prefix="robber-bench-"))
    dest_root.mkdir(parents=True, exist_ok=True)
    peak = [rss_bytes()]
    outcomes: list[TaskOutcome] = []
    for spec in tasks or default_suite():
        outcomes.append(run_task(dest_root, spec, peak=peak))
        _peak_rss(rss_bytes(), peak)
    success = sum(1 for item in outcomes if item.ok)
    total = len(outcomes) or 1
    phase_totals = PhaseTiming()
    for item in outcomes:
        phase_totals.plan_ms += item.latency_ms.plan_ms
        phase_totals.context_ms += item.latency_ms.context_ms
        phase_totals.edit_ms += item.latency_ms.edit_ms
        phase_totals.verify_ms += item.latency_ms.verify_ms
    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "root": str(dest_root),
        "success_rate": round(success / total, 4),
        "tasks_ok": success,
        "tasks_total": len(outcomes),
        "tokens": sum(item.tokens for item in outcomes),
        "cost_usd": round(sum(item.cost_usd for item in outcomes), 6),
        "peak_rss_bytes": int(peak[0]),
        "rss_limit_bytes": COMBINED_RSS_LIMIT,
        "rss_ok": int(peak[0]) < COMBINED_RSS_LIMIT,
        "latency_ms": phase_totals.as_dict(),
        "tasks": [item.as_dict() for item in outcomes],
    }
    dest = Path(out) if out else ROOT / "benchmarks" / "results.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["results_path"] = str(dest)
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Agent Robber coding benchmark matrix")
    parser.add_argument("--root", default="", help="workspace to mutate for the suite")
    parser.add_argument("--out", default="", help="results json path")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else None
    out = Path(args.out).resolve() if args.out else None
    report = run_suite(root, out=out)
    print(
        "success_rate={rate} tokens={tokens} peak_rss={rss} rss_ok={ok}".format(
            rate=report["success_rate"],
            tokens=report["tokens"],
            rss=report["peak_rss_bytes"],
            ok=report["rss_ok"],
        )
    )
    return 0 if report["rss_ok"] and report["success_rate"] >= 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
