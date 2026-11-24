"""AST-aware code analysis for a single jailed file."""

from __future__ import annotations

from pathlib import Path

from robber.core.ast_parser import ASTCodeParser
from robber.jail import JailError, resolve_inside, workspace_root
from robber.tools.workspace import clip, pick_read_target


def analyze_file(query: str, *, root: Path | None = None) -> str:
    base = (root or workspace_root()).resolve()
    relative = pick_read_target(query)
    if relative is None:
        raise JailError("No source file path found for analysis")
    target = resolve_inside(base, relative)
    if not target.is_file():
        raise JailError(f"not a file: {relative}")
    symbols = ASTCodeParser().extract_symbols(str(target))
    rel = target.as_posix()
    try:
        rel = target.relative_to(base).as_posix()
    except ValueError:
        pass
    if not symbols:
        return clip(f"AST analysis {rel}: no symbols")
    lines = [f"AST analysis {rel} ({len(symbols)} symbols)"]
    for item in symbols[:40]:
        name = item.get("name") or "?"
        kind = item.get("type") or "symbol"
        start = item.get("start_line")
        end = item.get("end_line")
        lines.append(f"- {kind} {name} L{start}-L{end}")
    return clip("\n".join(lines))
