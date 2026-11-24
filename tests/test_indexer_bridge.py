from __future__ import annotations

from robber.indexer_bridge import load_indexer


def test_indexer_bridge_is_optional() -> None:
    dll = load_indexer()
    assert dll is None or hasattr(dll, "robber_index_workspace")
    if dll is not None and hasattr(dll, "robber_get_symbol_context"):
        assert callable(dll.robber_get_symbol_context)
