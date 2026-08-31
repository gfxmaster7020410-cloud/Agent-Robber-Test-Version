"""Self-healing loop: intercept failures, generate a jailed patch, retry."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from robber.jail import JailError, resolve_inside, workspace_root
from robber.patch import extract_diff
from robber.ssot import get_index
from robber.tools.patch_applier import JailedPatchApplicator

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
TRACE_FILE = re.compile(r'File "([^"]+)", line (\d+)')
HEALABLE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}

_HEALER: SelfHealingEngine | None = None

T = Callable[[], Awaitable[Any]]


def get_healer() -> SelfHealingEngine | None:
    return _HEALER


def bind_healer(engine: SelfHealingEngine | None) -> None:
    global _HEALER
    _HEALER = engine


class SelfHealingEngine:
    def __init__(self, hub: Any, applicator: JailedPatchApplicator | None = None) -> None:
        self.hub = hub
        self.applicator = applicator or JailedPatchApplicator()
        self.root = workspace_root()

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
        for step in range(1, attempts + 1):
            try:
                return await factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = exc
                await self.emit("detected", "Error detected", task=name, attempt=step)
                recovered = await self.heal_exception(exc, task_name=name)
                if not recovered or step == attempts:
                    await self.emit(
                        "manual",
                        "Self-healing exhausted — waiting for operator",
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

    async def heal_exception(self, exc: BaseException, *, task_name: str = "task", stderr: str | None = None) -> bool:
        try:
            trace = stderr or "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            extra = getattr(exc, "_heal_stderr", "")
            if extra and extra not in trace:
                trace = f"{trace}\n{extra}"
            target, line_no = self._extract_workspace_frame(trace)
            if target is None:
                await self.emit(
                    "manual",
                    "No jailed source frame in the trace — operator needed",
                    task=task_name,
                )
                return False
            snippet = await asyncio.to_thread(self._read_snippet, target, line_no)
            ssot_blob = await asyncio.to_thread(self._ssot_context, f"{target} {exc}")
            await self.emit("generating", "Generating patch", path=str(target), task=task_name)
            diff = await self._propose_diff(trace, target, snippet, ssot_blob)
            if not diff:
                await self.emit("manual", "Model returned no unified diff", task=task_name)
                return False
            from robber.agent_loop import get_loop

            loop = get_loop()
            if loop is not None and not loop.is_autonomous():
                approved = await loop.request_heal_approval(
                    diff, {"path": str(target), "task": task_name}
                )
                if not approved:
                    await self.emit("manual", "Operator rejected or timed out", task=task_name)
                    return False
            result = await self.applicator.apply(diff)
            if not result.ok:
                detail = result.traceback or result.message
                await self.emit(
                    "manual",
                    "Patch rejected",
                    task=task_name,
                    error=detail[:500],
                )
                return False
            await self.emit("syntax_verified", "Syntax verified", path=result.path)
            await self.emit("applied", "Applied", path=result.path)
            await asyncio.to_thread(get_index().reindex)
            return True
        except Exception:
            logger.exception("self-healing failed closed")
            await self.emit("manual", "Self-healing crashed — falling back to operator")
            return False

    def _extract_workspace_frame(self, trace: str) -> tuple[Path | None, int]:
        root = self.root
        best: tuple[Path, int] | None = None
        for match in TRACE_FILE.finditer(trace):
            raw = match.group(1)
            try:
                candidate = Path(raw)
                if not candidate.is_absolute():
                    candidate = resolve_inside(root, raw)
                else:
                    candidate = candidate.resolve()
                    candidate.relative_to(root)
            except (JailError, ValueError, OSError):
                continue
            if candidate.suffix.lower() not in HEALABLE_SUFFIXES:
                continue
            if not candidate.is_file():
                continue
            best = (candidate, int(match.group(2)))
        return (best[0], best[1]) if best else (None, 0)

    def _read_snippet(self, path: Path, line_no: int, radius: int = 8) -> str:
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

    def _ssot_context(self, query: str) -> str:
        try:
            index = get_index()
            return index.format_hits(index.search(query, k=4))
        except Exception as exc:
            return f"(ssot unavailable: {type(exc).__name__})"

    async def _propose_diff(self, trace: str, target: Path, snippet: str, ssot: str) -> str | None:
        rel = str(target.relative_to(self.root))
        prompt = (
            "You repair local workspace code. Return ONLY a unified diff (--- / +++ / @@ hunks).\n"
            "Do not explain. Do not touch secrets, .env, credentials, or files outside the listed path.\n"
            f"Target file: {rel}\n\n"
            f"Trace:\n{trace[-4000:]}\n\n"
            f"Snippet:\n{snippet}\n\n"
            f"SSOT:\n{ssot}\n"
        )
        text = await self._complete(prompt)
        if not text:
            return None
        return extract_diff(text) or (text if "@@" in text and "+++" in text else None)

    async def _complete(self, prompt: str) -> str:
        try:
            from robber.router import collect_completion, resolve_live_engines

            engines = resolve_live_engines("Hybrid Multi-LLM") or resolve_live_engines("Gemini Ultra")
            if not engines:
                return ""
            _reason, text = await collect_completion(engines[0], prompt, "act")
            return text.strip()
        except Exception as exc:
            logger.info("heal LLM unavailable (%s)", type(exc).__name__)
            return ""
