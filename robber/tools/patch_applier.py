"""Jailed unified-diff applicator with pre-flight Python compile checks."""

from __future__ import annotations

import ast
import asyncio
import re
import traceback
from dataclasses import dataclass
from pathlib import Path

from robber.jail import JailError, display_path, resolve_inside, workspace_root
from robber.security.perimeter import validate_file_mutation
from robber.patch import BLOCKED_NAMES, _apply_hunks, extract_diff

PLUS_HEADER = re.compile(r"^\+\+\+ (?:[ab]/)?(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class PatchResult:
    ok: bool
    path: str | None
    message: str
    traceback: str = ""


class JailedPatchApplicator:
    """Parse a unified diff, compile Python, then write. Paths may be anywhere on disk."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or workspace_root()).resolve()

    def parse_target(self, diff: str) -> str:
        match = PLUS_HEADER.search(diff)
        if not match:
            raise JailError("diff has no +++ target path")
        return match.group(1).strip()

    def assert_jailed(self, relative: str) -> Path:
        text = relative.strip().replace("\\", "/")
        if not text or text in {".", "./"}:
            raise JailError("empty patch target")
        if text in {"/dev/null", "nul"}:
            raise JailError("refusing to patch /dev/null")
        if Path(text).name in BLOCKED_NAMES:
            raise JailError(f"refusing to patch protected file: {text}")
        target = resolve_inside(self.root, text)
        validate_file_mutation(str(target), is_dev_mode=False)
        return target

    def verify_python(self, source: str, filename: str) -> None:
        try:
            ast.parse(source, filename=filename)
            compile(source, filename, "exec")
        except SyntaxError as exc:
            raise SyntaxError("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))) from exc

    def apply_sync(self, diff_text: str) -> PatchResult:
        try:
            diff = extract_diff(diff_text) or diff_text.strip()
            if "@@" not in diff or "+++" not in diff:
                raise JailError("not a unified diff")
            relative = self.parse_target(diff)
            target = self.assert_jailed(relative)
            if not target.is_file():
                raise JailError(f"not a file: {relative}")
            original = target.read_text(encoding="utf-8")
            updated = _apply_hunks(original, diff)
            if target.suffix.lower() == ".py":
                self.verify_python(updated, str(target))
            rel = display_path(target, self.root)
            from robber.mutations import get_mutations

            get_mutations().write_file(rel, updated, action="edit")
            return PatchResult(True, rel, f"patched {rel}")
        except SyntaxError as exc:
            tb = str(exc)
            return PatchResult(False, None, "python syntax check failed", tb)
        except (JailError, OSError, ValueError) as exc:
            return PatchResult(False, None, str(exc))

    async def apply(self, diff_text: str) -> PatchResult:
        return await asyncio.to_thread(self.apply_sync, diff_text)
