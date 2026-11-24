"""Self-healing diagnostic loop: parse, hypothesize, patch, verify, retry."""

from __future__ import annotations

import ast
import asyncio
import difflib
import logging
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from robber.config import HEAL_MAX_ATTEMPTS
from robber.diagnostics.error_parser import (
    BLOCK_START,
    ParsedError,
    looks_like_missing_colon,
    parse_error_log,
)
from robber.jail import workspace_root
from robber.patch import extract_diff
from robber.ssot_db import default_ssot_path
from robber.state_manager import StateManager
from robber.tools.patch_applier import JailedPatchApplicator

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = HEAL_MAX_ATTEMPTS
T = Callable[[], Awaitable[Any]]

_HEALER: SelfHealingEngine | None = None


def get_healer() -> SelfHealingEngine | None:
    return _HEALER


def bind_healer(engine: SelfHealingEngine | None) -> None:
    global _HEALER
    _HEALER = engine


class SelfHealingEngine:
    """Catch compiler/tool failures, apply an isolated repair, and re-verify."""

    def __init__(
        self,
        hub: Any = None,
        applicator: JailedPatchApplicator | None = None,
        *,
        root: Path | str | None = None,
        manager: StateManager | None = None,
    ) -> None:
        self.hub = hub
        self.root = Path(root).resolve() if root else workspace_root()
        self.applicator = applicator or JailedPatchApplicator(self.root)
        self.manager = manager or StateManager(self.root)
        self.last_repair: dict[str, Any] = {}

    async def emit(self, phase: str, message: str, **meta: Any) -> None:
        content = f"[SELF-HEAL] {message}"
        payload = {
            "type": "self_heal",
            "content": content,
            "metadata": {"phase": phase, **meta},
            "timestamp": time.time(),
        }
        if self.hub is not None:
            try:
                await self.hub.broadcast(payload)
            except Exception:
                logger.debug("self-heal broadcast skipped")
        logger.info(content)

    async def run_with_heal(self, name: str, factory: T, *, attempts: int = MAX_ATTEMPTS) -> Any:
        last: BaseException | None = None
        for step in range(1, max(1, int(attempts)) + 1):
            try:
                return await factory()
            except asyncio.CancelledError:
                raise
            except UnicodeDecodeError:
                raise
            except Exception as exc:
                last = exc
                await self.emit("detected", "Error detected", task=name, attempt=step)
                recovered = await self.heal_exception(exc, task_name=name)
                if not recovered or step == attempts:
                    await self.emit(
                        "manual",
                        "Self-healing exhausted - waiting for operator",
                        task=name,
                        attempt=step,
                    )
                    break
                await self.emit("retry", "Re-executing failed task", task=name, attempt=step)
        assert last is not None
        raise last

    async def heal_from_stderr(self, stderr: str, *, task_name: str = "command") -> bool:
        fake = RuntimeError(stderr[:500] or "command failed")
        fake._heal_stderr = stderr  # type: ignore[attr-defined]
        return await self.heal_exception(fake, task_name=task_name, stderr=stderr)

    async def heal_exception(
        self,
        exc: BaseException,
        *,
        task_name: str = "task",
        stderr: str | None = None,
    ) -> bool:
        trace = stderr or "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        extra = getattr(exc, "_heal_stderr", "")
        if extra and extra not in trace:
            trace = f"{trace}\n{extra}"
        return await asyncio.to_thread(self.repair_sync, trace, task_name)

    def repair_source(self, path: Path | str, attempts: int = MAX_ATTEMPTS) -> bool:
        target = Path(path)
        last = ""
        for step in range(1, max(1, int(attempts)) + 1):
            try:
                self._verify_python(target)
                return True
            except SyntaxError:
                last = traceback.format_exc()
            if not self.repair_sync(last, task_name=f"repair:{target.name}"):
                return False
        try:
            self._verify_python(target)
            return True
        except SyntaxError:
            return False

    def repair_sync(self, trace: str, task_name: str = "heal") -> bool:
        try:
            parsed = parse_error_log(trace, root=self.root)
            if not parsed.healable or parsed.path is None:
                logger.info("self-heal: no jailed source frame")
                return False
            task = self._dispatch_repair_task(parsed, task_name)
            self._snapshot_before(parsed.path)
            diff = self._propose_local_diff(parsed)
            if not diff:
                diff = self._propose_llm_diff_blocking(trace, parsed)
            if not diff:
                logger.info("self-heal: no patch hypothesis")
                return False
            applied = self._apply_patch(diff)
            if not applied.get("ok"):
                logger.info("self-heal: patch rejected %s", applied.get("error") or "")
                return False
            try:
                self._verify_python(parsed.path)
            except SyntaxError:
                return False
            self._record_recovery(parsed, task, diff)
            try:
                from robber.ssot import SsotIndex

                SsotIndex(self.root).reindex()
            except Exception:
                logger.debug("self-heal reindex skipped")
            self.last_repair = {
                "ok": True,
                "path": str(parsed.path),
                "kind": parsed.kind,
                "task_id": str(task.get("id") or ""),
            }
            return True
        except Exception:
            logger.exception("self-healing failed closed")
            return False

    def _dispatch_repair_task(self, parsed: ParsedError, task_name: str) -> dict[str, Any]:
        rel = self._rel(parsed.path) if parsed.path else task_name
        return self.manager.put_task(
            f"Heal {rel}",
            status="queued",
            payload={
                "tag": "fix",
                "task_type": "CODE_GEN",
                "tools": ["ast_edit", "patch_applier"],
                "heal": True,
                "path": rel,
                "kind": parsed.kind,
                "facts": parsed.facts,
            },
        )

    def _snapshot_before(self, path: Path) -> None:
        try:
            from robber.engine.recovery import SnapshotRecoveryEngine

            SnapshotRecoveryEngine(self.root, manager=self.manager).snapshot(
                label=f"pre-heal {path.name}",
                files=[self._rel(path)],
                reason="self-heal",
            )
        except Exception:
            logger.debug("pre-heal snapshot skipped")

    def _propose_local_diff(self, parsed: ParsedError) -> str | None:
        if parsed.path is None:
            return None
        try:
            original = parsed.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        updated = _local_fix(original, parsed)
        if updated is None or updated == original:
            return None
        return _unified_diff(self._rel(parsed.path), original, updated)

    def _propose_llm_diff_blocking(self, trace: str, parsed: ParsedError) -> str | None:
        if parsed.path is None:
            return None
        try:
            snippet = _read_snippet(parsed.path, parsed.line_no)
            prompt = (
                "You repair local workspace code. Return ONLY a unified diff "
                "(--- / +++ / @@ hunks).\n"
                "Do not explain. Do not touch secrets, .env, or files outside the listed path.\n"
                f"Target file: {self._rel(parsed.path)}\n\n"
                f"Trace:\n{trace[-4000:]}\n\n"
                f"Snippet:\n{snippet}\n"
            )
            text = _complete_blocking(prompt)
            if not text:
                return None
            return extract_diff(text) or (text if "@@" in text and "+++" in text else None)
        except Exception:
            return None

    def _apply_patch(self, diff: str) -> dict[str, Any]:
        ssot = default_ssot_path(self.root)
        try:
            from robber.indexer_bridge import apply_diff_atomic, load_indexer

            if load_indexer() is not None:
                report = apply_diff_atomic(self.root, diff, ssot)
                if isinstance(report, dict) and report.get("ok"):
                    return report
                if isinstance(report, dict) and report.get("ok") is False:
                    logger.info("rust patch failed, trying python applicator")
        except Exception:
            logger.debug("rust AtomicDiffPatcher unavailable")
        from robber.mutations import FileMutationManager, bind_mutations, get_mutations

        current = get_mutations()
        if Path(current.root).resolve() != self.root:
            bind_mutations(FileMutationManager(self.root))
        result = self.applicator.apply_sync(diff)
        return {
            "ok": result.ok,
            "error": result.message,
            "files": [result.path] if result.path else [],
        }

    def _record_recovery(self, parsed: ParsedError, task: dict[str, Any], diff: str) -> None:
        rel = self._rel(parsed.path) if parsed.path else ""
        self.manager.record_decision(
            f"self-heal {rel}",
            rationale=f"{parsed.kind}: {parsed.message}"[:800],
            task_id=str(task.get("id") or "") or None,
            branch="heal",
            payload=diff[:4000],
            version_tag="heal",
        )
        self.manager.add_artifact(
            "self-heal",
            body=f"{rel}\n{parsed.message}\n{diff}"[:4000],
            kind="heal",
            task_id=str(task.get("id") or "") or None,
            exit_code=0,
        )
        try:
            from robber.state_graph import StateGraphTracker

            StateGraphTracker(self.root).commit(
                f"SELF-HEAL {rel}\n{parsed.message}",
                summary=f"self-heal {path_name(rel)}",
            )
        except Exception:
            logger.debug("heal graph commit skipped")

    def _verify_python(self, path: Path) -> None:
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))
        compile(source, str(path), "exec")

    def _rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return path.name


