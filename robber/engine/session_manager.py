import asyncio
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

@dataclass
class Session:
    client_id: str
    history: List[Dict[str, Any]] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    last_active: float = field(default_factory=lambda: asyncio.get_running_loop().time())

class SessionManager:
    def __init__(self):
        self._sessions: Dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def get_session(self, client_id: str) -> Session:
        async with self._lock:
            if client_id not in self._sessions:
                self._sessions[client_id] = Session(client_id=client_id)
            session = self._sessions[client_id]
            session.last_active = asyncio.get_running_loop().time()
            return session

    async def add_to_history(self, client_id: str, message: Dict[str, Any]):
        async with self._lock:
            if client_id in self._sessions:
                self._sessions[client_id].history.append(message)
                self._sessions[client_id].last_active = asyncio.get_running_loop().time()

    async def remove_session(self, client_id: str):
        async with self._lock:
            if client_id in self._sessions:
                del self._sessions[client_id]

    async def list_sessions(self) -> List[str]:
        async with self._lock:
            return list(self._sessions.keys())

    async def update_session(self, client_id: str, key: str, value: Any) -> None:
        async with self._lock:
            if client_id not in self._sessions:
                self._sessions[client_id] = Session(client_id=client_id)
            session = self._sessions[client_id]
            session.context[key] = value
            session.last_active = asyncio.get_running_loop().time()
