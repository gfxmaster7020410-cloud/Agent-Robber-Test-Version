from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from robber.config import HANDOFF_TTL_SECONDS
from robber.handoff_ledger import HANDOFF_KIND, HandoffLedger
from robber.ssot_db import get_ssot_db, reset_ssot_cache


@pytest.fixture(autouse=True)
def _close_ssot() -> None:
    yield
    reset_ssot_cache()


def test_handoff_persists_into_memory_store(tmp_path: Path) -> None:
    ledger = HandoffLedger(tmp_path)
    block = ledger.post("```markdown\nTODO: take the coding slice\n```", source="planning", target="coding")
    assert block.verify()
    assert HANDOFF_TTL_SECONDS == 3600.0
    db = get_ssot_db(tmp_path)
    stored = db.get_memory(block.id)
    assert stored is not None
    assert stored["kind"] == HANDOFF_KIND
    payload = json.loads(stored["body"])
    assert payload["source"] == "planning"
    assert payload["unhandled"] is True
    assert ledger.pending("coding")
    windows = list(ledger.iter_mmap_blocks())
    assert windows
    assert max(len(chunk) for chunk in windows) <= 65_536
    ledger.consume(block.id, "coding")
    updated = json.loads(db.get_memory(block.id)["body"])
    assert updated["unhandled"] is False
    assert "coding" in updated["consumed_by"]
    assert not ledger.pending("coding")


def test_handoff_prune_drops_ssot_row_after_one_hour(tmp_path: Path) -> None:
    ledger = HandoffLedger(tmp_path)
    now = time.time()
    old_consumed = ledger.post("done work", source="review", target="*")
    ledger.consume(old_consumed.id, "brain")
    live = ledger.post("still live", source="planning", target="coding")
    for block in ledger.blocks:
        if block.id == old_consumed.id:
            block.created_at = now - 3601
    dropped = ledger.prune(now=now)
    assert dropped >= 1
    db = get_ssot_db(tmp_path)
    assert db.get_memory(old_consumed.id) is None
    assert db.get_memory(live.id) is not None
    reset_ssot_cache()
    revived = HandoffLedger(tmp_path)
    ids = {block.id for block in revived.blocks}
    assert old_consumed.id not in ids
    assert live.id in ids
    assert json.loads(get_ssot_db(tmp_path).get_memory(live.id)["body"])["body"] == "still live"


def test_handoff_recovers_from_memory_store_without_json(tmp_path: Path) -> None:
    ledger = HandoffLedger(tmp_path)
    block = ledger.post("peer payload", source="rust", target="go")
    ledger.path.unlink()
    reset_ssot_cache()
    restored = HandoffLedger(tmp_path)
    assert any(item.id == block.id and item.body == "peer payload" for item in restored.blocks)
    assert restored.pending("go")
