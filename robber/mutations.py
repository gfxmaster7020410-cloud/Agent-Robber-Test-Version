"""Session file-mutation stack: apply, undo, redo, accept, revert."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robber.jail import JailError, display_path, resolve_inside, workspace_root
from robber.security.perimeter import validate_file_mutation
from robber.patch import (
    BLOCKED_NAMES,
    DIFF_FENCE,
    create_workspace_snapshot,
    extract_diff,
    restore_workspace_snapshot,
)

logger = logging.getLogger(__name__)

JSON_FENCE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
MAX_CARD_CHARS = 12_000

_MANAGER: FileMutationManager | None = None


class TurnAborted(Exception):
    """Raised to stop a turn and roll the workspace back."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def get_mutations() -> FileMutationManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = FileMutationManager()
    return _MANAGER


def bind_mutations(manager: FileMutationManager | None) -> FileMutationManager:
    global _MANAGER
    _MANAGER = manager if manager is not None else FileMutationManager()
    return _MANAGER


@dataclass
class Mutation:
    id: str
    path: str
    action: str
    before: str | None
    after: str | None
    status: str = "pending"
    timestamp: float = field(default_factory=time.time)

    def marker(self) -> str:
        if self.action == "create":
            return "+"
        if self.action == "delete":
            return "-"
        return "~"

    def line_delta(self) -> tuple[int, int]:
        after_lines = 0 if not self.after else len(self.after.splitlines())
        before_lines = 0 if not self.before else len(self.before.splitlines())
        if self.action == "create":
            return after_lines, 0
        if self.action == "delete":
            return 0, before_lines
        return max(0, after_lines - before_lines), max(0, before_lines - after_lines)

    def detail(self) -> str:
        added, removed = self.line_delta()
        return f"(+{added}, -{removed})"

    def line(self) -> str:
        return f"[{self.marker()}] {self.path} {self.detail()}"

    def public(self, *, bodies: bool = False) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "path": self.path,
            "action": self.action,
            "status": self.status,
            "marker": self.marker(),
            "detail": self.detail(),
            "line": self.line(),
        }
        if bodies:
            row["before"] = _clip(self.before or "")
            row["after"] = _clip(self.after or "")
            row["language"] = language_for(self.path)
        return row

    def card(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "path": self.path,
            "language": language_for(self.path),
            "before": _clip(self.before or ""),
            "after": _clip(self.after or ""),
        }


class FileMutationManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or workspace_root()).resolve()
        self._undo: list[Mutation] = []
        self._redo: list[Mutation] = []
        self._by_path: dict[str, Mutation] = {}
        self._origin: dict[str, str | None] = {}
        self._turn: list[Mutation] = []
        self._turn_started: float | None = None
        self._snapshot: dict[str, Any] | None = None

    def begin_turn(self) -> None:
        self._turn = []
        self._turn_started = time.monotonic()
        try:
            self._snapshot = create_workspace_snapshot(self.root)
        except Exception:
            logger.exception("turn snapshot skipped")
            self._snapshot = None

    def ensure_snapshot(self) -> None:
        if self._snapshot is not None:
            return
        try:
            self._snapshot = create_workspace_snapshot(self.root)
        except Exception:
            logger.exception("turn snapshot skipped")
            self._snapshot = None

    def commit_turn(self) -> None:
        self._snapshot = None

    def rollback_turn(self) -> str:
        restore_workspace_snapshot(self.root, self._snapshot)
        note = self.revert_all()
        self._snapshot = None
        self._turn_started = None
        if note.startswith("Nothing"):
            return "Rolled back turn snapshot"
        return f"Rolled back turn snapshot · {note}"

    def elapsed_ms(self) -> int:
        if self._turn_started is None:
            return 0
        return max(0, int((time.monotonic() - self._turn_started) * 1000))

    def public(self) -> dict[str, Any]:
        files = [item.public() for item in self._by_path.values()]
        pending = sum(1 for item in self._by_path.values() if item.status == "pending")
        return {
            "files": files,
            "pending": pending,
            "can_undo": bool(self._undo),
            "can_redo": bool(self._redo),
        }

    def turn_cards(self) -> list[dict[str, Any]]:
        return [item.card() for item in self._turn]

    def write_file(self, relative: str, content: str, *, action: str | None = None) -> Mutation:
        target = self._jailed(relative)
        existed = target.is_file()
        before = target.read_text(encoding="utf-8", errors="replace") if existed else None
        rel = self._rel(target)
        if before == content:
            current = self._by_path.get(rel)
            if current is not None:
                return current
            return Mutation(
                id="noop",
                path=rel,
                action=action or ("edit" if existed else "create"),
                before=before,
                after=content,
                status="pending",
            )
        resolved = action or ("edit" if existed else "create")
        if not existed:
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return self._record(self._rel(target), resolved, before, content)

    def delete_file(self, relative: str) -> Mutation:
        target = self._jailed(relative)
        if not target.is_file():
            raise JailError(f"not a file: {relative}")
        before = target.read_text(encoding="utf-8", errors="replace")
        target.unlink()
        return self._record(self._rel(target), "delete", before, None)

    def undo(self) -> str:
        if not self._undo:
            return "Nothing to undo"
        mutation = self._undo.pop()
        self._restore(mutation.path, mutation.before)
        mutation.status = "pending"
        self._redo.append(mutation)
        self._refresh_path(mutation.path)
        return f"Undid {mutation.action} {mutation.path}"

    def redo(self) -> str:
        if not self._redo:
            return "Nothing to redo"
        mutation = self._redo.pop()
        self._restore(mutation.path, mutation.after)
        mutation.status = "pending"
        self._undo.append(mutation)
        self._by_path[mutation.path] = mutation
        return f"Redid {mutation.action} {mutation.path}"

    def accept_all(self) -> str:
        pending = [item for item in self._by_path.values() if item.status == "pending"]
        if not pending and not self._by_path:
            return "Nothing pending to accept"
        for item in self._by_path.values():
            item.status = "accepted"
            self._origin[item.path] = item.after
        count = len(self._by_path)
        return f"Accepted {count} file mutation(s)"

    def revert_all(self) -> str:
        if not self._origin and not self._by_path:
            return "Nothing to revert"
        paths = list(dict.fromkeys([*self._origin.keys(), *self._by_path.keys()]))
        for path in paths:
            original = self._origin.get(path, None)
            # If we never captured origin, treat as "no file".
            if path in self._origin:
                original = self._origin[path]
            else:
                first = self._by_path.get(path)
                original = first.before if first is not None else None
            self._restore(path, original)
        count = len(paths)
        self._undo.clear()
        self._redo.clear()
        self._by_path.clear()
        self._origin.clear()
        self._turn.clear()
        return f"Reverted {count} file mutation(s)"

    def apply_from_text(self, text: str) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []
        seen: set[str] = set()
        pending_cards: list[dict[str, Any]] = []
        for blob in _json_blobs(text):
            card = _as_file_card(blob)
            if card is None:
                continue
            path = card["path"]
            if path in seen:
                continue
            seen.add(path)
            pending_cards.append(card)
        diffs = _iter_diffs(text)
        if len(pending_cards) + len(diffs) >= 2:
            self.ensure_snapshot()
        for card in pending_cards:
            path = str(card["path"])
            action = str(card.get("action") or "edit")
            if action == "delete":
                mutation = self.delete_file(path)
            else:
                body = str(card.get("after") or card.get("content") or "")
                mutation = self.write_file(path, body, action=action if action in {"create", "edit"} else None)
            applied.append(mutation.card())
        for diff in diffs:
            try:
                mutation = self.apply_diff(diff)
            except (JailError, OSError, ValueError):
                continue
            if mutation.path in seen:
                continue
            seen.add(mutation.path)
            applied.append(mutation.card())
        return applied

    def apply_diff(self, diff_text: str) -> Mutation:
        from robber.tools.patch_applier import JailedPatchApplicator

        result = JailedPatchApplicator(self.root).apply_sync(diff_text)
        if not result.ok or not result.path:
            raise JailError(result.traceback or result.message)
        current = self._by_path.get(result.path.replace("\\", "/"))
        if current is None:
            raise JailError(f"patch applied but not recorded: {result.path}")
        return current

    def _record(self, relative: str, action: str, before: str | None, after: str | None) -> Mutation:
        path = relative.replace("\\", "/")
        if path not in self._origin:
            self._origin[path] = before
        mutation = Mutation(
            id=uuid.uuid4().hex[:12],
            path=path,
            action=action,
            before=before,
            after=after,
            status="pending",
        )
        self._undo.append(mutation)
        self._redo.clear()
        self._by_path[path] = mutation
        self._turn.append(mutation)
        return mutation

    def _refresh_path(self, path: str) -> None:
        for item in reversed(self._undo):
            if item.path == path:
                self._by_path[path] = item
                return
        self._by_path.pop(path, None)

    def _restore(self, relative: str, content: str | None) -> None:
        target = self._jailed(relative)
        if content is None:
            if target.is_file():
                target.unlink()
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def _jailed(self, relative: str) -> Path:
        text = (relative or "").strip().replace("\\", "/")
        if not text or text in {".", "./"}:
            raise JailError("empty mutation target")
        if Path(text).name in BLOCKED_NAMES:
            raise JailError(f"refusing to mutate protected file: {text}")
        target = resolve_inside(self.root, text)
        validate_file_mutation(str(target), is_dev_mode=False)
        return target

    def _rel(self, target: Path) -> str:
        return display_path(target, self.root)


def language_for(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    return {
        "py": "python",
        "ts": "typescript",
        "tsx": "typescript",
        "js": "javascript",
        "jsx": "javascript",
        "json": "json",
        "md": "markdown",
        "css": "css",
        "html": "html",
        "htm": "html",
        "rs": "rust",
        "go": "go",
        "sql": "sql",
    }.get(suffix, "plaintext")


def _clip(text: str, limit: int = MAX_CARD_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n… (truncated)"


def _json_blobs(text: str) -> list[Any]:
    blobs: list[Any] = []
    for match in JSON_FENCE.finditer(text or ""):
        try:
            blobs.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    stripped = (text or "").strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            blobs.append(json.loads(stripped))
        except json.JSONDecodeError:
            pass
    return blobs


def _as_file_card(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        for item in value:
            card = _as_file_card(item)
            if card is not None:
                return card
        return None
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    if not any(key in value for key in ("after", "content", "before", "action")):
        return None
    return value


def _iter_diffs(text: str) -> list[str]:
    diffs = [match.group(1).strip() for match in DIFF_FENCE.finditer(text or "") if match.group(1).strip()]
    if diffs:
        return diffs
    extracted = extract_diff(text or "")
    return [extracted] if extracted else []
