from typing import Any, Dict, List, Tuple
import re

from robber.ssot_context import DEFAULT_CONTEXT_WINDOW, pack_ssot_context
from .ast_parser import ASTCodeParser

class VirtualSSOTEngine:
    """Main V-SSOT Coordinator. Packs the hash-json index into the configured window."""

    def __init__(self):
        self.active_window_limit = DEFAULT_CONTEXT_WINDOW
        self.parser = ASTCodeParser()
        self._vector_store = None

    @property
    def vector_store(self):
        if self._vector_store is None:
            from .vector_store import VectorIndexManager

            self._vector_store = VectorIndexManager()
        return self._vector_store

    def set_active_window_limit(self, limit: int):
        from robber.ssot_context import parse_window

        self.active_window_limit = parse_window(limit)

    def parse_anchor(self, anchor_str: str) -> Tuple[str, int, int]:
        """Parses @file_path#Lstart-Lend"""
        match = re.match(r"@(.+)#L(\d+)-L(\d+)", anchor_str)
        if not match:
            raise ValueError(f"Invalid anchor format: {anchor_str}")
        file_path, start, end = match.groups()
        return file_path, int(start), int(end)

    def build_context_payload(self, query: str, pinned_anchors: List[str]) -> Dict[str, Any]:
        packed = pack_ssot_context(
            query,
            window=self.active_window_limit,
            extra_anchors=pinned_anchors,
        )
        return {
            "payload_text": packed.text,
            "token_count": packed.packed,
            "configured": packed.configured,
            "available": packed.available,
            "hit_count": packed.hit_count,
            "pinned_sources": packed.pin_count,
            "semantic_sources": packed.hit_count,
        }

if __name__ == "__main__":
    engine = VirtualSSOTEngine()
    payload = engine.build_context_payload("startup", ["@server.py#L1-L20"])
    print(f"Tokens: {payload['token_count']}")
    print(f"Content snippet: {payload['payload_text'][:100]}...")
