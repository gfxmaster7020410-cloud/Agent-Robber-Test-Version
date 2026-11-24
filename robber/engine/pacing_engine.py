"""Recursive micro-batching and tactical cool-down for long-horizon research.

Slice 100–200+ page documents with MemoryMappedChunkStreamer, checkpoint into
AMACE (state graph + handoff ledger), pause between heavy inferences, then
resume from bidirectional gap scan and ACRM synthesis.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

from robber.config import (
    HORIZON_SIZE_BYTES,
    MAX_TOKENS_PER_TURN,
    MMAP_WINDOW_BYTES,
    PACING_BATCH_TIMEOUT,
    PACING_COOLDOWN_BASE,
    PACING_COOLDOWN_MAX,
    PACING_HEAVY_STREAK,
    PACING_MACRO_CHUNKS,
    PACING_MAX_CHUNKS,
    PACING_MICRO_LINES,
)
from robber.conflict_resolver import ConflictResolutionMatrix
from robber.handoff_ledger import HandoffLedger
from robber.jail import JailError, workspace_root
from robber.ssot import MemoryMappedChunkStreamer
from robber.ssot_context import bidirectional_gap_scan
from robber.state_graph import StateGraphTracker

HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+\S|Chapter\s+\d+|SECTION\s+\d+|(?:Page|PAGE)\s+\d+\b|[A-Z][A-Z0-9 \-]{11,}$)",
    re.MULTILINE,
)
HORIZON_RE = re.compile(
    r"\b(long[- ]?horizon|micro[- ]?batch|200\+?\s*pages?|100\+?\s*pages?|"
    r"research paper|tactical pause)\b",
    re.IGNORECASE,
)
BATCH_MARK = "[HORIZON_BATCH"
AnalyzeFn = Callable[["MicroChunk", "HorizonJob"], Awaitable[str]]
SleepFn = Callable[[float], Awaitable[Any]]


@dataclass
class MicroChunk:
    id: str
    index: int
    macro_id: str
    macro_title: str
    path: str
    start: int
    end: int
    text: str

    def preview(self) -> str:
        line = (self.text or "").strip().splitlines()
        return (line[0] if line else "")[:120]

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "index": self.index,
            "macro_id": self.macro_id,
            "macro_title": self.macro_title,
            "path": self.path,
            "start": self.start,
            "end": self.end,
            "preview": self.preview(),
        }


@dataclass
class MacroBlock:
    id: str
    title: str
    path: str
    start: int
    end: int
    chunk_ids: list[str] = field(default_factory=list)


@dataclass
class HorizonJob:
    id: str
    query: str
    path: str
    cursor: int = 0
    status: str = "ready"
    insights: dict[str, str] = field(default_factory=dict)
    timeouts: list[str] = field(default_factory=list)
    pauses: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    synthesis: str = ""
    chunks: list[MicroChunk] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.chunks)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "query": (self.query or "")[:200],
            "path": self.path,
            "cursor": self.cursor,
            "total": self.total,
            "status": self.status,
            "pauses": self.pauses,
            "timeouts": len(self.timeouts),
            "insights": len(self.insights),
        }


class CoolDownScheduler:
    """Grow pauses between heavy inferences so rate limits and caches can settle."""

    def __init__(
        self,
        *,
        base: float = PACING_COOLDOWN_BASE,
        maximum: float = PACING_COOLDOWN_MAX,
        heavy_after: int = PACING_HEAVY_STREAK,
    ) -> None:
        self.base = max(0.0, float(base))
        self.maximum = max(self.base, float(maximum))
        self.heavy_after = max(1, int(heavy_after))
        self.streak = 0

    def mark_heavy(self) -> None:
        self.streak += 1

    def mark_light(self) -> None:
        self.streak = 0

    def interval(self) -> float:
        if self.streak < self.heavy_after:
            return min(self.base, self.maximum) if self.streak else 0.0
        shift = min(self.streak - self.heavy_after + 1, 5)
        return min(self.maximum, self.base * (2**shift))

    async def pause(self, *, sleep: SleepFn | None = None) -> float:
        wait = self.interval()
        if wait <= 0:
            return 0.0
        sleeper = sleep or asyncio.sleep
        try:
            await asyncio.wait_for(sleeper(wait), timeout=wait + 1.0)
        except asyncio.TimeoutError:
            pass
        return wait


class PacingEngine:
    """Decompose, checkpoint, cool down, resume, and synthesize a horizon job."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or workspace_root()).resolve()
        self.dir = self.root / ".robber" / "horizon"
        self.scheduler = CoolDownScheduler()
        self.pause_requested = False
        self.job: HorizonJob | None = None
        self.request_count = 0
        self.token_used = 0
        self.token_budget = MAX_TOKENS_PER_TURN
        self.health = "ok"
        self.execution_task_id: str | None = None

    def looks_like_horizon(self, query: str, *, path: Path | None = None) -> bool:
        text = query or ""
        if BATCH_MARK in text:
            return False
        if HORIZON_RE.search(text):
            return True
        target = path
        if target is None:
            target = self._path_from_query(text)
        try:
            return bool(target and target.is_file() and target.stat().st_size >= HORIZON_SIZE_BYTES)
        except OSError:
            return False

    def decompose(self, query: str, *, path: Path | None = None) -> list[MicroChunk]:
        target = path or self._path_from_query(query)
        pieces: list[tuple[str, int, int, str]] = []
        if target is not None and target.is_file():
            rel = self._rel(target)
            streamer = MemoryMappedChunkStreamer(target, window=MMAP_WINDOW_BYTES)
            for piece in streamer.iter_line_chunks(
                rel,
                lines_per_chunk=PACING_MICRO_LINES,
                max_chunks=PACING_MAX_CHUNKS,
            ):
                pieces.append((piece.path, piece.start, piece.end, piece.text))
        elif len(query or "") > 4_000:
            pieces.extend(self._split_inline(query, "inline.txt"))
        else:
            body = (query or "").strip() or "(empty)"
            pieces.append(("prompt", 1, max(1, body.count("\n") + 1), body))
        return list(self._to_micro(pieces))

    def create_job(self, query: str, *, path: Path | None = None) -> HorizonJob:
        chunks = self.decompose(query, path=path)
        target = path or self._path_from_query(query)
        job = HorizonJob(
            id=uuid.uuid4().hex[:12],
            query=query or "",
            path=self._rel(target) if target is not None else "",
            chunks=chunks,
        )
        self.job = job
        self.pause_requested = False
        self.scheduler = CoolDownScheduler()
        self.request_count = 0
        self.token_used = 0
        self.health = "ok"
        self.execution_task_id = None
        self._save(job)
        return job

    def request_pause(self) -> None:
        self.pause_requested = True

    def cool_down_interval(self) -> float:
        return self.scheduler.interval()

    def record_request(self, tokens: int = 0, *, heavy: bool = True) -> None:
        self.request_count += 1
        self.token_used += max(0, int(tokens))
        if heavy:
            self.scheduler.mark_heavy()
        else:
            self.scheduler.mark_light()
        if self.token_used >= self.token_budget:
            self.health = "budget"
            self.pause_requested = True
        elif self.cool_down_interval() >= self.scheduler.maximum:
            self.health = "throttled"

    def should_pause(self) -> bool:
        if self.pause_requested:
            return True
        if self.token_used >= self.token_budget:
            self.health = "budget"
            return True
        return False

    def flush_caches(self) -> None:
        self.scheduler.mark_light()
        if self.health != "budget":
            self.health = "ok"

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "requests": self.request_count,
            "tokens": self.token_used,
            "budget": self.token_budget,
            "cool_down_interval": self.cool_down_interval(),
            "health": self.health,
            "pause_requested": self.pause_requested,
            "task_id": self.execution_task_id,
        }

    def bind_execution_task(self, task_id: str | None) -> None:
        self.execution_task_id = task_id

    def commit_execution_checkpoint(
        self,
        *,
        manager: Any,
        task: dict[str, Any],
        reason: str = "tactical-pause",
        output: str = "",
        context: str = "",
    ) -> dict[str, Any]:
        """Persist task_state + handoff_ledger before a tactical cool-down."""
        from robber.state_manager import normalize_phase
        from robber.context_assembly import merge_payload

        task_id = str(task.get("id") or "")
        self.execution_task_id = task_id or self.execution_task_id
        payload = merge_payload(task.get("payload_json"))
        payload["checkpoint"] = {
            "reason": reason,
            "output": (output or "")[:4000],
            "context": (context or "")[:1200],
            "tokens": self.token_used,
            "requests": self.request_count,
            "health": self.health,
            "ts": time.time(),
        }
        current = normalize_phase(str(task.get("status") or "queued"), kind="task")
        if current in {"queued", "running"}:
            row = manager.transition_task(task_id, "paused", payload=payload)
        elif current == "paused":
            row = manager.put_task(
                str(task.get("title") or ""),
                status="paused",
                priority=int(task.get("priority") or 0),
                queue_order=int(task.get("queue_order") or 0),
                parent_id=task.get("parent_id"),
                payload=payload,
                depends_on=task.get("depends_on"),
                task_id=task_id,
            )
        else:
            row = task
        body = (
            f"LOOP CHECKPOINT {task_id} {reason}\n"
            f"title: {task.get('title') or '-'}\n"
            f"tokens: {self.token_used}/{self.token_budget}\n"
            f"{(output or '')[:800]}"
        )
        tracker = StateGraphTracker(self.root)
        node = tracker.commit(body, summary=f"loop {reason}")
        payload["checkpoint"]["graph_id"] = node.id
        if str(row.get("status") or "") == "paused":
            row = manager.put_task(
                str(row.get("title") or task.get("title") or ""),
                status="paused",
                priority=int(row.get("priority") or 0),
                queue_order=int(row.get("queue_order") or 0),
                parent_id=row.get("parent_id"),
                payload=payload,
                depends_on=row.get("depends_on"),
                task_id=task_id,
            )
        HandoffLedger(self.root).post(body, source="loop", target="*", kind="loop_checkpoint")
        return row

    def load_job(self, job_id: str | None = None) -> HorizonJob | None:
        if job_id and self.job and self.job.id == job_id:
            return self.job
        if job_id:
            loaded = self._load_id(job_id)
            if loaded is not None:
                self.job = loaded
            return loaded
        latest = self._latest()
        if latest is not None:
            self.job = latest
        return latest

    def checkpoint(self, job: HorizonJob, *, reason: str = "pause") -> HorizonJob:
        job.updated_at = time.time()
        current = job.chunks[job.cursor] if 0 <= job.cursor < job.total else None
        remaining = job.chunks[job.cursor :]
        payload = [
            f"HORIZON {job.id} {job.cursor}/{job.total} {reason}",
            f"query: {(job.query or '')[:200]}",
            f"path: {job.path}",
        ]
        if current is not None:
            payload.append(f"next: {current.macro_title} L{current.start}-{current.end}")
        tracker = StateGraphTracker(self.root)
        tracker.commit("\n".join(payload), summary=f"horizon {job.cursor}/{job.total} {reason}")
        if reason != "final" and remaining:
            chunk = remaining[0]
            body = (
                f"[UNHANDLED] horizon gap {chunk.id} {chunk.macro_title} "
                f"L{chunk.start}-{chunk.end}\n{chunk.preview()}"
            )
            HandoffLedger(self.root).post(body, source="horizon", target="*", kind="horizon_gap")
        self._save(job)
        return job

    def pending_gaps(self, job: HorizonJob | None = None) -> list[str]:
        active = job or self.job
        ledger = HandoffLedger(self.root)
        extra = ledger.export_text()
        if active is not None:
            extra = extra + "\n" + "\n".join(
                f"[UNHANDLED] {chunk.macro_title} {chunk.id}"
                for chunk in active.chunks[active.cursor :]
            )
        return bidirectional_gap_scan(extra)

    def next_chunk(self, job: HorizonJob) -> MicroChunk | None:
        if job.cursor >= job.total:
            return None
        gaps = [item.lower() for item in self.pending_gaps(job)]
        if gaps:
            for offset, chunk in enumerate(job.chunks[job.cursor :]):
                blob = f"{chunk.id} {chunk.macro_title} {chunk.preview()}".lower()
                if any(gap[:40] in blob or chunk.id in gap for gap in gaps):
                    job.cursor = job.cursor + offset
                    return chunk
        chunk = job.chunks[job.cursor]
        return chunk

    async def run(
        self,
        query: str,
        *,
        analyze: AnalyzeFn | None = None,
        sleep: SleepFn | None = None,
        job: HorizonJob | None = None,
        timeout: float | None = None,
    ) -> HorizonJob:
        active = job or self.job or self.create_job(query)
        self.job = active
        if not self.pause_requested:
            active.status = "running"
        worker = analyze or _default_analyze
        limit = PACING_BATCH_TIMEOUT if timeout is None else float(timeout)
        while True:
            if self.pause_requested or active.status == "paused":
                active.status = "paused"
                active.pauses += 1
                self.checkpoint(active, reason="tactical-pause")
                return active
            chunk = self.next_chunk(active)
            if chunk is None:
                break
            try:
                insight = await asyncio.wait_for(worker(chunk, active), timeout=max(0.05, limit))
                active.insights[chunk.id] = (insight or "").strip() or chunk.preview()
                self.scheduler.mark_heavy()
            except asyncio.TimeoutError:
                active.timeouts.append(chunk.id)
                active.insights[chunk.id] = f"TIMEOUT after {limit}s on {chunk.id}"
                self.scheduler.mark_heavy()
            except asyncio.CancelledError:
                active.status = "paused"
                self.checkpoint(active, reason="cancelled")
                raise
            except Exception as exc:
                active.insights[chunk.id] = f"{type(exc).__name__}: {exc}"
                self.scheduler.mark_heavy()
            active.cursor = min(active.cursor + 1, active.total)
            self.checkpoint(active, reason="batch")
            if self.pause_requested or active.status == "paused":
                active.status = "paused"
                active.pauses += 1
                self.checkpoint(active, reason="tactical-pause")
                return active
            if active.cursor < active.total:
                await self.scheduler.pause(sleep=sleep)
                active.pauses += 1
        active.status = "done"
        active.synthesis = self.synthesize(active)
        self.checkpoint(active, reason="final")
        self._save(active)
        return active

    def synthesize(self, job: HorizonJob) -> str:
        if not job.insights:
            return "Horizon empty - no micro-batches produced insights."
        matrix = ConflictResolutionMatrix()
        outputs = {key: value for key, value in job.insights.items() if (value or "").strip()}
        report = matrix.report(outputs)
        reconciled = matrix.reconcile(outputs)
        lines = [
            f"HORIZON SYNTHESIS  {job.id}  {job.total} batches  conflicts={report.get('count')}",
            f"path: {job.path or '(inline)'}",
        ]
        for item in report.get("conflicts") or []:
            lines.append(
                f"- [{item.get('kind')}] {item.get('left_node')} vs {item.get('right_node')} "
                f"-> {item.get('winner')} {item.get('detail')}"
            )
        lines.append("")
        for chunk in job.chunks:
            body = reconciled.get(chunk.id) or job.insights.get(chunk.id) or ""
            if not body:
                continue
            lines.append(f"### {chunk.macro_title}  L{chunk.start}-{chunk.end}")
            lines.append(body.strip()[:1200])
            lines.append("")
        return "\n".join(lines).strip()

    def _to_micro(self, pieces: list[tuple[str, int, int, str]]) -> Iterator[MicroChunk]:
        macro_index = 0
        macro_id = uuid.uuid4().hex[:8]
        macro_title = "Opening"
        in_macro = 0
        for index, (path, start, end, text) in enumerate(pieces):
            heading = _heading(text)
            if heading or in_macro >= PACING_MACRO_CHUNKS:
                macro_index += 1
                macro_id = uuid.uuid4().hex[:8]
                macro_title = heading or f"Block {macro_index}"
                in_macro = 0
            in_macro += 1
            yield MicroChunk(
                id=f"h{index:04d}",
                index=index,
                macro_id=macro_id,
                macro_title=macro_title[:80],
                path=path,
                start=start,
                end=end,
                text=text,
            )

    def _split_inline(self, text: str, path: str) -> list[tuple[str, int, int, str]]:
        lines = (text or "").splitlines()
        out: list[tuple[str, int, int, str]] = []
        size = PACING_MICRO_LINES
        for start in range(0, len(lines), size):
            end = min(len(lines), start + size)
            body = "\n".join(lines[start:end])
            if body.strip():
                out.append((path, start + 1, end, body))
            if len(out) >= PACING_MAX_CHUNKS:
                break
        return out

    def _path_from_query(self, query: str) -> Path | None:
        from robber.tools.workspace import pick_read_target

        relative = pick_read_target(query or "")
        if not relative:
            return None
        try:
            from robber.jail import resolve_inside

            return resolve_inside(self.root, relative)
        except (JailError, OSError, ValueError):
            candidate = (self.root / relative).resolve()
            try:
                candidate.relative_to(self.root)
            except ValueError:
                return None
            return candidate if candidate.is_file() else None

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def _save(self, job: HorizonJob) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        row = {
            "id": job.id,
            "query": job.query,
            "path": job.path,
            "cursor": job.cursor,
            "status": job.status,
            "insights": job.insights,
            "timeouts": job.timeouts,
            "pauses": job.pauses,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "synthesis": job.synthesis,
            "chunks": [chunk.public() for chunk in job.chunks],
        }
        tmp = self.dir / f"{job.id}.json.tmp"
        dest = self.dir / f"{job.id}.json"
        tmp.write_text(json.dumps(row, indent=2), encoding="utf-8")
        os.replace(tmp, dest)

    def _load_id(self, job_id: str) -> HorizonJob | None:
        path = self.dir / f"{job_id}.json"
        if not path.is_file():
            return None
        return self._from_file(path)

    def _latest(self) -> HorizonJob | None:
        if not self.dir.is_dir():
            return None
        files = sorted(self.dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        for path in files:
            loaded = self._from_file(path)
            if loaded is not None:
                return loaded
        return None

    def _from_file(self, path: Path) -> HorizonJob | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        chunks: list[MicroChunk] = []
        for row in raw.get("chunks") or []:
            if not isinstance(row, dict):
                continue
            chunks.append(
                MicroChunk(
                    id=str(row.get("id") or f"h{len(chunks):04d}"),
                    index=int(row.get("index") or len(chunks)),
                    macro_id=str(row.get("macro_id") or ""),
                    macro_title=str(row.get("macro_title") or "Block"),
                    path=str(row.get("path") or ""),
                    start=int(row.get("start") or 1),
                    end=int(row.get("end") or 1),
                    text=str(row.get("text") or ""),
                )
            )
        source = str(raw.get("path") or "")
        query = str(raw.get("query") or "")
        if source:
            target = (self.root / source).resolve()
            if target.is_file():
                chunks = self.decompose(query or source, path=target)
        elif query:
            rebuilt = self.decompose(query)
            if rebuilt:
                chunks = rebuilt
        return HorizonJob(
            id=str(raw.get("id") or path.stem),
            query=str(raw.get("query") or ""),
            path=source,
            cursor=int(raw.get("cursor") or 0),
            status=str(raw.get("status") or "ready"),
            insights={str(key): str(value) for key, value in (raw.get("insights") or {}).items()},
            timeouts=[str(item) for item in (raw.get("timeouts") or [])],
            pauses=int(raw.get("pauses") or 0),
            created_at=float(raw.get("created_at") or 0.0),
            updated_at=float(raw.get("updated_at") or 0.0),
            synthesis=str(raw.get("synthesis") or ""),
            chunks=chunks,
        )


def _heading(text: str) -> str:
    for line in (text or "").splitlines()[:8]:
        stripped = line.strip()
        if HEADING_RE.match(stripped):
            return stripped[:80]
    return ""


async def _default_analyze(chunk: MicroChunk, job: HorizonJob) -> str:
    lines = [row.strip() for row in (chunk.text or "").splitlines() if row.strip()][:4]
    return f"{chunk.macro_title}: " + " ".join(lines)[:500]


def looks_like_horizon(query: str, *, root: Path | None = None) -> bool:
    return PacingEngine(root).looks_like_horizon(query)
