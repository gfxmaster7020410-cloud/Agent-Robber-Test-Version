"""Adjustable SSOT unified context window — logical budget, not an in-RAM buffer.

Default is one trillion tokens. The value is persisted and used as a pack cap.
The hash-json index is still bounded (see MAX_FILES); packed tokens will usually
be far below the configured window until the workspace itself is that large.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from robber.jail import JailError, resolve_inside, workspace_root
from robber.notes import load_notes
from robber.ssot import Hit, MAX_FILES, SECRET_NAMES, get_index, skip_reason

DEFAULT_CONTEXT_WINDOW = 1_000_000_000_000
MIN_CONTEXT_WINDOW = 1
MAX_CONTEXT_WINDOW = (2**63) - 1
CHARS_PER_TOKEN = 4
# Rough tokens-per-chunk used only to decide how many ranked hits to fetch.
TOKENS_PER_CHUNK_HINT = 64

_SUFFIXES = {
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
    "g": 1_000_000_000,
    "t": 1_000_000_000_000,
}
_ANCHOR_RE = re.compile(r"@([^\s@]+)#L(\d+)(?:-L(\d+))?", re.IGNORECASE)


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
    if value > MAX_CONTEXT_WINDOW:
        raise ValueError(f"context window cannot exceed {MAX_CONTEXT_WINDOW}")
    return value


def parse_window(raw: object) -> int:
    """Parse a window size from int, float, or strings like 1t / 1e12 / 1_000."""
    if isinstance(raw, bool):
        raise ValueError("context window must be a positive integer")
    if isinstance(raw, int):
        return normalize_window(raw)
    if isinstance(raw, float):
        if not math.isfinite(raw) or raw <= 0:
            raise ValueError("context window must be a positive number")
        return normalize_window(int(raw))
    text = str(raw).strip().lower().replace(",", "").replace("_", "").replace(" ", "")
    if not text:
        raise ValueError("context window is empty")
    multiplier = 1
    if text[-1] in _SUFFIXES and len(text) > 1 and (text[-2].isdigit() or text[-2] == "."):
        multiplier = _SUFFIXES[text[-1]]
        text = text[:-1]
    try:
        amount = float(text)
    except ValueError as exc:
        raise ValueError(f"invalid context window '{raw}'") from exc
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("context window must be a positive number")
    return normalize_window(int(amount * multiplier))


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
    want = max(4, window // TOKENS_PER_CHUNK_HINT)
    return min(chunk_count, want)


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
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    lo = max(1, start)
    hi = max(lo, end)
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
) -> PackedContext:
    """Fill a unified memory blob up to `window` estimated tokens."""
    configured = normalize_window(window if window is not None else DEFAULT_CONTEXT_WINDOW)
    index = get_index()
    parts: list[str] = []
    used = 0
    pin_count = 0
    note_count = 0
    hits: list[Hit] = []

    header = (
        f"SSOT unified context · window {format_window(configured)} "
        f"({configured} tokens) · index {index.stats['files']} files / "
        f"{index.stats['chunks']} chunks (cap {MAX_FILES} files)"
    )
    used, _ = _append_block(parts, used, configured, header)

    if extra_directives and extra_directives.strip():
        used, ok = _append_block(
            parts,
            used,
            configured,
            "[inject] live self-instruction",
            extra_directives.strip(),
        )
        if ok:
            note_count += 1

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

    ranked = index.rank_chunks(query or "workspace architecture", k=retrieval_k(configured, len(index.chunks)))
    pinned = {(hit.path, hit.start, hit.end) for hit in hits}
    
    # Filter: require minimum relevance score for non-pinned chunks
    MIN_RELEVANCE = 0.5 
    
    for score, chunk in ranked:
        if score < MIN_RELEVANCE:
            continue
        key = (chunk.path, chunk.start, chunk.end)
        if key in pinned:
            continue
        
        # Anti-tampering/Noise: block dumping of sensitive internal files
        if any(part in {"robber", "gui", "server.py"} for part in Path(chunk.path).parts):
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

    text = "\n".join(part.rstrip() for part in parts if part.strip()).strip()
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
