"""Interactive User Clarification Engine.

Broadcasts clarification questions to the Web Dashboard (and optionally
TUI via IPC) and waits for user responses.  Used by the swarm orchestrator
before large multi-file mutations or when architectural ambiguity is detected.

Target overhead: near-zero when no question is pending.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

QUESTION_TIMEOUT_DEFAULT = 120.0
QUESTION_TIMEOUT_MAX = 600.0


@dataclass
class ClarificationQuestion:
    """A single clarification prompt sent to the user."""

    id: str = ""
    question: str = ""
    options: list[str] = field(default_factory=list)
    allow_custom: bool = True
    timeout: float = QUESTION_TIMEOUT_DEFAULT
    source_worker: str = ""
    task_id: str = ""
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"q-{uuid.uuid4().hex[:12]}"
        if not self.created_at:
            self.created_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "options": list(self.options),
            "allow_custom": self.allow_custom,
            "timeout": self.timeout,
            "source_worker": self.source_worker,
            "task_id": self.task_id,
            "created_at": self.created_at,
        }


@dataclass
class _PendingQuestion:
    question: ClarificationQuestion
    event: asyncio.Event
    answer: str | None = None


class QuestionEngine:
    """Dispatches clarification questions and collects user responses."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingQuestion] = {}
        self._answered: dict[str, str] = {}
        self._lock = asyncio.Lock()

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def pending_list(self) -> list[ClarificationQuestion]:
        return [p.question for p in self._pending.values()]

    async def ask(
        self,
        question: str,
        *,
        options: list[str] | None = None,
        allow_custom: bool = True,
        timeout: float = QUESTION_TIMEOUT_DEFAULT,
        source_worker: str = "",
        task_id: str = "",
    ) -> str:
        """Broadcast a clarification question and wait for the user response.

        Returns the selected option text or custom input string.
        Returns ``"[timeout]"`` if the user does not respond in time.
        Returns ``"[auto:skip]"`` if no dashboard client is connected and
        ``allow_custom`` is True (autonomous mode).
        """
        q = ClarificationQuestion(
            question=question,
            options=options or [],
            allow_custom=allow_custom,
            timeout=min(timeout, QUESTION_TIMEOUT_MAX),
            source_worker=source_worker,
            task_id=task_id,
        )
        evt = asyncio.Event()
        async with self._lock:
            self._pending[q.id] = _PendingQuestion(question=q, event=evt)

        logger.info("[question] %s (%s)", q.id, question[:80])

        try:
            self._broadcast_question(q)
        except Exception:
            logger.debug("[question] broadcast failed")

        has_clients = self._has_connected_clients()
        if not has_clients and allow_custom:
            logger.info("[question] %s no clients connected, autonomous skip", q.id)
            self._finalize(q.id, "[auto:skip]")
            return "[auto:skip]"

        try:
            await asyncio.wait_for(evt.wait(), timeout=q.timeout)
        except asyncio.TimeoutError:
            logger.warning("[question] %s timed out after %.0fs", q.id, q.timeout)
            self._finalize(q.id, "[timeout]")
            return "[timeout]"

        answer = self._answered.pop(q.id, "[auto:skip]")
        logger.info("[question] %s answer=%s", q.id, answer[:60])
        return answer

    def answer(self, question_id: str, answer: str) -> bool:
        """Feed a user answer into the engine. Returns True if accepted."""
        qid = (question_id or "").strip()
        async_answer = (answer or "").strip()
        if not qid or qid not in self._pending:
            return False
        self._finalize(qid, async_answer)
        return True

    def _finalize(self, question_id: str, answer: str) -> None:
        entry = self._pending.pop(question_id, None)
        if entry is None:
            return
        self._answered[question_id] = answer
        entry.answer = answer
        entry.event.set()
        try:
            self._broadcast_answer(question_id, answer)
        except Exception:
            logger.debug("[question] answer broadcast failed")

    def _has_connected_clients(self) -> bool:
        try:
            from robber.ui.web_dashboard import _connected_clients

            return bool(_connected_clients)
        except Exception:
            return False

    def _broadcast_question(self, q: ClarificationQuestion) -> None:
        try:
            from robber.ui.web_dashboard import broadcast_dashboard_event

            broadcast_dashboard_event("question", q.to_dict())
        except Exception:
            logger.debug("[question] dashboard broadcast skipped")
        try:
            from robber.gateway_ipc import broadcast_question_ipc

            broadcast_question_ipc(q)
        except (ImportError, AttributeError):
            pass
        except Exception:
            logger.debug("[question] IPC broadcast skipped")

    def _broadcast_answer(self, question_id: str, answer: str) -> None:
        try:
            from robber.ui.web_dashboard import broadcast_dashboard_event

            broadcast_dashboard_event(
                "question_answer",
                {"question_id": question_id, "answer": answer},
            )
        except Exception:
            pass


_engine: QuestionEngine | None = None


def get_question_engine() -> QuestionEngine:
    global _engine
    if _engine is None:
        _engine = QuestionEngine()
    return _engine
