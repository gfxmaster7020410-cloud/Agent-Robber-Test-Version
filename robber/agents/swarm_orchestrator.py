"""SQLite-backed swarm coordinator. No in-memory work queues."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from robber.agents.sub_agents import (
    ROLE_LABELS,
    AuditorWorker,
    ArchitectWorker,
    CodeWorker,
    TestWorker,
    WorkerAgent,
    WorkerResult,
    role_for_task,
)
from robber.agents.vision_auditor import VisionAuditorWorker
from robber.agents.pr_writer import PRWriterWorker
from robber.config import (
    COMBINED_RSS_LIMIT,
    SWARM_MAX_CONCURRENT,
    SWARM_MAX_WORKERS,
    SWARM_TEST_RETRIES,
    WORKER_PROMPT_TOKEN_CAP,
)
from robber.context_assembly import ContextAssembler, merge_payload
from robber.jail import workspace_root
from robber.planner import parse_depends, topological_order
from robber.state_manager import COMPLETED, FAILED, IN_PROGRESS, PENDING, StateManager
from robber.telemetry import rss_bytes

logger = logging.getLogger(__name__)

SWARM_NEEDLE = re.compile(
    r"\b(refactor|implement|patch|fix|mutate|unit tests?|add tests?|async locks?)\b",
    re.IGNORECASE,
)
FILE_NEEDLE = re.compile(
    r"(?:[\w.-]+/)*[\w.-]+\.(?:py|ts|tsx|js|rs|go)\b",
    re.IGNORECASE,
)

ExecuteFn = Callable[[dict[str, Any], Any], Awaitable[Any] | Any]


@dataclass
class SwarmReport:
    hops: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    ok: bool = True
    retries: int = 0

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "hops": list(self.hops),
            "tasks": list(self.task_ids),
            "completed": list(self.completed),
            "failed": list(self.failed),
            "retries": self.retries,
        }


class SwarmOrchestrator:
    """Drive Architect -> Coder -> Auditor -> Tester through task_state."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        manager: StateManager | None = None,
        hub: Any = None,
        assembler: ContextAssembler | None = None,
        execute: ExecuteFn | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
    ) -> None:
        self.root = Path(root).resolve() if root else workspace_root()
        self.manager = manager or StateManager(self.root)
        self.hub = hub
        self.assembler = assembler or ContextAssembler(
            self.root, manager=self.manager, window=WORKER_PROMPT_TOKEN_CAP
        )
        self._execute = execute
        self._sleep = sleep or asyncio.sleep
        self.hops: list[str] = []
        self.workers: dict[str, WorkerAgent] = {
            "architect": ArchitectWorker(self.root, manager=self.manager, hub=hub),
            "coder": CodeWorker(self.root, manager=self.manager, hub=hub),
            "auditor": AuditorWorker(self.root, manager=self.manager, hub=hub),
            "tester": TestWorker(self.root, manager=self.manager, hub=hub),
            "vision_auditor": VisionAuditorWorker(self.root, manager=self.manager, hub=hub),
            "pr_writer": PRWriterWorker(self.root, manager=self.manager, hub=hub),
        }
        self._gate = asyncio.Semaphore(max(1, min(SWARM_MAX_CONCURRENT, SWARM_MAX_WORKERS)))
        self._question_skipped = False

    async def _inspect_scope_before_dispatch(
        self, task: dict[str, Any], role: str
    ) -> str:
        """Check if the coder task needs user clarification before execution.

        Asks when >3 files are targeted or when no specific files are resolved
        (architecturally ambiguous). Returns the user's chosen approach or
        ``"[auto:proceed]"`` if no clarification is needed.
        """
        if role != "coder":
            return "[auto:proceed]"
        payload = merge_payload(task.get("payload_json"))
        files: list[str] = []
        for key in ("files", "inputs", "outputs"):
            items = payload.get(key) or []
            if isinstance(items, list):
                files.extend(str(f) for f in items if f)
        seen: list[str] = []
        for f in files:
            if f not in seen:
                seen.append(f)
        files = seen

        if len(files) <= 3 and files:
            return "[auto:proceed]"

        question_text = (
            f"The coder will mutate {len(files)} files."
            if files
            else "The coder target files could not be resolved (ambiguous scope)."
        )
        options: list[str] = []
        if files:
            for f in files[:5]:
                options.append(f"Include {f}")
            if len(files) > 5:
                options.append(f"... and {len(files) - 5} more")
        options.append("Proceed with all")
        options.append("Skip this mutation")

        try:
            from robber.ui.question_engine import get_question_engine

            engine = get_question_engine()
            answer = await engine.ask(
                question_text,
                options=options,
                allow_custom=True,
                timeout=90.0,
                source_worker=role,
                task_id=str(task.get("id") or ""),
            )
            if answer in ("[timeout]", "[auto:skip]"):
                logger.info("[scope] %s auto-proceed (no answer)", task.get("id", "")[:8])
                return "[auto:proceed]"
            if "skip" in answer.lower():
                logger.info("[scope] %s user skipped mutation", task.get("id", "")[:8])
                self._question_skipped = True
                return "[skip]"
            return answer
        except Exception:
            logger.debug("[scope] question engine unavailable, proceeding")
            return "[auto:proceed]"

    def seed_dag(self, query: str) -> list[dict[str, Any]]:
        """Architect writes a 4-node JSON DAG into task_state."""
        files = _files_for(query, self.root)
        dag = _four_node_dag(query, files)
        architect = self.workers["architect"]
        rows: list[dict[str, Any]] = []
        with self.manager.db.transaction():
            self.manager.record_decision(
                "swarm dag",
                rationale=f"architect packed {len(dag['nodes'])} nodes",
                payload=json.dumps(dag, ensure_ascii=True)[:4000],
                branch="swarm",
            )
            for node in dag["nodes"]:
                payload = {
                    "tag": node["tag"],
                    "worker": node["worker"],
                    "query": query,
                    "files": files,
                    "inputs": files,
                    "depends_on": node["depends_on"],
                    "dag": dag if node["worker"] == "architect" else {},
                    "task_type": node["task_type"],
                }
                row = self.manager.put_task(
                    node["title"],
                    status="queued",
                    priority=node["priority"],
                    queue_order=node["queue_order"],
                    parent_id=node.get("parent_id"),
                    payload=payload,
                    depends_on=node["depends_on"],
                    task_id=node["id"],
                )
                rows.append(row)
            for role, worker in self.workers.items():
                agent = self.manager.put_agent(
                    f"swarm-{role}",
                    language="python",
                    phase="planning" if role == "architect" else "idle",
                    loop_status="seed",
                    telemetry={"role": role, "window": worker.window},
                )
                worker.bind_agent(str(agent.get("id") or "") or None)
        architect.relay("coder", json.dumps(dag, ensure_ascii=True), rows[0]["id"] if rows else None)
        return rows

    async def execute_task_dag(self, query: str) -> SwarmReport:
        if rss_bytes() >= COMBINED_RSS_LIMIT:
            logger.error("swarm skipped: rss cap")
            report = SwarmReport(ok=False)
            report.failed.append("rss")
            return report
        self.hops = []
        rows = self.seed_dag(query)
        report = SwarmReport(task_ids=[str(row["id"]) for row in rows])
        by_id = {str(row["id"]): row for row in rows}
        order = topological_order(
            [
                _PlanShim(
                    str(row["id"]),
                    parse_depends(merge_payload(row.get("payload_json")).get("depends_on") or row.get("depends_on")),
                )
                for row in rows
            ]
        )
        for task_id in order:
            task = self.manager.get_task(task_id) or by_id.get(task_id)
            if task is None:
                continue
            role = role_for_task(task)
            result = await self.dispatch(task, close=role != "tester")
            if result.pause:
                report.ok = False
                break
            if role == "tester":
                if not result.ok:
                    from robber.diagnostics.test_parser import TracebackParser

                    probe = TracebackParser().parse(result.output or "", root=self.root)
                    if probe.path and probe.line:
                        result = await self._repair_loop(task, result, report, probe)
                dest = COMPLETED if result.ok else FAILED
                payload = merge_payload(task.get("payload_json"))
                payload["result"] = (result.output or "")[:4000]
                payload["retries"] = report.retries
                payload["worker"] = "tester"
                try:
                    self.manager.transition_task(task_id, dest, payload=payload)
                except Exception:
                    logger.debug("tester close skipped")
            if result.ok:
                report.completed.append(task_id)
                await self._compress_completed(task_id, result)
            else:
                report.failed.append(task_id)
                report.ok = False
            try:
                from robber.memory.ram_guard import get_ram_guard

                get_ram_guard(self.root).maybe_reclaim()
            except Exception:
                logger.debug("swarm ram guard skipped")
        report.hops = list(self.hops)
        return report

    async def dispatch(
        self,
        task: dict[str, Any],
        context: Any = None,
        fallback: ExecuteFn | None = None,
        *,
        close: bool = True,
    ) -> WorkerResult:
        role = role_for_task(task)
        label = ROLE_LABELS.get(role, "CODER")
        worker = self.workers.get(role) or self.workers["coder"]
        task_id = str(task.get("id") or "")
        async with self._gate:
            scope_answer = await self._inspect_scope_before_dispatch(task, role)
            if scope_answer == "[skip]":
                return WorkerResult(ok=True, output="skipped by user clarification", pause=True)
            await worker.emit("active")
            self.hops.append(label)
            worker.touch("running", task_id or None, label.lower())
            status = str(task.get("status") or PENDING)
            if status == PENDING and task_id:
                try:
                    task = self.manager.transition_task(task_id, IN_PROGRESS)
                except Exception:
                    pass
            packed = context
            if packed is None:
                try:
                    packed = self.assembler.assemble(task)
                except Exception:
                    packed = None
            if role == "coder":
                self._checkpoint_high_risk(task)
            execute = fallback if fallback is not None else self._execute
            if execute is not None:
                result = execute(task, packed)
                if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                    result = await result
                worker_result = _as_worker_result(result)
            else:
                worker_result = await worker.run(task, packed)
            payload = merge_payload(task.get("payload_json"))
            payload["result"] = (worker_result.output or "")[:4000]
            payload["worker"] = role
            payload["hop"] = label
            if close and task_id and not worker_result.pause:
                dest = COMPLETED if worker_result.ok else FAILED
                try:
                    self.manager.transition_task(task_id, dest, payload=payload)
                except Exception:
                    logger.debug("swarm task close skipped")
            worker.touch("completed" if worker_result.ok else "idle", task_id or None, label.lower())
            return worker_result

    async def _compress_completed(self, task_id: str, result: WorkerResult) -> None:
        try:
            from robber.memory.context_compressor import ContextCompressor

            compressor = ContextCompressor(self.root, manager=self.manager, hub=self.hub)
            saved = compressor.summarize_task(task_id, force=True)
        except Exception:
            logger.debug("swarm compress skipped")
            return
        blob = compressor.summary_for(task_id) if saved else ""
        text = f"[COMPRESS] {task_id[:8]} {blob[:180]}"
        if self.hub is None:
            return
        try:
            await self.hub.broadcast(
                {
                    "type": "message",
                    "event": "STATE_ACK",
                    "role": "system",
                    "content": text,
                    "text": text,
                    "stream_kind": "ack",
                    "append": False,
                }
            )
        except Exception:
            logger.debug("compress emit skipped")

    def _checkpoint_high_risk(self, task: dict[str, Any]) -> None:
        try:
            from robber.engine.recovery import SnapshotRecoveryEngine

            payload = merge_payload(task.get("payload_json"))
            files: list[str] = []
            for key in ("outputs", "inputs", "files"):
                items = payload.get(key) or []
                if isinstance(items, list):
                    files.extend(str(item) for item in items if item)
            SnapshotRecoveryEngine(self.root, manager=self.manager, hub=self.hub).snapshot(
                label="pre-coder",
                files=files[:12],
                reason="high-risk",
            )
        except Exception:
            logger.debug("swarm snapshot skipped")

    async def _repair_loop(
        self,
        tester_task: dict[str, Any],
        tester_result: WorkerResult,
        report: SwarmReport,
        failure: Any | None = None,
    ) -> WorkerResult:
        from robber.diagnostics.test_parser import TracebackParser

        parser = TracebackParser()
        failure = failure or parser.parse(tester_result.output or "", root=self.root)
        self._store_run_artifact(tester_task, "test", tester_result, failure, attempt=0)
        result = tester_result
        retries = max(1, int(SWARM_TEST_RETRIES))
        for retry in range(1, retries + 1):
            if rss_bytes() >= COMBINED_RSS_LIMIT:
                logger.error("swarm repair skipped: rss cap")
                break
            report.retries = retry
            extra = {
                "test_failure": failure.as_dict(),
                "retry_count": retry,
                "stack": failure.frames,
            }
            auditor_task = _task_with_role(tester_task, "auditor", extra)
            audit_result = await self.dispatch(auditor_task, close=False)
            coder_extra = dict(extra)
            coder_extra["audit"] = (audit_result.output or "")[:800]
            coder_task = _task_with_role(tester_task, "coder", coder_extra)
            code_result = await self.dispatch(coder_task, close=False)
            self._store_run_artifact(tester_task, "repair", code_result, failure, attempt=retry)
            result = await self.dispatch(tester_task, close=False)
            failure = parser.parse(result.output or "", root=self.root)
            self._store_run_artifact(tester_task, "test", result, failure, attempt=retry)
            if result.ok:
                return result
        return result

    def _store_run_artifact(
        self,
        task: dict[str, Any],
        kind: str,
        result: WorkerResult,
        failure: Any,
        *,
        attempt: int,
    ) -> None:
        task_id = str(task.get("id") or "") or None
        body = json.dumps(
            {
                "attempt": attempt,
                "ok": bool(result.ok),
                "failure": failure.as_dict() if hasattr(failure, "as_dict") else failure,
                "output": (result.output or "")[:2000],
            },
            ensure_ascii=True,
        )[:4000]
        try:
            self.manager.add_artifact(
                f"{kind}:{attempt}",
                body=body,
                kind=kind,
                task_id=task_id,
                exit_code=0 if result.ok else 1,
            )
        except Exception:
            logger.debug("swarm artifact skipped")


