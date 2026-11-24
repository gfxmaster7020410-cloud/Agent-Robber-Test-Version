"""DAG task decomposition and SSOT persistence for the autonomous planner."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from robber.intent_analyzer import (
    ARCHITECTURAL_AUDIT,
    BUG_FIX,
    INQUIRY,
    RESEARCH_ANALYSIS,
    IntentAnalyzer,
    IntentPayload,
)
from robber.jail import workspace_root
from robber.state_manager import StateManager

_TEST_NAME = re.compile(r"(^|/)tests?/|\btest_", re.I)

REASONING = "REASONING"
FAST_DRAFT = "FAST_DRAFT"
CODE_GEN = "CODE_GEN"

_TASK_TYPE_BY_TAG = {
    "goal": REASONING,
    "inspect": REASONING,
    "answer": FAST_DRAFT,
    "ingest": FAST_DRAFT,
    "synthesize": REASONING,
    "audit": REASONING,
    "report": REASONING,
    "mutate": CODE_GEN,
    "fix": CODE_GEN,
    "test": CODE_GEN,
    "verify": FAST_DRAFT,
    "plugin": FAST_DRAFT,
}

_BUILTIN_TOOLS = {
    "ast_edit",
    "patch_applier",
    "workspace",
    "analyze",
    "mmap",
    "horizon",
    "web_search",
    "ssot",
    "look-deep",
    "graph",
    "conversation",
    "pytest",
}


def _plugin_tools(intent: IntentPayload) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for name in intent.tools:
        key = str(name or "").strip()
        if not key or key in _BUILTIN_TOOLS or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def task_type_for(tag: str, category: str = "") -> str:
    key = (tag or "").strip().lower()
    if key in _TASK_TYPE_BY_TAG:
        return _TASK_TYPE_BY_TAG[key]
    cat = (category or "").upper()
    if cat in {"BUG_FIX", "CODE_MUTATION"}:
        return CODE_GEN
    if cat in {"ARCHITECTURAL_AUDIT", "RESEARCH_ANALYSIS"}:
        return REASONING
    return FAST_DRAFT


@dataclass
class PlannedTask:
    id: str
    title: str
    tag: str
    parent_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    priority: int = 0
    queue_order: int = 0
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    task_type: str = FAST_DRAFT

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionPlan:
    intent: IntentPayload
    root_id: str
    tasks: list[PlannedTask]
    order: list[str]

    def public(self) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "category": self.intent.category,
            "order": list(self.order),
            "tasks": [task.public() for task in self.tasks],
        }


class PlannerError(ValueError):
    """Raised when a task graph cannot be scheduled."""


def parse_depends(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [str(item) for item in data if str(item).strip()]
    return []


def topological_order(tasks: Iterable[PlannedTask]) -> list[str]:
    rows = list(tasks)
    ids = [task.id for task in rows]
    known = set(ids)
    incoming = {task_id: 0 for task_id in ids}
    edges: dict[str, list[str]] = {task_id: [] for task_id in ids}
    for task in rows:
        for dep in task.depends_on:
            if dep not in known:
                continue
            edges[dep].append(task.id)
            incoming[task.id] += 1
    ready = [task_id for task_id in ids if incoming[task_id] == 0]
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for child in edges[node]:
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)
    if len(order) != len(ids):
        raise PlannerError("cyclic task graph")
    return order


class AutonomousPlanner:
    """Turn an IntentPayload into a DAG and write it into task_state / decisions_ledger."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        manager: StateManager | None = None,
        analyzer: IntentAnalyzer | None = None,
    ) -> None:
        self.root = Path(root).resolve() if root else workspace_root()
        self.manager = manager or StateManager(self.root)
        self.analyzer = analyzer or IntentAnalyzer(self.root, manager=self.manager)

    def plan(self, query: str | IntentPayload) -> ExecutionPlan:
        intent = query if isinstance(query, IntentPayload) else self.analyzer.analyze(query)
        tasks = self._decompose(intent)
        for task in tasks:
            task.task_type = task_type_for(task.tag, intent.category)
        order = topological_order(tasks)
        by_id = {task.id: task for task in tasks}
        ordered = [by_id[task_id] for task_id in order]
        with self.manager.db.transaction():
            self.manager.record_decision(
                f"classify {intent.category}",
                rationale=(
                    f"action={intent.action} confidence={intent.confidence:.2f} "
                    f"files={', '.join(intent.files) or '-'}"
                ),
                payload=json.dumps(intent.public(), ensure_ascii=True)[:4000],
            )
            for task in ordered:
                payload = {
                    **task.payload,
                    "tag": task.tag,
                    "task_type": task.task_type,
                    "inputs": task.inputs,
                    "outputs": task.outputs,
                    "depends_on": task.depends_on,
                    "category": intent.category,
                }
                self.manager.put_task(
                    task.title,
                    status="queued",
                    priority=task.priority,
                    queue_order=task.queue_order,
                    parent_id=task.parent_id,
                    payload=payload,
                    depends_on=task.depends_on,
                    task_id=task.id,
                )
            self.manager.record_decision(
                f"plan {intent.category}",
                rationale=(
                    f"query: {intent.query[:240]}\n"
                    f"files: {', '.join(intent.files) or '-'}\n"
                    f"tasks: {len(ordered)}\n"
                    f"order: {' -> '.join(order)}"
                ),
                task_id=ordered[0].id if ordered else None,
                payload=json.dumps(
                    {"edges": {task.id: task.depends_on for task in ordered}},
                    ensure_ascii=True,
                )[:4000],
            )
        return ExecutionPlan(intent=intent, root_id=ordered[0].id, tasks=ordered, order=order)

    def _decompose(self, intent: IntentPayload) -> list[PlannedTask]:
        root_id = _tid()
        inspect_id = _tid()
        files = list(intent.files)
        impl_files = [path for path in files if not _TEST_NAME.search(path)]
        test_files = [path for path in files if _TEST_NAME.search(path)]
        root = PlannedTask(
            id=root_id,
            title=f"Goal: {(intent.query or 'objective')[:80]}",
            tag="goal",
            priority=100,
            queue_order=0,
            inputs=files,
            outputs=[],
            payload={"role": "root"},
        )
        inspect = PlannedTask(
            id=inspect_id,
            title="Inspect SSOT project context",
            tag="inspect",
            parent_id=root_id,
            depends_on=[root_id],
            priority=80,
            queue_order=1,
            inputs=files,
            outputs=[".robber/ssot.db"],
            payload={"tools": ["ssot"]},
        )
        steps = [root, inspect]
        previous = [inspect_id]
        plugin_names = _plugin_tools(intent)
        for index, pname in enumerate(plugin_names):
            plugin_id = _tid()
            steps.append(
                PlannedTask(
                    id=plugin_id,
                    title=f"Run plugin {pname}",
                    tag="plugin",
                    parent_id=root_id,
                    depends_on=list(previous),
                    priority=65,
                    queue_order=2 + index,
                    inputs=files,
                    outputs=["execution_artifacts"],
                    payload={"tools": [pname], "plugin": pname, "contract": "plugin"},
                )
            )
            previous = [plugin_id]
        if intent.category == INQUIRY:
            answer = PlannedTask(
                id=_tid(),
                title="Answer from SSOT without mutating files",
                tag="answer",
                parent_id=root_id,
                depends_on=list(previous),
                priority=40,
                queue_order=2,
                inputs=files,
                outputs=[],
                payload={"tools": intent.tools},
            )
            steps.append(answer)
            return steps
        if intent.category == RESEARCH_ANALYSIS:
            ingest = PlannedTask(
                id=_tid(),
                title="Stream source documents via mmap",
                tag="ingest",
                parent_id=root_id,
                depends_on=list(previous),
                priority=70,
                queue_order=2,
                inputs=files,
                outputs=["memory_store"],
                payload={"tools": ["mmap", "horizon"]},
            )
            synth = PlannedTask(
                id=_tid(),
                title="Synthesize research into SSOT notes",
                tag="synthesize",
                parent_id=root_id,
                depends_on=[ingest.id],
                priority=30,
                queue_order=3,
                inputs=files,
                outputs=["memory_store"],
                payload={"tools": ["horizon"]},
            )
            steps.extend([ingest, synth])
            return steps
        if intent.category == ARCHITECTURAL_AUDIT:
            scan = PlannedTask(
                id=_tid(),
                title="Audit architecture and SSOT indexes",
                tag="audit",
                parent_id=root_id,
                depends_on=list(previous),
                priority=70,
                queue_order=2,
                inputs=files,
                outputs=["decisions_ledger"],
                payload={"tools": intent.tools},
            )
            report = PlannedTask(
                id=_tid(),
                title="Record audit findings",
                tag="report",
                parent_id=root_id,
                depends_on=[scan.id],
                priority=30,
                queue_order=3,
                inputs=files,
                outputs=["decisions_ledger"],
                payload={"tools": ["graph"]},
            )
            steps.extend([scan, report])
            return steps
        mutate_ids: list[str] = []
        targets = impl_files or files or ["workspace"]
        parallel = len(targets) > 1 and not any(word in intent.query.lower() for word in (" then ", " after "))
        for index, path in enumerate(targets):
            task_id = _tid()
            mutate_ids.append(task_id)
            title = "Patch failure site" if intent.category == BUG_FIX else f"Mutate {path}"
            if intent.category == BUG_FIX and index == 0:
                title = f"Fix {path}"
            steps.append(
                PlannedTask(
                    id=task_id,
                    title=title,
                    tag="mutate" if intent.category != BUG_FIX else "fix",
                    parent_id=root_id,
                    depends_on=list(previous),
                    priority=60,
                    queue_order=2 if parallel else 2 + index,
                    inputs=[path] if path != "workspace" else files,
                    outputs=[path] if path != "workspace" else files,
                    payload={"tools": intent.tools, "contract": "write"},
                )
            )
        gate = list(mutate_ids) if mutate_ids else list(previous)
        if test_files or "tests" in intent.tags:
            test_id = _tid()
            outputs = test_files or ["tests/"]
            steps.append(
                PlannedTask(
                    id=test_id,
                    title="Update tests",
                    tag="test",
                    parent_id=root_id,
                    depends_on=list(gate),
                    priority=40,
                    queue_order=3,
                    inputs=impl_files or files,
                    outputs=outputs,
                    payload={"tools": ["pytest"], "contract": "write"},
                )
            )
            gate = [test_id]
        verify = PlannedTask(
            id=_tid(),
            title="Verify success criteria",
            tag="verify",
            parent_id=root_id,
            depends_on=list(gate),
            priority=20,
            queue_order=4,
            inputs=files,
            outputs=[],
            payload={"criteria": intent.success_criteria, "tools": ["pytest"]},
        )
        steps.append(verify)
        return steps


def _tid() -> str:
    return uuid.uuid4().hex[:16]
