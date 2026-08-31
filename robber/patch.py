"""Jailed unified-diff apply with a post-apply Python syntax check."""

from __future__ import annotations

import re
from pathlib import Path

from robber.jail import JailError, resolve_inside

BLOCKED_NAMES = {".env", ".env.local", "keys.vault", "credentials.json"}
FILE_HEADER = re.compile(r"^\+\+\+ [ab]/(.+)$", re.MULTILINE)
DIFF_FENCE = re.compile(r"```diff\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def extract_diff(query: str) -> str | None:
    fenced = DIFF_FENCE.search(query)
    if fenced:
        return fenced.group(1).strip()
    stripped = query.lstrip()
    if stripped.startswith("--- ") and "+++" in stripped and "@@" in stripped:
        return stripped
    return None


def _target_from_diff(diff: str) -> str | None:
    match = FILE_HEADER.search(diff)
    return match.group(1).strip() if match else None


def apply_unified_diff(root: Path, diff: str, *, relative: str | None = None) -> str:
    path = relative or _target_from_diff(diff)
    if not path:
        raise JailError("diff has no +++ target path")
    if Path(path).name in BLOCKED_NAMES:
        raise JailError(f"refusing to patch protected file: {path}")
    target = resolve_inside(root, path)
    if not target.is_file():
        raise JailError(f"not a file: {path}")
    original = target.read_text(encoding="utf-8")
    updated = _apply_hunks(original, diff)
    if target.suffix.lower() == ".py":
        compile(updated, str(target), "exec")
    target.write_text(updated, encoding="utf-8")
    return f"patched {target.relative_to(root.resolve())}"


def _apply_hunks(original: str, diff: str) -> str:
    lines = original.splitlines(keepends=True)
    hunks = re.split(r"(?=^@@ )", diff, flags=re.MULTILINE)
    cursor_adjust = 0
    applied = False
    for hunk in hunks:
        header = HUNK_HEADER.match(hunk)
        if not header:
            continue
        applied = True
        old_start = int(header.group(1)) - 1 + cursor_adjust
        body = hunk.splitlines(keepends=True)[1:]
        new_block: list[str] = []
        old_count = 0
        for raw in body:
            if raw.startswith("\\"):
                continue
            if raw.startswith("+") and not raw.startswith("+++"):
                new_block.append(raw[1:])
            elif raw.startswith("-") and not raw.startswith("---"):
                old_count += 1
            elif raw.startswith(" "):
                new_block.append(raw[1:])
                old_count += 1
        end = old_start + old_count
        if old_start < 0 or end > len(lines):
            raise JailError("hunk is outside the file")
        lines[old_start:end] = new_block
        cursor_adjust += len(new_block) - old_count
    if not applied:
        raise JailError("diff contained no hunks")
    return "".join(lines)
