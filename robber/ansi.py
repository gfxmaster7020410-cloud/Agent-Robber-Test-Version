"""ANSI SGR parser for terminal stream frames."""

from __future__ import annotations

import re
from typing import Any

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m|\x1b\].*?(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

SGR_COLORS = {
    30: "#4b5563",
    31: "#ef4444",
    32: "#22c55e",
    33: "#ffd400",
    34: "#38bdf8",
    35: "#a855f7",
    36: "#22d3ee",
    37: "#e8e8ed",
    90: "#8b8b9a",
    91: "#f87171",
    92: "#4ade80",
    93: "#facc15",
    94: "#60a5fa",
    95: "#c084fc",
    96: "#22d3ee",
    97: "#ffffff",
}


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def parse_ansi(text: str) -> tuple[str, list[dict[str, Any]]]:
    raw = text or ""
    segments: list[dict[str, Any]] = []
    plain: list[str] = []
    color: str | None = None
    cursor = 0
    for match in re.finditer(r"\x1b\[([0-9;]*)m", raw):
        chunk = raw[cursor : match.start()]
        if chunk:
            plain.append(chunk)
            segments.append({"text": chunk, "color": color})
        codes = [int(part) for part in match.group(1).split(";") if part.isdigit()]
        if not codes or 0 in codes:
            color = None
        else:
            for code in codes:
                mapped = SGR_COLORS.get(code)
                if mapped:
                    color = mapped
        cursor = match.end()
    rest = raw[cursor:]
    if rest:
        cleaned = strip_ansi(rest) if "\x1b" in rest else rest
        if cleaned:
            plain.append(cleaned)
            segments.append({"text": cleaned, "color": color})
    joined = "".join(plain)
    if not segments and joined:
        segments = [{"text": joined, "color": None}]
    return joined, segments
