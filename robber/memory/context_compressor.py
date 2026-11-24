"""Token-aware context compression for long-horizon turns."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from robber.config import (
    COMPRESS_ARTIFACT_CHARS,
    COMPRESS_LOG_TOKEN_TRIGGER,
    COMPRESS_PROMPT_TOKEN_CAP,
    COMPRESS_SUMMARY_CHARS,
)
from robber.jail import workspace_root
from robber.ssot_context import estimate_tokens
from robber.state_manager import StateManager

logger = logging.getLogger(__name__)

_SYMBOL = re.compile(r"\b(def|class|async def)\s+([A-Za-z_][\w]*)")
_PATH = re.compile(r"[\w./\\-]+\.(?:py|ts|tsx|js|go|rs)\b")
_ERROR = re.compile(r"\b(Error|Exception|FAILED|SyntaxError|ok)\b")
_FAIL = re.compile(r"\b(FAILED|ERROR|AssertionError|failed assertion)\b")
_PASS = re.compile(r"\b(PASSED|passed in |ok graph|exit_code.: 0|tests passed)\b", re.IGNORECASE)


class ContextCompressor:
    """Collapse completed tool logs into memory_store summaries and prune prompts."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        manager: StateManager | None = None,
        token_cap: int = COMPRESS_PROMPT_TOKEN_CAP,
        log_trigger: int = COMPRESS_LOG_TOKEN_TRIGGER,
        hub: Any = None,
    ) -> None:
        self.root = Path(root).resolve() if root else workspace_root()
        self.manager = manager or StateManager(self.root)
        self.token_cap = max(256, int(token_cap))
        self.log_trigger = max(256, int(log_trigger))
        self.hub = hub
        self.last_summary: dict[str, Any] = {}

    def summarize_task(self, task_id: str, *, force: bool = False) -> dict[str, Any] | None:
        task = self.manager.get_task(task_id)
        if task is None:
            return None
        artifacts = self.manager.list_artifacts(task_id=task_id, limit=16)
        blob = "\n".join(str(item.get("body") or "") for item in artifacts)
        tokens = estimate_tokens(blob) if blob else 0
        if not artifacts and not force:
            return None
        payload = self._structured(task, artifacts, blob)
        summary = json.dumps(payload, ensure_ascii=True)[:COMPRESS_SUMMARY_CHARS]
        saved = self.manager.add_memory(
            summary,
            kind="summary",
            source=str(task_id),
            token_est=estimate_tokens(summary),
        )
        self.last_summary = payload
        if tokens >= self.log_trigger or force:
            logger.info(
                "[COMPRESS] task=%s tokens=%s files=%s test_status=%s",
                str(task_id)[:12],
                tokens,
                ",".join(payload.get("files_modified") or [])[:120],
                payload.get("test_status"),
            )
        return saved

    def compress_logs(self, text: str, *, meta: dict[str, Any] | None = None) -> str:
        blob = text or ""
        if estimate_tokens(blob) <= self.log_trigger:
            return blob
        extra = meta or {}
        payload = {
            "files_modified": _unique(_PATH.findall(blob))[:8],
            "key_decisions": self._extract_facts(blob)[:8],
            "test_status": _test_status(blob),
        }
        for key in ("files_modified", "key_decisions", "test_status"):
            if key in extra:
                payload[key] = extra[key]
        return json.dumps(payload, ensure_ascii=True)[:COMPRESS_SUMMARY_CHARS]

    def compact_session(self) -> dict[str, Any]:
        wrote = 0
        tokens = 0
        existing = {
            str(row.get("source") or "")
            for row in self.manager.list_memory(kind="summary", limit=256)
        }
        for row in self.manager.list_tasks(status="completed", limit=64):
            task_id = str(row.get("id") or "")
            if not task_id or task_id in existing:
                continue
            saved = self.summarize_task(task_id, force=True)
            if saved:
                wrote += 1
                existing.add(task_id)
        for row in self.manager.list_memory(kind="summary", limit=256):
            tokens += int(row.get("token_est") or 0)
        return {"summaries": wrote, "tokens": tokens, "cap": self.token_cap}

    def prune_prompt(self, text: str, *, keep: list[str] | None = None) -> str:
        blob = self.strip_tool_logs(text or "")
        pins = [item for item in (keep or []) if item]
        if estimate_tokens(blob) <= self.token_cap:
            return blob
        facts = self._extract_facts(blob)
        for pin in pins:
            if pin not in facts:
                facts.append(pin[:200])
        packed = "\n".join(facts)
        if estimate_tokens(packed) <= self.token_cap:
            return packed
        char_budget = self.token_cap * 4
        return packed[:char_budget].rstrip()

    def strip_tool_logs(self, text: str) -> str:
        blob = text or ""
        if estimate_tokens(blob) <= self.log_trigger:
            return blob
        cleaned: list[str] = []
        for raw in blob.splitlines():
            line = raw.rstrip()
            lower = line.lower()
            if lower.startswith("stdout") or lower.startswith("stderr"):
                continue
            if "traceback (most recent call last)" in lower:
                continue
            cleaned.append(line)
        packed = "\n".join(cleaned).strip() or self.compress_logs(blob)
        return packed

    def clip_artifact_body(self, body: str) -> str:
        text = body or ""
        if estimate_tokens(text) > self.log_trigger:
            return self.compress_logs(text)
        if len(text) <= COMPRESS_ARTIFACT_CHARS:
            return text
        head = text[: COMPRESS_ARTIFACT_CHARS // 2].rstrip()
        tail = text[-COMPRESS_ARTIFACT_CHARS // 2 :].lstrip()
        return f"{head}\n...\n{tail}"

    def summary_for(self, task_id: str) -> str:
        rows = self.manager.list_memory(kind="summary", source=str(task_id), limit=4)
        if not rows:
            return ""
        return str(rows[-1].get("body") or "")[:COMPRESS_SUMMARY_CHARS]

    def _structured(
        self,
        task: dict[str, Any],
        artifacts: list[dict[str, Any]],
        blob: str,
    ) -> dict[str, Any]:
        title = str(task.get("title") or task.get("id") or "task")
        status = str(task.get("status") or "")
        symbols: list[str] = []
        paths: list[str] = []
        errors: list[str] = []
        for item in artifacts:
            body = str(item.get("body") or "")
            for match in _SYMBOL.finditer(body):
                symbols.append(match.group(2))
            for match in _PATH.finditer(body):
                paths.append(match.group(0).replace("\\", "/"))
            if _ERROR.search(body):
                errors.append(body.strip().splitlines()[-1][:160])
        return {
            "files_modified": _unique(paths)[:8],
            "key_decisions": _unique(symbols + errors)[:8],
            "test_status": _test_status(blob + " " + status),
            "goal": title[:160],
            "status": status,
        }

    def _extract_facts(self, text: str) -> list[str]:
        facts: list[str] = []
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("## ") or line.startswith("id:") or line.startswith("title:"):
                facts.append(line[:220])
                continue
            if line.startswith("{") and "files_modified" in line:
                facts.append(line[:220])
                continue
            if _SYMBOL.search(line) or _PATH.search(line) or _ERROR.search(line):
                facts.append(line[:220])
        if not facts:
            facts = [line.strip()[:220] for line in text.splitlines() if line.strip()][:12]
        return facts[:40]


def _test_status(blob: str) -> str:
    text = blob or ""
    if _FAIL.search(text):
        return "FAILED"
    if _PASS.search(text) or "completed" in text.lower():
        return "PASSED"
    return "UNKNOWN"


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out