def wants_swarm(query: str) -> bool:
    text = (query or "").strip()
    if not text:
        return False
    try:
        from robber.intent import ACTION, classify_intent

        if classify_intent(text) != ACTION:
            return False
    except Exception:
        pass
    return bool(SWARM_NEEDLE.search(text) or FILE_NEEDLE.search(text))


@dataclass
class _PlanShim:
    id: str
    depends_on: list[str]


def _files_for(query: str, root: Path) -> list[str]:
    found = [item.group(0).replace("\\", "/") for item in FILE_NEEDLE.finditer(query or "")]
    out: list[str] = []
    for item in found:
        if item not in out:
            out.append(item)
    if out:
        return out[:8]
    try:
        from robber.intent_analyzer import IntentAnalyzer

        files = list(IntentAnalyzer(root).analyze(query).files)
        return files[:8]
    except Exception:
        return []


def _four_node_dag(query: str, files: list[str]) -> dict[str, Any]:
    architect_id = uuid.uuid4().hex[:16]
    coder_id = uuid.uuid4().hex[:16]
    auditor_id = uuid.uuid4().hex[:16]
    tester_id = uuid.uuid4().hex[:16]
    title = (query or "objective").strip()[:72]
    nodes = [
        {
            "id": architect_id,
            "title": f"[ARCHITECT] Analyze {title}",
            "tag": "inspect",
            "worker": "architect",
            "depends_on": [],
            "parent_id": None,
            "priority": 90,
            "queue_order": 0,
            "task_type": "REASONING",
        },
        {
            "id": coder_id,
            "title": f"[CODER] Mutate {', '.join(files[:3]) or 'workspace'}",
            "tag": "mutate",
            "worker": "coder",
            "depends_on": [architect_id],
            "parent_id": architect_id,
            "priority": 70,
            "queue_order": 1,
            "task_type": "CODE_GEN",
        },
        {
            "id": auditor_id,
            "title": "[AUDITOR] Inspect diffs",
            "tag": "audit",
            "worker": "auditor",
            "depends_on": [coder_id],
            "parent_id": architect_id,
            "priority": 50,
            "queue_order": 2,
            "task_type": "REASONING",
        },
        {
            "id": tester_id,
            "title": "[TESTER] Sandbox tests",
            "tag": "verify",
            "worker": "tester",
            "depends_on": [auditor_id],
            "parent_id": architect_id,
            "priority": 30,
            "queue_order": 3,
            "task_type": "FAST_DRAFT",
        },
    ]
    return {"nodes": nodes, "order": [architect_id, coder_id, auditor_id, tester_id], "files": files}


def _task_with_role(task: dict[str, Any], worker: str, extra: dict[str, Any]) -> dict[str, Any]:
    payload = merge_payload(task.get("payload_json"))
    payload.update(extra)
    payload["worker"] = worker
    payload["tag"] = {"auditor": "audit", "coder": "fix", "tester": "verify"}.get(worker, payload.get("tag"))
    clone = dict(task)
    clone["payload_json"] = payload
    return clone


def _as_worker_result(value: Any) -> WorkerResult:
    if isinstance(value, WorkerResult):
        return value
    pause = bool(getattr(value, "pause", False))
    ok = bool(getattr(value, "ok", True))
    output = str(getattr(value, "output", value) or "")
    tokens = int(getattr(value, "tokens", 0) or 0)
    return WorkerResult(ok=ok, output=output, tokens=tokens, pause=pause)
