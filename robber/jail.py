"""Path resolution for Agent Robber tools.

Relative paths are anchored at the workspace cwd. Absolute, UNC, and ~ paths
are used as-is across drives. This is not a sandbox.
"""

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
    """Raised for invalid targets (missing file, empty path), not for leaving cwd."""


def workspace_root() -> Path:
    raw = os.environ.get("ROBBER_WORKSPACE", "").strip()
    root = Path(raw).expanduser() if raw else Path.cwd()
    return root.resolve()


def resolve_inside(root: Path, relative: str | None = None) -> Path:
    """Resolve a path. Absolute / UNC / ~ targets are not confined to `root`."""
    base = root.resolve()
    if not relative or relative.strip() in {".", "./"}:
        return base
    raw = relative.strip().strip('"').strip("'")
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (base / candidate).resolve()


def display_path(target: Path, root: Path | None = None) -> str:
    resolved = target.resolve()
    if root is not None:
        try:
            return str(resolved.relative_to(root.resolve())).replace("\\", "/")
        except ValueError:
            pass
    return str(resolved)


def is_skipped_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.startswith(".")


LOCK_FILE = ".sector.lock"


def dir_is_locked(path: Path) -> bool:
    """True when this directory is a password-marked sector (skip in SSOT walks)."""
    try:
        return path.is_dir() and (path / LOCK_FILE).is_file()
    except OSError:
        return False
