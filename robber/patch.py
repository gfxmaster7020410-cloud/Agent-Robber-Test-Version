"""Jailed unified-diff apply with a post-apply Python syntax check."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from robber.jail import JailError, display_path, resolve_inside

BLOCKED_NAMES = {".env", ".env.local", "keys.vault", "credentials.json"}
BINARY_SUFFIXES = {
    ".7z",
    ".bin",
    ".bmp",
    ".class",
    ".db",
    ".dll",
    ".eot",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".mp3",
    ".mp4",
    ".o",
    ".obj",
    ".ogg",
    ".otf",
    ".pdb",
    ".pdf",
    ".png",
    ".pyc",
    ".pyo",
    ".rar",
    ".so",
    ".sqlite",
    ".sqlite3",
    ".ttf",
    ".wasm",
    ".wav",
    ".webm",
    ".webp",
    ".whl",
    ".woff",
    ".woff2",
    ".zip",
}
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
    original = target.read_text(encoding="utf-8", errors="replace")
    updated = _apply_hunks(original, diff)
    if target.suffix.lower() == ".py":
        compile(updated, str(target), "exec")
    target.write_text(updated, encoding="utf-8")
    return f"patched {display_path(target, root)}"


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


def _git(root: Path, *args: str, timeout: float = 8.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return int(proc.returncode or 0), out


def create_workspace_snapshot(root: Path) -> dict[str, Any]:
    """Capture a pre-batch git stash object plus untracked file bodies."""
    snap: dict[str, Any] = {"oid": "", "untracked": {}}
    code, oid = _git(root, "stash", "create")
    if code == 0 and oid and re.fullmatch(r"[0-9a-f]{7,40}", oid.split()[0] if oid else ""):
        snap["oid"] = oid.split()[0]
    _code, listing = _git(root, "ls-files", "--others", "--exclude-standard")
    untracked: dict[str, str] = {}
    for rel in listing.splitlines():
        rel = rel.strip().replace("\\", "/")
        if not rel or rel.startswith(".robber/") or rel.startswith(".git/"):
            continue
        if Path(rel).name in BLOCKED_NAMES:
            continue
        path = root / rel
        if not path.is_file():
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4096]:
            continue
        try:
            untracked[rel] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
    snap["untracked"] = untracked
    return snap


def restore_workspace_snapshot(root: Path, snap: dict[str, Any] | None) -> bool:
    """Restore tracked files from the stash oid and rewrite saved untracked files."""
    if not snap:
        return False
    restored = False
    oid = str(snap.get("oid") or "").strip()
    if oid:
        code, _out = _git(root, "checkout", oid, "--", ".")
        restored = code == 0
    untracked = snap.get("untracked") if isinstance(snap.get("untracked"), dict) else {}
    for rel, content in untracked.items():
        try:
            target = resolve_inside(root, str(rel))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
            restored = True
        except (JailError, OSError):
            continue
    return restored
