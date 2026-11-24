"""Adjustable SSOT unified context window — logical budget, not an in-RAM buffer.

Default working context is 200k tokens. The operator budget is unbounded
(K / M / B / T shorthand). A 90% model-capacity trim is opt-in via enforce_cap.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robber.config import (
    CONTEXT_TRIM_RATIO,
    DEFAULT_CONTEXT_WINDOW,
    MAX_TOKENS_PER_TURN,
    MIN_CONTEXT_WINDOW,
    MMAP_WINDOW_BYTES,
    PACING_MAX_CHUNKS,
    PACING_MICRO_LINES,
    SSOT_INJECT_TOKEN_CAP,
    SSOT_PACK_HEADROOM,
    SSOT_PACK_TOKEN_CAP,
)
from robber.jail import JailError, resolve_inside, workspace_root
from robber.notes import load_notes
from robber.ssot import (
    DeadlockRecoveryWatchdog,
    FileMicroLock,
    Hit,
    MAX_FILES,
    MemoryMappedChunkStreamer,
    SECRET_NAMES,
    atomic_section_lock,
    get_index,
    skip_reason,
)

CHARS_PER_TOKEN = 4
# Rough tokens-per-chunk used only to decide how many ranked hits to fetch.
TOKENS_PER_CHUNK_HINT = 64

_MULTIPLIER_MAP = {
    "K": 1_000,
    "M": 1_000_000,
    "B": 1_000_000_000,
    "G": 1_000_000_000,
    "T": 1_000_000_000_000,
}
_SHORTHAND_RE = re.compile(
    r"^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([KMBTG])?$",
)
_ANCHOR_RE = re.compile(r"@([^\s@]+)#L(\d+)(?:-L(\d+))?", re.IGNORECASE)
_TODO_RE = re.compile(
    r"(?im)^\s*(?:[-*]|\d+[.)])?\s*(?:TODO|FIXME|UNHANDLED|GAP|MISSING)\b[:\s-]*(.+)$"
)
_TASK_RE = re.compile(r"(?m)^\s*(?:[-*]|\d+[.)])\s+(\S.+)$")

_context_state: dict[str, Any] = {"packed_window_tokens": DEFAULT_CONTEXT_WINDOW}


@dataclass(frozen=True)
class PackedContext:
    text: str
    configured: int
    packed: int
    available: int
    hit_count: int
    note_count: int
    pin_count: int
    hits: list[Hit] = field(default_factory=list)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


def normalize_window(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("context window must be a positive integer")
    if value < MIN_CONTEXT_WINDOW:
        raise ValueError("context window must be at least 1")
    return value


def working_window(
    configured: int | None = None,
    model_capacity: int | None = None,
    *,
    enforce_cap: bool = False,
) -> int:
    """Operator budget as-is. Pass enforce_cap=True to trim to 90% of model capacity."""
    raw = DEFAULT_CONTEXT_WINDOW if configured is None else int(configured)
    window = normalize_window(raw)
    if not enforce_cap or model_capacity is None:
        return window
    try:
        cap = int(model_capacity)
    except (TypeError, ValueError):
        return window
    if cap <= 0:
        return window
    ceiling = max(MIN_CONTEXT_WINDOW, int(cap * CONTEXT_TRIM_RATIO))
    return min(window, ceiling)


def turn_pack_window(
    configured: int | None = None,
    model_capacity: int | None = None,
) -> int:
    """Cap a single-turn SSOT pack so the 80k token brake still has headroom."""
    window = working_window(configured, model_capacity, enforce_cap=bool(model_capacity))
    ceiling = min(SSOT_PACK_TOKEN_CAP, max(4_000, MAX_TOKENS_PER_TURN - SSOT_PACK_HEADROOM))
    return max(MIN_CONTEXT_WINDOW, min(window, ceiling))


def clip_to_token_budget(text: str, budget: int) -> str:
    blob = text or ""
    limit = max(MIN_CONTEXT_WINDOW, int(budget))
    if estimate_tokens(blob) <= limit:
        return blob
    char_budget = max(4, limit * CHARS_PER_TOKEN)
    clipped = blob[:char_budget].rstrip()
    return clipped + "\n… (context clipped at token budget)"


def _parse_tokens(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("context window must be a positive integer")
    if isinstance(value, int):
        return normalize_window(value)
    if isinstance(value, float):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("context window must be a positive number")
        return normalize_window(int(value))
    text = str(value).strip().upper().replace(",", "").replace("_", "").replace(" ", "")
    if not text:
        raise ValueError("context window is empty")
    match = _SHORTHAND_RE.match(text)
    if match:
        num, unit = match.groups()
        amount = float(num)
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("context window must be a positive number")
        if unit in _MULTIPLIER_MAP:
            amount *= _MULTIPLIER_MAP[unit]
        return normalize_window(int(amount))
    try:
        amount = float(text)
    except ValueError as exc:
        raise ValueError(f"invalid context window '{value}'") from exc
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("context window must be a positive number")
    return normalize_window(int(amount))


def parse_context_shorthand(value: str | int | float) -> int:
    """Parse raw numbers or shorthand (e.g. '1M', '100B', '1T') into token counts."""
    try:
        return _parse_tokens(value)
    except ValueError:
        return DEFAULT_CONTEXT_WINDOW


def parse_window(raw: object) -> int:
    """Parse a window size from int, float, or strings like 1t / 1M / 1e12. No upper cap."""
    return _parse_tokens(raw)


def set_context_window(
    raw_value: str | int | float,
    enforce_cap: bool = False,
    model_capacity: int | None = None,
) -> int:
    """Set an unbounded SSOT context budget. 90% model trim only if enforce_cap is True."""
    tokens = parse_window(raw_value)
    if enforce_cap:
        tokens = working_window(tokens, model_capacity, enforce_cap=True)
    _context_state["packed_window_tokens"] = tokens
    return tokens


def get_context_window() -> int:
    try:
        return normalize_window(int(_context_state.get("packed_window_tokens") or DEFAULT_CONTEXT_WINDOW))
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_WINDOW


def format_window(value: int) -> str:
    n = int(value)
    if n >= 1_000_000_000_000 and n % 1_000_000_000_000 == 0:
        return f"{n // 1_000_000_000_000}T"
    if n >= 1_000_000_000_000:
        return f"{n / 1_000_000_000_000:g}T"
    if n >= 1_000_000_000 and n % 1_000_000_000 == 0:
        return f"{n // 1_000_000_000}B"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:g}B"
    if n >= 1_000_000 and n % 1_000_000 == 0:
        return f"{n // 1_000_000}M"
    if n >= 1_000_000:
        return f"{n / 1_000_000:g}M"
    if n >= 1_000 and n % 1_000 == 0:
        return f"{n // 1_000}K"
    return f"{n:,}"


def retrieval_k(window: int, chunk_count: int) -> int:
    if chunk_count <= 0:
        return 0
    want = max(4, int(window) // TOKENS_PER_CHUNK_HINT)
    return min(chunk_count, want)


def adaptive_relevance_threshold(chunk_count: int) -> float:
    """Scale retrieval cutoff with index volume so small indexes are not starved."""
    n = max(0, int(chunk_count))
    if n <= 4:
        return 0.0
    if n <= 16:
        return 0.18
    return min(0.55, 0.18 + math.log10(n / 16.0) * 0.22)


def bidirectional_gap_scan(text: str | None = None, *, max_items: int = 24) -> list[str]:
    """Read a ledger from head and tail so specialists can claim unhandled tasks."""
    blob = text
    if blob is None:
        try:
            from robber.handoff_ledger import get_handoff_ledger

            blob = get_handoff_ledger().export_text()
        except Exception:
            blob = ""
    lines = (blob or "").splitlines()
    if not lines:
        return []
    head = lines[:48]
    tail = lines[-48:] if len(lines) > 48 else []
    found: list[str] = []
    seen: set[str] = set()

    def take(row: str) -> None:
        text_row = row.strip()
        if not text_row:
            return
        todo = _TODO_RE.match(text_row)
        task = _TASK_RE.match(text_row)
        item = ""
        if todo:
            item = todo.group(1).strip()
        elif text_row.upper().startswith("[UNHANDLED]"):
            item = text_row.split("]", 1)[-1].strip()
        elif task and any(flag in text_row.upper() for flag in ("TODO", "FIXME", "UNHANDLED", "GAP", "MISSING")):
            item = task.group(1).strip()
        if not item:
            return
        key = item.lower()
        if key in seen:
            return
        seen.add(key)
        found.append(item)

    for row in head:
        take(row)
        if len(found) >= max_items:
            return found
    for row in reversed(tail):
        take(row)
        if len(found) >= max_items:
            break
    return found


def stream_document(
    path: Path | str,
    *,
    lines_per_chunk: int = PACING_MICRO_LINES,
    max_chunks: int = PACING_MAX_CHUNKS,
    window: int = MMAP_WINDOW_BYTES,
):
    """Yield mmap line chunks for 100-200+ page files without loading the corpus."""
    target = Path(path)
    streamer = MemoryMappedChunkStreamer(target, window=window)
    yield from streamer.iter_line_chunks(
        target.name,
        lines_per_chunk=lines_per_chunk,
        max_chunks=max_chunks,
    )


def extract_anchors(text: str, extra: list[str] | None = None) -> list[tuple[str, int, int]]:
    found: list[tuple[str, int, int]] = []
    seen: set[tuple[str, int, int]] = set()
    blobs = list(extra or [])
    if text:
        blobs.append(text)
    for blob in blobs:
        for match in _ANCHOR_RE.finditer(blob):
            path = match.group(1).strip()
            start = int(match.group(2))
            end = int(match.group(3) or match.group(2))
            if end < start:
                start, end = end, start
            key = (path, start, end)
            if key in seen:
                continue
            seen.add(key)
            found.append(key)
    return found


def _read_jailed_range(rel: str, start: int, end: int) -> str | None:
    try:
        path = resolve_inside(workspace_root(), rel)
    except (JailError, OSError, ValueError):
        return None
    if not path.is_file():
        return None
    if path.name in SECRET_NAMES:
        return None
    reason = skip_reason(path)
    if reason in {"secret", "lock", "binary", "dump"}:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    lo = max(1, start)
    hi = max(lo, end)
    if size >= 32_768:
        text = MemoryMappedChunkStreamer(path).extract_lines(lo, hi)
        return text or None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    sliced = lines[lo - 1 : hi]
    if not sliced:
        return None
    return "\n".join(sliced)


def _append_block(
    parts: list[str],
    used: int,
    window: int,
    heading: str,
    body: str = "",
) -> tuple[int, bool]:
    heading = heading.strip()
    body = body.rstrip()
    if not heading and not body:
        return used, False
    block = heading if not body else f"{heading}\n{body}"
    block = f"{block}\n"
    cost = estimate_tokens(block)
    if used > 0 and used + cost > window:
        return used, False
    if used == 0 and cost > window:
        char_budget = max(4, window * CHARS_PER_TOKEN)
        parts.append(block[:char_budget])
        return window, True
    parts.append(block)
    return used + cost, True


def pack_ssot_context(
    query: str,
    *,
    window: int | None = None,
    extra_directives: str = "",
    extra_anchors: list[str] | None = None,
    model_capacity: int | None = None,
) -> PackedContext:
    """Fill a unified memory blob up to the per-turn pack cap, not the 1B operator window."""
    operator = working_window(
        window if window is not None else DEFAULT_CONTEXT_WINDOW,
        model_capacity,
        enforce_cap=False,
    )
    configured = turn_pack_window(operator, model_capacity)
    index = get_index()
    parts: list[str] = []
    used = 0
    pin_count = 0
    note_count = 0
    hits: list[Hit] = []

    header = (
        f"SSOT unified context · pack {format_window(configured)} "
        f"({configured} tokens) · operator {format_window(operator)} · "
        f"index {index.stats['files']} files / {index.stats['chunks']} chunks "
        f"(cap {MAX_FILES} files)"
    )
    used, _ = _append_block(parts, used, configured, header)

    for path, start, end in extract_anchors(query, extra_anchors):
        body = _read_jailed_range(path, start, end)
        if body is None:
            continue
        used, ok = _append_block(
            parts,
            used,
            configured,
            f"[pin] @{path}#L{start}-L{end}",
            body,
        )
        if not ok:
            break
        pin_count += 1
        hits.append(
            Hit(
                path=path,
                kind="pin",
                start=start,
                end=end,
                score=1.0,
                preview=body.strip().splitlines()[0][:80] if body.strip() else "",
            )
        )

    ranked = index.rank_chunks(query or "workspace architecture", k=retrieval_k(configured, len(index.chunks)))
    pinned = {(hit.path, hit.start, hit.end) for hit in hits}

    threshold = adaptive_relevance_threshold(len(index.chunks))
    accepted = [(score, chunk) for score, chunk in ranked if score >= threshold]
    if not accepted and ranked:
        accepted = ranked[: min(8, len(ranked))]

    for score, chunk in accepted:
        key = (chunk.path, chunk.start, chunk.end)
        if key in pinned:
            continue
        if Path(chunk.path).name in SECRET_NAMES:
            continue
        used, ok = _append_block(
            parts,
            used,
            configured,
            f"[hit] {chunk.path}:{chunk.start}-{chunk.end} [{chunk.kind} · {round(float(score), 3)}]",
            chunk.text,
        )
        if not ok:
            break
        preview = chunk.text.strip().splitlines()[0][:80] if chunk.text.strip() else ""
        hits.append(
            Hit(
                path=chunk.path,
                kind=chunk.kind,
                start=chunk.start,
                end=chunk.end,
                score=round(float(score), 3),
                preview=preview,
            )
        )

    if extra_directives and extra_directives.strip():
        inject = clip_to_token_budget(extra_directives.strip(), SSOT_INJECT_TOKEN_CAP)
        used, ok = _append_block(
            parts,
            used,
            configured,
            "[inject] live self-instruction",
            inject,
        )
        if ok:
            note_count += 1

    for note in load_notes():
        key = str(note.get("key", "")).strip() or "note"
        value = str(note.get("value", "")).strip()
        category = str(note.get("category", "user")).strip() or "user"
        used, ok = _append_block(
            parts,
            used,
            configured,
            f"[note] {category}:{key}",
            value,
        )
        if not ok:
            break
        note_count += 1

    for gap in bidirectional_gap_scan():
        used, ok = _append_block(
            parts,
            used,
            configured,
            "[gap] unhandled specialist task",
            gap,
        )
        if not ok:
            break

    text = "\n".join(part.rstrip() for part in parts if part.strip()).strip()
    text = clip_to_token_budget(text, configured)
    packed = estimate_tokens(text) if text else 0
    if packed > configured:
        packed = configured
    return PackedContext(
        text=text,
        configured=configured,
        packed=packed,
        available=max(0, configured - packed),
        hit_count=len(hits),
        note_count=note_count,
        pin_count=pin_count,
        hits=hits,
    )


def window_public(configured: int, packed: int = 0, hit_count: int = 0) -> dict[str, object]:
    configured = int(configured)
    packed = max(0, int(packed))
    return {
        "window": configured,
        "window_label": format_window(configured),
        "packed": packed,
        "available": max(0, configured - packed),
        "hits": int(hit_count),
    }
