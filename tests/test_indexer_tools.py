from __future__ import annotations

import pytest

from robber.indexer_bridge import apply_diff_atomic, load_indexer, run_test_suite, run_tool_sandboxed


@pytest.mark.skipif(load_indexer() is None, reason="robber indexer dll is not built")
def test_rust_tool_exports_exist() -> None:
    dll = load_indexer()
    assert dll is not None
    assert hasattr(dll, "robber_run_tool_sandboxed")
    assert hasattr(dll, "robber_apply_diff_atomic")
    assert hasattr(dll, "robber_quick_search")
    assert callable(run_tool_sandboxed)
    assert callable(apply_diff_atomic)
    if hasattr(dll, "robber_run_test_suite"):
        assert callable(run_test_suite)
