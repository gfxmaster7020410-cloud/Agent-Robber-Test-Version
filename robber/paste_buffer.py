"""Huge paste offload — keep the prompt bar small, park the payload on disk."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from robber.jail import workspace_root

CACHE_DIR = ".agent_robber"
TMP_DIR = "tmp_context"
PASTE_NAME = "paste_buffer.txt"
WORD_LIMIT = 10_000
BYTE_LIMIT = 30 * 1024
CHARS_PER_PAGE = 3_000
CHARS_PER_TOKEN = 4
MARKER_NAME = "paste_buffer.txt"

_MARKER_RE = re.compile(r"\[Context File Attached:\s*paste_buffer\.txt", re.I)
_NULL = re.compile(r"[\x00]")


@dataclass(frozen=True)
class PasteStats:
    words: int
    bytes: int
    pages: int
    tokens: int
    path: str
    marker: str
    offloaded: bool


def sanitize_paste(raw: str) -> str:
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    return _NULL.sub("", text)


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def page_count(text: str) -> int:
    size = max(len(text or ""), 1)
    return max(1, (size + CHARS_PER_PAGE - 1) // CHARS_PER_PAGE)


def token_estimate(text: str) -> int:
    return max(1, (len(text or "") + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def is_huge_paste(text: str) -> bool:
    clean = sanitize_paste(text)
    if _MARKER_RE.search(clean) and not is_raw_huge(clean):
        return False
    return is_raw_huge(clean)


def is_raw_huge(text: str) -> bool:
    encoded = (text or "").encode("utf-8", errors="replace")
    return word_count(text) >= WORD_LIMIT or len(encoded) >= BYTE_LIMIT


def paste_dir() -> Path:
    return workspace_root() / CACHE_DIR / TMP_DIR


def paste_path() -> Path:
    return paste_dir() / PASTE_NAME


def context_marker(pages: int) -> str:
    return f"[Context File Attached: {MARKER_NAME} ({pages} pages)]"


def ingest_notice(stats: PasteStats) -> str:
    pages = stats.pages
    tokens = stats.tokens
    page_label = f"{pages}+ pages" if pages >= 100 else f"{pages} pages"
    return (
        f"✦ Initiating: Ingested large paste payload "
        f"({page_label} / {tokens:,} tokens) into active memory context."
    )


def save_paste_buffer(text: str) -> PasteStats:
    clean = sanitize_paste(text)
    target = paste_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(clean, encoding="utf-8")
    pages = page_count(clean)
    rel = f"{CACHE_DIR}/{TMP_DIR}/{PASTE_NAME}"
    return PasteStats(
        words=word_count(clean),
        bytes=len(clean.encode("utf-8")),
        pages=pages,
        tokens=token_estimate(clean),
        path=rel,
        marker=context_marker(pages),
        offloaded=True,
    )


def prepare_prompt(raw: str) -> tuple[str, str | None]:
    """Return (prompt_for_agent, optional_stream_notice). Never drops the payload."""
    text = sanitize_paste(raw)
    if not text.strip():
        return text, None
    if _MARKER_RE.search(text) and not is_raw_huge(text):
        return text, None
    if not is_raw_huge(text):
        return text, None
    stats = save_paste_buffer(text)
    prompt = (
        f"{stats.marker}\n\n"
        f"Read the attached context file at {stats.path} "
        f"({stats.words:,} words, ~{stats.tokens:,} tokens, ~{stats.pages} pages)."
    )
    return prompt, ingest_notice(stats)


def public_stats(stats: PasteStats) -> dict[str, object]:
    return {
        "offloaded": stats.offloaded,
        "words": stats.words,
        "bytes": stats.bytes,
        "pages": stats.pages,
        "tokens": stats.tokens,
        "path": stats.path,
        "marker": stats.marker,
        "notice": ingest_notice(stats),
    }
