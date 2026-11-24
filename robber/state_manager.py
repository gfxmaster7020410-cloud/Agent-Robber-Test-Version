"""SSOT data access layer: task, agent, and artifact CRUD with atomic transitions.

No ORM. Every write goes through SsotDB WAL transactions with rollback on error.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from robber.jail import workspace_root
from robber.ssot_db import SsotDB, get_ssot_db

AGENT_PHASES = ("idle", "planning", "running", "paused", "completed")
TASK_STATUSES = ("queued", "running", "paused", "completed", "failed")

AGENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "idle": frozenset({"planning"}),
    "planning": frozenset({"running", "idle"}),
    "running": frozenset({"paused", "completed"}),
    "paused": frozenset({"running", "completed", "idle"}),
    "completed": frozenset({"idle"}),
}

TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "paused"}),
    "running": frozenset({"paused", "completed", "failed"}),
    "paused": frozenset({"running", "queued", "completed", "failed"}),
    "completed": frozenset({"queued"}),
    "failed": frozenset({"queued"}),
}

_PHASE_ALIAS = {
    "complete": "completed",
    "done": "completed",
    "plan": "planning",
    "pause": "paused",
    "fail": "failed",
    "queue": "queued",
    "pending": "queued",
    "in_progress": "running",
    "in-progress": "running",
    "inprogress": "running",
}

# Public aliases used by the execution loop (canonical storage stays queued/running/...).
PENDING = "queued"
IN_PROGRESS = "running"
PAUSED = "paused"
COMPLETED = "completed"
FAILED = "failed"


class StateTransitionError(ValueError):
    """Raised when an agent or task status change is not allowed."""


def normalize_phase(raw: str | None, *, kind: str = "agent") -> str:
    key = (raw or "").strip().lower()
    key = _PHASE_ALIAS.get(key, key)
    allowed = AGENT_PHASES if kind == "agent" else TASK_STATUSES
    if key not in allowed:
        raise StateTransitionError(f"unknown {kind} status '{raw}'")
    return key


class StateManager:
    """Thread-safe CRUD over SSOT worker tables for one workspace."""

    def __init__(self, root: Path | str | None = None, *, db: SsotDB | None = None) -> None:
        self.root = Path(root).resolve() if root else workspace_root()
        self.db = db or get_ssot_db(self.root)
        self.db.connect()
        self._lock = threading.RLock()
        self.project = self.ensure_project()

    @property
    def project_id(self) -> str:
        return str(self.project["id"])

    def ensure_project(self, *, name: str | None = None, language: str = "mixed") -> dict[str, Any]:
        with self._lock:
            found = self.db.find_project_by_root(str(self.root))
            if found is not None:
                self.project = found
                return found
            created = self.db.put_project(
                name=name or self.root.name or "workspace",
                root_path=str(self.root),
                language=language,
                metadata={"dal": "state_manager"},
            )
            self.project = created
            return created

    def merge_metadata(self, extra: dict[str, Any]) -> dict[str, Any]:
        """Merge keys into project_state.metadata_json. Never drops unknown fields."""
        with self._lock:
            row = self.db.get_project(self.project_id) or self.project
            meta = _json_object(row.get("metadata_json"))
            meta.update(extra or {})
            config = row.get("config_json")
            updated = self.db.put_project(
                name=str(row.get("name") or self.root.name or "workspace"),
                root_path=str(row.get("root_path") or self.root),
                language=str(row.get("language") or "mixed"),
                config=config,
                metadata=meta,
                project_id=self.project_id,
            )
            self.project = updated
            return updated

    def put_task(
        self,
        title: str,
        *,
        status: str = "queued",
        priority: int = 0,
        parent_id: str | None = None,
        payload: Any = None,
        depends_on: Any = None,
        task_id: str | None = None,
        queue_order: int = 0,
    ) -> dict[str, Any]:
        phase = normalize_phase(status, kind="task")
        with self._lock:
            return self.db.put_task(
                project_id=self.project_id,
                title=title,
                status=phase,
                priority=priority,
                queue_order=queue_order,
                parent_id=parent_id,
                payload=payload,
                depends_on=depends_on,
                task_id=task_id,
            )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self.db.get_task(task_id)

    def list_tasks(self, *, status: str | None = None, limit: int = 128) -> list[dict[str, Any]]:
        if status is not None:
            status = normalize_phase(status, kind="task")
        return self.db.list_tasks(self.project_id, status=status, limit=limit)

    def list_queue(self, *, limit: int = 64) -> list[dict[str, Any]]:
        return self.db.list_queue(self.project_id, limit=limit)

    def list_memory(self, *, kind: str | None = None, source: str | None = None, limit: int = 64) -> list[dict[str, Any]]:
        return self.db.list_memory(self.project_id, kind=kind, source=source, limit=limit)

    def add_memory(
        self,
        body: str,
        *,
        kind: str = "chunk",
        source: str = "",
        token_est: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            return self.db.add_memory(
                body=body,
                kind=kind,
                source=source,
                project_id=self.project_id,
                token_est=token_est,
            )

    def list_codebase(self, *, path: str | None = None, limit: int = 64) -> list[dict[str, Any]]:
        return self.db.list_codebase(self.project_id, path=path, limit=limit)

    def transition_task(self, task_id: str, status: str, *, payload: Any = None) -> dict[str, Any]:
        dest = normalize_phase(status, kind="task")
        with self._lock:
            with self.db.transaction():
                row = self.db.get_task(task_id)
                if row is None:
                    raise StateTransitionError(f"task '{task_id}' not found")
                current = normalize_phase(str(row.get("status") or "queued"), kind="task")
                if dest not in TASK_TRANSITIONS.get(current, frozenset()):
                    raise StateTransitionError(f"task {current} -> {dest} is not allowed")
                return self.db.put_task(
                    project_id=str(row["project_id"]),
                    title=str(row.get("title") or ""),
                    status=dest,
                    priority=int(row.get("priority") or 0),
                    queue_order=int(row.get("queue_order") or 0),
                    parent_id=row.get("parent_id"),
                    payload=payload if payload is not None else row.get("payload_json"),
                    depends_on=row.get("depends_on"),
                    task_id=task_id,
                )

    def put_agent(
        self,
        worker_id: str,
        *,
        language: str = "python",
        phase: str = "idle",
        loop_status: str = "",
        task_id: str | None = None,
        telemetry: Any = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = normalize_phase(phase, kind="agent")
        with self._lock:
            return self.db.put_agent(
                worker_id=worker_id,
                language=language,
                phase=normalized,
                loop_status=loop_status,
                project_id=self.project_id,
                task_id=task_id,
                telemetry=telemetry,
                agent_id=agent_id,
            )

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return self.db.get_agent(agent_id)

    def list_agents(self, *, worker_id: str | None = None, limit: int = 128) -> list[dict[str, Any]]:
        return self.db.list_agents(self.project_id, worker_id=worker_id, limit=limit)

    def transition_agent(
        self,
        agent_id: str,
        phase: str,
        *,
        loop_status: str | None = None,
        telemetry: Any = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        dest = normalize_phase(phase, kind="agent")
        with self._lock:
            with self.db.transaction():
                row = self.db.get_agent(agent_id)
                if row is None:
                    raise StateTransitionError(f"agent '{agent_id}' not found")
                current = normalize_phase(str(row.get("phase") or "idle"), kind="agent")
                if dest not in AGENT_TRANSITIONS.get(current, frozenset()):
                    raise StateTransitionError(f"agent {current} -> {dest} is not allowed")
                return self.db.put_agent(
                    worker_id=str(row.get("worker_id") or ""),
                    language=str(row.get("language") or "python"),
                    phase=dest,
                    loop_status=loop_status if loop_status is not None else str(row.get("loop_status") or dest),
                    project_id=row.get("project_id") or self.project_id,
                    task_id=task_id if task_id is not None else row.get("task_id"),
                    telemetry=telemetry if telemetry is not None else row.get("telemetry_json"),
                    agent_id=agent_id,
                )

    def add_artifact(
        self,
        name: str,
        *,
        body: str = "",
        kind: str = "tool",
        task_id: str | None = None,
        exit_code: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            return self.db.add_artifact(
                name=name,
                body=body,
                kind=kind,
                project_id=self.project_id,
                task_id=task_id,
                exit_code=exit_code,
            )

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        return self.db.get_artifact(artifact_id)

    def list_artifacts(self, *, task_id: str | None = None, limit: int = 128) -> list[dict[str, Any]]:
        return self.db.list_artifacts(self.project_id, task_id=task_id, limit=limit)

    def record_decision(
        self,
        choice: str,
        rationale: str = "",
        *,
        task_id: str | None = None,
        branch: str = "plan",
        payload: str = "",
        version_tag: str = "plan",
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            return self.db.upsert_decision(
                project_id=self.project_id,
                task_id=task_id,
                branch=branch,
                parent_id=parent_id,
                choice=choice,
                rationale=rationale,
                version_tag=version_tag,
                payload=payload or rationale,
                summary=choice[:80],
                confidence_score=0.72,
            )


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}
