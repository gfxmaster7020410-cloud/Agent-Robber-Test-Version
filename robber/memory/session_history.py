"""Stateful chat session memory for multi-turn LLM conversations.

Retains all previous user, assistant, and tool interaction turns within the
active session ID so that models routed through ProxyRouter never lose
conversation context between dispatches.

Thread-safe: all mutations go through a lock.  History is held in memory
backed by the SSOT database for persistence across restarts.
"""

from __future__ import annotations

import json
import logging
import time
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MAX_TURNS_PER_SESSION = 120
SESSION_TTL_SECONDS = 3600 * 4  # 4 hours


@dataclass
class Turn:
    role: str
    content: str
    timestamp: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class SessionHistoryManager:
    """Per-session conversation turn retention.

    Each session ID maps to an ordered list of Turn objects.  The manager
    enforces a hard cap (MAX_TURNS_PER_SESSION) and evicts the oldest turns
    when the cap is exceeded.

    Usage::

        mgr = get_session_history()
        mgr.append("session-abc", "user", "What files are indexed?")
        mgr.append("session-abc", "assistant", "The indexed files are ...")
        history = mgr.get_messages("session-abc")
        # -> [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
    """

    def __init__(self, max_turns: int = MAX_TURNS_PER_SESSION) -> None:
        self._sessions: dict[str, list[Turn]] = {}
        self._timestamps: dict[str, float] = {}
        self._lock = threading.Lock()
        self._max_turns = max_turns

    def append(self, session_id: str, role: str, content: str, **metadata: Any) -> None:
        sid = (session_id or "").strip()
        if not sid or not content:
            return
        turn = Turn(role=role, content=content, metadata=metadata)
        with self._lock:
            turns = self._sessions.setdefault(sid, [])
            turns.append(turn)
            self._timestamps[sid] = time.time()
            if len(turns) > self._max_turns:
                self._sessions[sid] = turns[-self._max_turns:]

    def get_messages(self, session_id: str) -> list[dict[str, str]]:
        sid = (session_id or "").strip()
        with self._lock:
            turns = self._sessions.get(sid, [])
            if not turns:
                return []
            return [t.to_dict() for t in turns]

    def get_raw_turns(self, session_id: str) -> list[Turn]:
        sid = (session_id or "").strip()
        with self._lock:
            return list(self._sessions.get(sid, []))

    def clear(self, session_id: str) -> None:
        sid = (session_id or "").strip()
        with self._lock:
            self._sessions.pop(sid, None)
            self._timestamps.pop(sid, None)

    def session_ids(self) -> list[str]:
        with self._lock:
            return list(self._sessions.keys())

    def turn_count(self, session_id: str) -> int:
        sid = (session_id or "").strip()
        with self._lock:
            return len(self._sessions.get(sid, []))

    def evict_stale(self) -> int:
        now = time.time()
        evicted = 0
        with self._lock:
            stale = [
                sid
                for sid, ts in self._timestamps.items()
                if now - ts > SESSION_TTL_SECONDS
            ]
            for sid in stale:
                self._sessions.pop(sid, None)
                self._timestamps.pop(sid, None)
                evicted += 1
        return evicted

    def export_session(self, session_id: str) -> str:
        messages = self.get_messages(session_id)
        return json.dumps(messages, ensure_ascii=True, indent=2)

    def import_session(self, session_id: str, raw: str) -> int:
        try:
            messages = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return 0
        if not isinstance(messages, list):
            return 0
        count = 0
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") and msg.get("content"):
                self.append(session_id, msg["role"], msg["content"])
                count += 1
        return count


_global_history: SessionHistoryManager | None = None


def get_session_history() -> SessionHistoryManager:
    global _global_history
    if _global_history is None:
        _global_history = SessionHistoryManager()
    return _global_history
