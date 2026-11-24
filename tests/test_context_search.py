"""AST context extractor and node-level structural edits."""

from __future__ import annotations

from pathlib import Path

import pytest

from robber.memory.context_search import ASTContextExtractor
from robber.security.perimeter import SelfModificationProtectedError, agent_install_root
from robber.ssot_context import estimate_tokens
from robber.tools.structural_editor import EditError, replace_ast_node

ROTATOR = '''class Other:
    def ping(self):
        return 1

class APIKeyRotator:
    def __init__(self, key: str) -> None:
        self.key = key
        self.timeout = 30

    def rotate(self) -> str:
        return self.key

def helper():
    return APIKeyRotator("x")
'''

NEW_INIT = '''    def __init__(self, key: str, timeout: int = 30) -> None:
        self.key = key
        self.timeout = timeout
'''


def _write_rotator(root: Path) -> Path:
    path = root / "rotator.py"
    path.write_text(ROTATOR, encoding="utf-8")
    return path


def test_extractor_keeps_only_named_symbol(tmp_path: Path) -> None:
    _write_rotator(tmp_path)
    block = ASTContextExtractor(tmp_path, token_budget=800).extract(
        "Locate the definition of APIKeyRotator"
    )
    assert block.symbols
    assert any("APIKeyRotator" in str(item.get("name")) for item in block.symbols)
    assert "class Other" not in (block.text or "") or "APIKeyRotator" in block.text
    assert block.tokens <= 800
    assert estimate_tokens(block.text) <= 800


def test_replace_init_leaves_neighbors(tmp_path: Path) -> None:
    path = _write_rotator(tmp_path)
    updated = replace_ast_node(str(path), "APIKeyRotator.__init__", NEW_INIT, root=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "timeout: int = 30" in text
    assert "def ping(self):" in text
    assert "def rotate(self)" in text
    assert "def helper():" in text
    assert "class Other:" in text
    assert updated == text
    compile(text, str(path), "exec")


def test_invalid_replacement_does_not_touch_disk(tmp_path: Path) -> None:
    path = _write_rotator(tmp_path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(EditError):
        replace_ast_node(str(path), "APIKeyRotator.__init__", "    def __init__(self, key\n", root=tmp_path)
    assert path.read_text(encoding="utf-8") == before


def test_structural_edit_blocks_engine_tree() -> None:
    target = agent_install_root() / "robber" / "ssot_db.py"
    before = target.read_text(encoding="utf-8")
    with pytest.raises(SelfModificationProtectedError):
        replace_ast_node(str(target), "SsotDB", "class SsotDB:\n    pass\n")
    assert target.read_text(encoding="utf-8") == before
