"""Iterative State-Graph Revision Tracker (ISGRT).

Non-destructive version branches (v1.0 -> v-final) with delta propagation
and pointer rollback. Nodes are never deleted on rollback. Graph nodes and
edges persist in SSOT `decisions_ledger` (WAL SQLite).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from robber.jail import workspace_root
from robber.ssot import QuantumSafeChecksum
from robber.ssot_db import SsotDB, get_ssot_db

_TRACKER: StateGraphTracker | None = None


def _next_version(existing: list[str]) -> str:
    best = 0
    for label in existing:
        if not label.startswith("v"):
            continue
        body = label[1:]
        if body in {"final", "-final"}:
            continue
        try:
            major, _, minor = body.partition(".")
            if int(major) == 1:
                best = max(best, int(minor or 0))
        except ValueError:
            continue
    return f"v1.{best + 1}" if best or any(v.startswith("v1.") for v in existing) else "v1.0"


def _delta_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [text]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return [text]


@dataclass
class StateNode:
    id: str
    version: str
    parent_id: str | None
    branch: str
    summary: str
    payload: str
    checksum: str
    created_at: float
    deltas: list[str] = field(default_factory=list)
    final: bool = False
    confidence_score: float = 0.55

    @property
    def node_id(self) -> str:
        return self.id

    @property
    def version_tag(self) -> str:
        return self.version

    @property
    def hypothesis_delta(self) -> str:
        return "\n".join(self.deltas)

    def public(self) -> dict[str, Any]:
        row = asdict(self)
        row["payload"] = (self.payload or "")[:4000]
        row["node_id"] = self.id
        row["version_tag"] = self.version
        row["hypothesis_delta"] = self.hypothesis_delta
        return row


class StateGraphTracker:
    def __init__(self, root: Path | None = None, *, db: SsotDB | None = None) -> None:
        self.root = (root or workspace_root()).resolve()
        self.path = self.root / ".robber" / "state_graph.json"
        self.nodes: dict[str, StateNode] = {}
        self.head_id: str | None = None
        self.heads: dict[str, str] = {}
        self._lock = threading.RLock()
        self.db = db or get_ssot_db(self.root)
        self.db.connect()
        self.project_id = self._ensure_project()
        self._load()

    def commit(
        self,
        payload: str,
        *,
        summary: str = "",
        branch: str | None = None,
        parent_id: str | None = None,
        confidence_score: float | None = None,
    ) -> StateNode:
        with self._lock:
            branch_name = (branch or self._active_branch() or "main").strip() or "main"
            parent = parent_id or self.heads.get(branch_name) or self.head_id
            versions = [node.version for node in self.nodes.values() if node.branch == branch_name]
            parent_node = self.nodes.get(parent) if parent else None
            score = confidence_score
            if score is None:
                score = min(1.0, (parent_node.confidence_score if parent_node else 0.53) + 0.02)
            node = StateNode(
                id=uuid.uuid4().hex[:12],
                version=_next_version(versions),
                parent_id=parent,
                branch=branch_name,
                summary=(summary or "revision").strip(),
                payload=payload or "",
                checksum=QuantumSafeChecksum.digest(payload or ""),
                created_at=time.time(),
                deltas=self._delta_lines(parent, payload or ""),
                confidence_score=float(score),
            )
            previous_head = self.head_id
            previous_heads = dict(self.heads)
            self.nodes[node.id] = node
            self.heads[branch_name] = node.id
            self.head_id = node.id
            try:
                with self.db.transaction():
                    self._persist_node(node)
                    self._persist_heads()
            except Exception:
                self.nodes.pop(node.id, None)
                self.head_id = previous_head
                self.heads = previous_heads
                raise
            return node

    def branch(self, name: str, *, from_id: str | None = None) -> StateNode:
        source_id = from_id or self.head_id
        if source_id is None or source_id not in self.nodes:
            return self.commit("", summary=f"branch {name}", branch=name)
        source = self.nodes[source_id]
        return self.commit(
            source.payload,
            summary=f"branch {name} from {source.version}",
            branch=name,
            parent_id=source.id,
            confidence_score=source.confidence_score,
        )

    def mark_final(self, node_id: str | None = None) -> StateNode | None:
        with self._lock:
            target_id = node_id or self.head_id
            if target_id is None or target_id not in self.nodes:
                return None
            node = self.nodes[target_id]
            node.final = True
            node.version = "v-final"
            self.head_id = node.id
            self.heads[node.branch] = node.id
            try:
                with self.db.transaction():
                    self._persist_node(node)
                    self._persist_heads()
            except Exception:
                raise
            return node

    def rollback(self, to_id: str) -> StateNode | None:
        with self._lock:
            node = self.nodes.get(to_id)
            if node is None:
                return None
            self.head_id = node.id
            self.heads[node.branch] = node.id
            self._persist_heads()
            return node

    def lineage(self, node_id: str | None = None) -> list[StateNode]:
        target = self.nodes.get(node_id or self.head_id or "")
        if target is None:
            return []
        chain: list[StateNode] = []
        seen: set[str] = set()
        node: StateNode | None = target
        while node is not None and node.id not in seen:
            chain.append(node)
            seen.add(node.id)
            node = self.nodes.get(node.parent_id) if node.parent_id else None
        chain.reverse()
        return chain

    def propagate_delta(self, node_id: str, child_branch: str) -> StateNode | None:
        source = self.nodes.get(node_id)
        if source is None:
            return None
        child_id = self.heads.get(child_branch)
        child = self.nodes.get(child_id) if child_id else None
        merged = source.payload
        if child is not None and child.payload and child.payload not in merged:
            merged = f"{child.payload.rstrip()}\n\n{source.payload}"
        return self.commit(
            merged,
            summary=f"propagate {source.version} -> {child_branch}",
            branch=child_branch,
            parent_id=child.id if child else source.id,
        )

    def propagate_correction(
        self,
        fact: str,
        correction: str | dict[str, Any] | None = None,
        *,
        from_id: str | None = None,
        correction_payload: Any = None,
        node_id: str | None = None,
    ) -> list[StateNode]:
        """Patch a factual correction into dependent nodes without a corpus re-scan."""
        payload = correction_payload
        root_id = node_id or from_id
        fact_text = fact
        corr_text = correction if isinstance(correction, str) else None
        if isinstance(correction, dict):
            payload = correction
            if fact in self.nodes:
                root_id = fact
        if payload is not None:
            if root_id is None and fact in self.nodes:
                root_id = fact
            if isinstance(payload, dict):
                fact_text = str(payload.get("fact") or payload.get("from") or "")
                corr_text = str(
                    payload.get("correction") or payload.get("to") or payload.get("delta") or ""
                )
            else:
                fact_text = ""
                corr_text = str(payload)
        return self._apply_correction(fact_text, corr_text or "", root_id=root_id)

    def render_tree(self) -> str:
        if not self.nodes:
            return "State graph empty - commit a hypothesis to start v1.0."
        children: dict[str | None, list[StateNode]] = {}
        for node in self.nodes.values():
            children.setdefault(node.parent_id, []).append(node)
        for rows in children.values():
            rows.sort(key=lambda item: item.created_at)
        known = set(self.nodes)
        roots = [
            node
            for node in self.nodes.values()
            if node.parent_id is None or node.parent_id not in known
        ]
        roots.sort(key=lambda item: item.created_at)
        lines: list[str] = [
            f"ISGRT  {len(self.nodes)} nodes - branches {', '.join(sorted(self.heads)) or '-'}"
        ]

        def walk(node: StateNode, prefix: str, last: bool) -> None:
            branch_mark = "+-" if last else "|-"
            head = "*" if node.id == self.head_id else " "
            final = "  [v-final]" if node.final or node.version == "v-final" else ""
            lines.append(
                f"{prefix}{branch_mark}{head} {node.version} [{node.branch}] {node.summary}{final}"
            )
            kids = children.get(node.id) or []
            next_prefix = f"{prefix}{'   ' if last else '|  '}"
            for index, child in enumerate(kids):
                walk(child, next_prefix, index == len(kids) - 1)

        for index, root in enumerate(roots):
            walk(root, "", index == len(roots) - 1)
        return "\n".join(lines)

    def current(self) -> StateNode | None:
        return self.nodes.get(self.head_id) if self.head_id else None

    def history(self, branch: str | None = None) -> list[StateNode]:
        rows = list(self.nodes.values())
        if branch:
            rows = [node for node in rows if node.branch == branch]
        rows.sort(key=lambda item: item.created_at)
        return rows

    def public(self) -> dict[str, Any]:
        current = self.current()
        return {
            "head": current.public() if current else None,
            "heads": dict(self.heads),
            "nodes": len(self.nodes),
            "branches": sorted(self.heads),
        }

    def _apply_correction(self, fact: str, correction: str, *, root_id: str | None) -> list[StateNode]:
        if not root_id or root_id not in self.nodes:
            return []
        targets = [self.nodes[root_id], *self._descendants(root_id)]
        updated: list[StateNode] = []
        seen_branches: set[str] = set()
        for node in targets:
            body = node.payload or ""
            if fact and fact in body:
                next_body = body.replace(fact, correction)
            elif node.id == root_id:
                next_body = f"{body.rstrip()}\n\n[CORRECTION] {correction}".strip()
            else:
                continue
            if next_body == body:
                continue
            key = f"{node.branch}:{node.id}"
            if key in seen_branches:
                continue
            seen_branches.add(key)
            updated.append(
                self.commit(
                    next_body,
                    summary=f"correction -> {node.version}",
                    branch=node.branch,
                    parent_id=node.id,
                    confidence_score=min(1.0, max(node.confidence_score, 0.8)),
                )
            )
        return updated

    def _active_branch(self) -> str:
        if self.head_id and self.head_id in self.nodes:
            return self.nodes[self.head_id].branch
        return "main"

    def _descendants(self, node_id: str) -> list[StateNode]:
        kids = [node for node in self.nodes.values() if node.parent_id == node_id]
        out = list(kids)
        for child in kids:
            out.extend(self._descendants(child.id))
        return out

    def _delta_lines(self, parent_id: str | None, payload: str) -> list[str]:
        if not parent_id or parent_id not in self.nodes:
            return ["+ genesis"]
        before = set((self.nodes[parent_id].payload or "").splitlines())
        after = set((payload or "").splitlines())
        added = sorted(after - before)
        removed = sorted(before - after)
        lines = [f"+ {line}" for line in added[:24]] + [f"- {line}" for line in removed[:24]]
        return lines or ["~ no textual delta"]

    def _ensure_project(self) -> str:
        found = self.db.find_project_by_root(str(self.root))
        if found is not None:
            return str(found["id"])
        created = self.db.put_project(
            name=self.root.name or "workspace",
            root_path=str(self.root),
            language="mixed",
            metadata={"isgrt": True},
        )
        return str(created["id"])

    def _persist_node(self, node: StateNode) -> None:
        parent = node.parent_id if node.parent_id in self.nodes else None
        if node.parent_id and self.db.get_decision(node.parent_id) is not None:
            parent = node.parent_id
        self.db.upsert_decision(
            decision_id=node.id,
            project_id=self.project_id,
            parent_id=parent,
            branch=node.branch,
            choice=node.version,
            rationale=node.summary,
            version_tag=node.version,
            hypothesis_delta=node.deltas,
            confidence_score=node.confidence_score,
            payload=node.payload,
            checksum=node.checksum,
            summary=node.summary,
            is_final=1 if node.final else 0,
            is_head=1 if node.id == self.head_id else 0,
            created_at=node.created_at,
        )

    def _persist_heads(self) -> None:
        self.db.put_meta(f"isgrt:{self.project_id}:head", self.head_id or "")
        self.db.put_meta(f"isgrt:{self.project_id}:heads", json.dumps(self.heads, ensure_ascii=True))
        if self.head_id and self.head_id in self.nodes:
            self._persist_node(self.nodes[self.head_id])

    def _node_from_row(self, row: dict[str, Any]) -> StateNode:
        version = str(row.get("version_tag") or row.get("choice") or "v1.0")
        return StateNode(
            id=str(row.get("id") or uuid.uuid4().hex[:12]),
            version=version,
            parent_id=row.get("parent_id"),
            branch=str(row.get("branch") or "main"),
            summary=str(row.get("summary") or row.get("rationale") or ""),
            payload=str(row.get("payload") or ""),
            checksum=str(row.get("checksum") or ""),
            created_at=float(row.get("created_at") or 0.0),
            deltas=_delta_list(row.get("hypothesis_delta")),
            final=bool(int(row.get("is_final") or 0)) or version == "v-final",
            confidence_score=float(row.get("confidence_score") or 0.55),
        )

    def _load(self) -> None:
        rows = self.db.list_decisions(self.project_id, graph_only=True, limit=2000)
        if rows:
            for row in rows:
                node = self._node_from_row(row)
                self.nodes[node.id] = node
            head = self.db.get_meta(f"isgrt:{self.project_id}:head") or ""
            raw_heads = self.db.get_meta(f"isgrt:{self.project_id}:heads") or "{}"
            try:
                loaded_heads = json.loads(raw_heads)
            except json.JSONDecodeError:
                loaded_heads = {}
            if isinstance(loaded_heads, dict):
                self.heads = {str(key): str(value) for key, value in loaded_heads.items()}
            self.head_id = head if head in self.nodes else None
            if self.head_id is None and self.nodes:
                latest = max(self.nodes.values(), key=lambda item: item.created_at)
                self.head_id = latest.id
            if self.head_id and self.head_id in self.nodes:
                self.heads.setdefault(self.nodes[self.head_id].branch, self.head_id)
            return
        self._load_json_legacy()

    def _load_json_legacy(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, UnicodeError):
            return
        imported: list[StateNode] = []
        for row in raw.get("nodes") or []:
            if not isinstance(row, dict):
                continue
            node = StateNode(
                id=str(row.get("id") or uuid.uuid4().hex[:12]),
                version=str(row.get("version") or "v1.0"),
                parent_id=row.get("parent_id"),
                branch=str(row.get("branch") or "main"),
                summary=str(row.get("summary") or ""),
                payload=str(row.get("payload") or ""),
                checksum=str(row.get("checksum") or ""),
                created_at=float(row.get("created_at") or 0.0),
                deltas=[str(item) for item in (row.get("deltas") or [])],
                final=bool(row.get("final")),
                confidence_score=float(row.get("confidence_score") or 0.55),
            )
            self.nodes[node.id] = node
            imported.append(node)
        self.heads = {str(key): str(value) for key, value in (raw.get("heads") or {}).items()}
        self.head_id = raw.get("head_id")
        imported.sort(key=lambda item: item.created_at)
        try:
            with self.db.transaction():
                for node in imported:
                    self._persist_node(node)
                self._persist_heads()
        except Exception:
            pass


def get_state_graph() -> StateGraphTracker:
    global _TRACKER
    if _TRACKER is None:
        _TRACKER = StateGraphTracker()
    return _TRACKER


def bind_state_graph(tracker: StateGraphTracker | None) -> StateGraphTracker:
    global _TRACKER
    _TRACKER = tracker if tracker is not None else StateGraphTracker()
    return _TRACKER
