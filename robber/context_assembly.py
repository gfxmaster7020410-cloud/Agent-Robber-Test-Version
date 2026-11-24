"""Dynamic context retrieval for the autonomous execution loop.

Pulls the active task, codebase_state snippets, memory_store notes, and
prior-task artifacts, then truncates to the current model window.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from robber.config import DEFAULT_CONTEXT_WINDOW, WORKER_PROMPT_TOKEN_CAP
from robber.handoff_ledger import HandoffLedger
from robber.jail import workspace_root
from robber.planner import parse_depends
from robber.ssot_context import CHARS_PER_TOKEN, MIN_CONTEXT_WINDOW, estimate_tokens, turn_pack_window
from robber.state_manager import StateManager


def merge_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    text = (raw or "").strip() if isinstance(raw, str) else ""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}
    return dict(data) if isinstance(data, dict) else {"value": data}


def clip_context(text: str, budget: int) -> str:
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


@dataclass
class AssembledContext:
    task_id: str
    text: str
    tokens: int
    budget: int
    sections: dict[str, str] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        row = asdict(self)
        row["text"] = (self.text or "")[:400]
        return row


class ContextAssembler:
    """Assemble a bounded prompt pack from SSOT tables for one task."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        manager: StateManager | None = None,
        window: int | None = None,
        model_capacity: int | None = None,
    ) -> None:
        self.root = Path(root).resolve() if root else workspace_root()
        self.manager = manager or StateManager(self.root)
        self.window = DEFAULT_CONTEXT_WINDOW if window is None else int(window)
        self.model_capacity = model_capacity

    def budget(self) -> int:
        return turn_pack_window(self.window, self.model_capacity)

    def assemble(self, task: dict[str, Any] | str) -> AssembledContext:
        row = self.manager.get_task(task) if isinstance(task, str) else task
        if row is None:
            raise KeyError("task not found")
        payload = merge_payload(row.get("payload_json"))
        files = _files_of(payload)
        compressor = None
        try:
            from robber.memory.context_compressor import ContextCompressor

            compressor = ContextCompressor(self.root, manager=self.manager)
        except Exception:
            compressor = None
        sections = {
            "task": self._task_section(row, payload),
            "codebase": self._codebase_section(files),
            "memory": self._memory_section(),
            "prior": self._prior_section(row, compressor=compressor),
            "handoff": self._handoff_section(),
        }
        limit = self.budget()
        worker_mode = int(self.window or 0) <= WORKER_PROMPT_TOKEN_CAP * 2
        packed = self._pack_sections(sections, limit, worker_mode=worker_mode)
        if compressor is not None:
            packed = compressor.prune_prompt(
                packed,
                keep=[str(row.get("title") or ""), str(row.get("id") or "")],
            )
        budget = min(limit, WORKER_PROMPT_TOKEN_CAP) if worker_mode else limit
        packed = clip_context(packed, budget)
        try:
            from robber.memory.ram_guard import get_ram_guard

            get_ram_guard(self.root).maybe_reclaim()
        except Exception:
            pass
        return AssembledContext(
            task_id=str(row.get("id") or ""),
            text=packed,
            tokens=estimate_tokens(packed),
            budget=budget,
            sections=sections,
            files=files,
        )

    def _pack_sections(self, sections: dict[str, str], limit: int, *, worker_mode: bool) -> str:
        try:
            from robber.memory.token_budgeter import TokenBudgeter

            cap = WORKER_PROMPT_TOKEN_CAP if worker_mode else limit
            budgeter = TokenBudgeter(limit, hard_cap=cap)
            allocated = budgeter.allocate(
                {
                    "system": "",
                    "goal": "\n".join(
                        part
                        for part in (sections.get("task") or "", sections.get("handoff") or "")
                        if part.strip()
                    ),
                    "graph": sections.get("codebase") or "",
                    "history": "\n".join(
                        part
                        for part in (sections.get("memory") or "", sections.get("prior") or "")
                        if part.strip()
                    ),
                }
            )
            labeled = []
            if allocated.get("goal"):
                labeled.append("## task\n" + allocated["goal"])
            if allocated.get("graph"):
                labeled.append("## codebase\n" + allocated["graph"])
            if allocated.get("history"):
                labeled.append("## prior\n" + allocated["history"])
            packed, _, _ = budgeter.fit("\n\n".join(labeled).strip())
            return packed
        except Exception:
            parts = [f"## {name}\n{body}" for name, body in sections.items() if (body or "").strip()]
            return clip_context("\n\n".join(parts).strip(), limit)

    def _task_section(self, row: dict[str, Any], payload: dict[str, Any]) -> str:
        checkpoint = payload.get("checkpoint") if isinstance(payload.get("checkpoint"), dict) else {}
        lines = [
            f"id: {row.get('id')}",
            f"title: {row.get('title')}",
            f"status: {row.get('status')}",
            f"tag: {payload.get('tag') or '-'}",
            f"task_type: {payload.get('task_type') or '-'}",
            f"depends_on: {', '.join(parse_depends(row.get('depends_on'))) or '-'}",
            f"inputs: {', '.join(_as_list(payload.get('inputs'))) or '-'}",
            f"outputs: {', '.join(_as_list(payload.get('outputs'))) or '-'}",
            f"tools: {', '.join(_as_list(payload.get('tools'))) or '-'}",
        ]
        if checkpoint:
            lines.append(f"checkpoint: {checkpoint.get('reason') or 'resume'}")
            if checkpoint.get("output"):
                lines.append(str(checkpoint.get("output") or "")[:800])
        return "\n".join(lines)

    def _codebase_section(self, files: list[str]) -> str:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in files[:8]:
            for item in self.manager.list_codebase(path=path, limit=4):
                ident = str(item.get("id") or "")
                if ident in seen:
                    continue
                seen.add(ident)
                rows.append(item)
        if not rows:
            rows = self.manager.list_codebase(limit=6)
        lines: list[str] = []
        for item in rows[:8]:
            lines.append(
                f"{item.get('path')} [{item.get('language') or '-'}] "
                f"{(item.get('ast_summary') or item.get('symbol_index') or '')[:400]}"
            )
        return "\n".join(lines)

    def _memory_section(self) -> str:
        notes = self.manager.list_memory(limit=16)
        summaries = [item for item in notes if str(item.get("kind") or "") == "summary"]
        others = [item for item in notes if str(item.get("kind") or "") != "summary"]
        ordered = summaries[-8:] + others[-4:]
        lines: list[str] = []
        for item in ordered:
            body = (item.get("body") or "").strip()
            if not body:
                continue
            kind = str(item.get("kind") or "-")
            if kind == "summary":
                lines.append(f"[summary/{item.get('source') or '-'}] {body[:600]}")
            else:
                lines.append(f"[{kind}/{item.get('source') or '-'}] {body[:240]}")
        return "\n".join(lines)

    def _prior_section(self, row: dict[str, Any], *, compressor: Any = None) -> str:
        deps = parse_depends(row.get("depends_on"))
        lines: list[str] = []
        for dep in deps[:8]:
            parent = self.manager.get_task(dep)
            title = (parent or {}).get("title") or dep
            status = (parent or {}).get("status") or "-"
            lines.append(f"{dep} {status} {title}")
            summary = ""
            if compressor is not None:
                try:
                    summary = compressor.summary_for(dep)
                except Exception:
                    summary = ""
            if summary:
                lines.append(f"  summary: {summary}")
                continue
            for artifact in self.manager.list_artifacts(task_id=dep, limit=3):
                body = str(artifact.get("body") or "")
                if compressor is not None:
                    try:
                        body = compressor.clip_artifact_body(body)
                    except Exception:
                        body = body[:400]
                else:
                    body = body[:400]
                lines.append(f"  artifact {artifact.get('name')}: {body}")
        return "\n".join(lines)

    def _handoff_section(self) -> str:
        try:
            return HandoffLedger(self.root).export_text()[:1200]
        except OSError:
            return ""


def _as_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _files_of(payload: dict[str, Any]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for key in ("inputs", "outputs", "files"):
        for item in _as_list(payload.get(key)):
            if item not in seen:
                seen.add(item)
                found.append(item)
    return found
