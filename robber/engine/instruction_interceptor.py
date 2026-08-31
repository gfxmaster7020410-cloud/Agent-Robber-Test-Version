import threading
from typing import List

class InstructionInterceptor:
    """Thread-safe singleton for dynamic instruction injection."""
    
    def __init__(self):
        self._instructions: List[str] = []
        self._lock = threading.Lock()

    def inject_instruction(self, instruction: str) -> None:
        """Pushes dynamic instruction into queue."""
        with self._lock:
            self._instructions.append(instruction)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._instructions)

    def consume_pending_instructions(self) -> str:
        """Extracts and clears all queued dynamic directives."""
        with self._lock:
            compiled = "\n".join(self._instructions)
            self._instructions = []
            return compiled
