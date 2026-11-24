"""Strip model chain-of-thought so only user-facing text reaches the stream."""

from __future__ import annotations

import re

TAG_BLOCK = re.compile(
    r"<(think|thinking|thought|reasoning)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
FENCE_BLOCK = re.compile(
    r"```(?:think|thinking|thought|reasoning)\s*\n.*?```",
    re.IGNORECASE | re.DOTALL,
)
RED_BLOCK = re.compile(r"◁think▷.*?◁/think▷", re.IGNORECASE | re.DOTALL)
OPEN_TAG = re.compile(r"<(think|thinking|thought|reasoning)\b[^>]*>", re.IGNORECASE)
CLOSE_TAG = {
    "think": "</think>",
    "thinking": "</thinking>",
    "thought": "</thought>",
    "reasoning": "</reasoning>",
}
RED_OPEN = "◁think▷"
RED_CLOSE = "◁/think▷"


def strip_thoughts(text: str) -> str:
    raw = text or ""
    cleaned = TAG_BLOCK.sub("", raw)
    cleaned = FENCE_BLOCK.sub("", cleaned)
    cleaned = RED_BLOCK.sub("", cleaned)
    cleaned = _drop_incomplete(cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip("\n")


def looks_like_thought_leak(text: str) -> bool:
    sample = (text or "").lstrip()
    if not sample:
        return False
    if sample.lower().startswith(("<think", "<thinking", "<thought", "◁think")):
        return True
    if re.match(r"^```(?:think|thinking|thought|reasoning)\b", sample, re.I):
        return True
    return False


def _drop_incomplete(text: str) -> str:
    match = OPEN_TAG.search(text)
    red = text.lower().find(RED_OPEN.lower())
    starts: list[int] = []
    if match:
        starts.append(match.start())
    if red >= 0:
        starts.append(red)
    if not starts:
        return text
    return text[: min(starts)].rstrip()


def _trim_partial_open(text: str) -> tuple[str, str]:
    last_lt = text.rfind("<")
    if last_lt >= 0 and ">" not in text[last_lt:]:
        tail = text[last_lt:]
        if re.match(r"<t(?:h(?:i(?:n(?:k(?:i(?:n(?:g)?)?)?)?)?)?)?$", tail, re.I) or tail == "<":
            return text[:last_lt], tail
    red = text.lower().rfind("◁")
    if red >= 0 and "▷" not in text[red:] and len(text) - red < 12:
        return text[:red], text[red:]
    ticks = text.rfind("```")
    if ticks >= 0 and "\n" not in text[ticks:] and re.match(r"```(?:th(?:i(?:nk?)?)?)?$", text[ticks:], re.I):
        return text[:ticks], text[ticks:]
    return text, ""


class ThoughtGate:
    """Streaming filter: hold tokens inside think tags, emit the rest."""

    def __init__(self) -> None:
        self._buf = ""
        self._closed = ""

    def push(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buf += chunk
        visible, held = _split_visible(self._buf)
        safe, partial = _trim_partial_open(visible)
        self._buf = partial + held
        if safe:
            self._closed += safe
        return safe

    def flush(self) -> str:
        leftover = strip_thoughts(self._buf)
        self._buf = ""
        if leftover:
            self._closed += leftover
        return leftover

    @property
    def text(self) -> str:
        return self._closed


def _split_visible(buf: str) -> tuple[str, str]:
    out: list[str] = []
    i = 0
    length = len(buf)
    while i < length:
        open_at, kind = _next_open(buf, i)
        if open_at < 0:
            out.append(buf[i:])
            return "".join(out), ""
        out.append(buf[i:open_at])
        close = _close_for(kind)
        search_from = open_at + 3 if kind == "fence" else open_at + 1
        end = buf.lower().find(close.lower(), search_from)
        if end < 0:
            return "".join(out), buf[open_at:]
        i = end + len(close)
    return "".join(out), ""


def _next_open(buf: str, start: int) -> tuple[int, str]:
    match = OPEN_TAG.search(buf, start)
    red = buf.lower().find(RED_OPEN.lower(), start)
    fence = re.search(r"```(?:think|thinking|thought|reasoning)\b", buf[start:], re.I)
    candidates: list[tuple[int, str]] = []
    if match:
        candidates.append((match.start(), match.group(1).lower()))
    if red >= 0:
        candidates.append((red, "redacted"))
    if fence:
        candidates.append((start + fence.start(), "fence"))
    if not candidates:
        return -1, ""
    candidates.sort(key=lambda item: item[0])
    return candidates[0]


def _close_for(kind: str) -> str:
    if kind == "redacted":
        return RED_CLOSE
    if kind == "fence":
        return "```"
    return CLOSE_TAG.get(kind, "</think>")
