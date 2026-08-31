"""Workspace path jail — all file and process work stays inside one root."""

from __future__ import annotations

import os
from pathlib import Path

SKIP_DIR_NAMES = {
    "node_modules",
    "dist",
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    ".robber",
    ".cursor",
}


class JailError(ValueError):
    """Raised when a path would leave the workspace root."""


def workspace_root() -> Path:
    raw = os.environ.get("ROBBER_WORKSPACE", "").strip()
    root = Path(raw).expanduser() if raw else Path.cwd()
    return root.resolve()


def resolve_inside(root: Path, relative: str | None = None) -> Path:
    base = root.resolve()
    if not relative or relative.strip() in {".", "./"}:
        return base
    candidate = Path(relative)
    if candidate.is_absolute():
        target = candidate.resolve()
    else:
        target = (base / candidate).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise JailError(f"path escapes workspace jail: {relative}") from exc
        return target


def is_skipped_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.startswith(".")


LOCK_FILE = ".sector.lock"


def dir_is_locked(path: Path) -> bool:
    """True when this directory is a password-marked sector (skip in SSOT walks)."""
    try:
        return path.is_dir() and (path / LOCK_FILE).is_file()
    except OSError:
        return False