def path_name(rel: str) -> str:
    return Path(rel).name if rel else "file"


def _read_snippet(path: Path, line_no: int, radius: int = 8) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"(unreadable: {exc})"
    start = max(1, line_no - radius)
    end = min(len(lines), line_no + radius)
    body = []
    for index in range(start, end + 1):
        mark = ">>" if index == line_no else "  "
        body.append(f"{mark} {index:4d} | {lines[index - 1]}")
    return "\n".join(body)


def _local_fix(source: str, parsed: ParsedError) -> str | None:
    lines = source.splitlines()
    if not lines:
        return None
    idx = max(0, min(len(lines) - 1, (parsed.line_no or 1) - 1))
    line = lines[idx]
    stripped = line.rstrip()
    if parsed.kind in {"syntax", "unknown"} and looks_like_missing_colon(parsed.message):
        match = BLOCK_START.match(stripped)
        if match and not stripped.endswith(":"):
            lines[idx] = stripped + ":"
            return _join(source, lines)
        if stripped.startswith("def ") or stripped.startswith("async def ") or stripped.startswith("class "):
            if not stripped.endswith(":"):
                lines[idx] = stripped + ":"
                return _join(source, lines)
    if parsed.kind == "indent" or "expected an indented block" in (parsed.message or "").lower():
        nxt = idx + 1
        if nxt < len(lines) and lines[nxt].strip() and not lines[nxt].startswith((" ", "\t")):
            indent = (BLOCK_START.match(stripped).group(1) if BLOCK_START.match(stripped) else "") + "    "
            lines[nxt] = indent + lines[nxt].lstrip()
            if stripped and not stripped.endswith(":"):
                lines[idx] = stripped + ":"
            return _join(source, lines)
    return None


def _join(source: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    if source.endswith("\n"):
        return body + "\n"
    return body


def _unified_diff(rel: str, original: str, updated: str) -> str:
    old = original.splitlines(keepends=True)
    new = updated.splitlines(keepends=True)
    if old and not old[-1].endswith("\n"):
        old[-1] += "\n"
    if new and not new[-1].endswith("\n"):
        new[-1] += "\n"
    hunks = list(
        difflib.unified_diff(
            old,
            new,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            n=3,
        )
    )
    return "".join(hunks)


def _complete_blocking(prompt: str) -> str:
    try:
        from robber.router import resolve_live_engines

        engines = resolve_live_engines("Hybrid Multi-LLM") or resolve_live_engines("Gemini Ultra")
        if not engines:
            return ""
        return ""
    except Exception:
        return ""
