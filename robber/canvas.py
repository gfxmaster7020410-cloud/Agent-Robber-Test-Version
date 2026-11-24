"""Reasoning-canvas payload for the dual-pane TUI / future GUI."""

from __future__ import annotations


def build_canvas(
    query: str,
    *,
    mode: str,
    engine: str,
    tool: str,
    live: bool,
    ssot: str = "—",
) -> str:
    short = query if len(query) <= 72 else f"{query[:69]}…"
    source = "cloud" if live else "local fallback"
    lines = [
        f"goal    {short}",
        f"mode    {mode}",
        f"engine  {engine}",
        f"source  {source}",
        f"tool    {tool}",
        f"ssot    {ssot}",
    ]
    if (mode or "").strip().lower() in {"plan", "review"}:
        lines.extend(
            [
                "",
                "1. Recall SSOT project context",
                "2. Run the selected tool",
                "3. Draft a surgical plan (no whole-file rewrites)",
                "4. Answer in the conversation pane",
            ]
        )
    return "\n".join(lines)
