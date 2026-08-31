import asyncio
from typing import Any, Dict, Optional

class SessionManager:
    """Thread-safe manager for session context, history, and state."""
    
    def __init__(self):
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        
    async def get_session(self, session_id: str) -> Dict[str, Any]:
        async with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = {
                    "history": [],
                    "context": {},
                    "state": {}
                }
            return self._sessions[session_id]
            
    async def update_session(self, session_id: str, key: str, value: Any):
        async with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = {"history": [], "context": {}, "state": {}}
            self._sessions[session_id][key] = value

    async def add_to_history(self, session_id: str, message: Dict[str, Any]):
        async with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = {"history": [], "context": {}, "state": {}}
            self._sessions[session_id]["history"].append(message)
