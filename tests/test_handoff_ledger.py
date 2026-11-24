from __future__ import annotations

import time
from pathlib import Path

from robber.config import HANDOFF_TTL_SECONDS
from robber.handoff_ledger import HandoffLedger


def test_post_consume_dump_and_checksum(tmp_path: Path) -> None:
    ledger = HandoffLedger(tmp_path)
    block = ledger.post("TODO: claim this task", source="planning", target="coding")
    assert block.verify()
    assert HANDOFF_TTL_SECONDS == 3600.0
    assert ledger.pending("coding")
    dumped = ledger.format_dump("coding")
    assert "planning" in dumped
    assert "```markdown" in dumped
    ledger.consume(block.id, "coding")
    assert not ledger.pending("coding")
    assert "No unconsumed" in ledger.format_dump("coding")


def test_prune_consumed_after_one_hour_keeps_unhandled(tmp_path: Path) -> None:
    ledger = HandoffLedger(tmp_path)
    now = time.time()
    old_consumed = ledger.post("done work", source="review", target="*")
    ledger.consume(old_consumed.id, "brain")
    fresh_open = ledger.post("still live", source="planning", target="coding")
    stale_open = ledger.post("unhandled ancient", source="coding", target="review")
    for block in ledger.blocks:
        if block.id == old_consumed.id:
            block.created_at = now - 3601
        elif block.id == stale_open.id:
            block.created_at = now - 7200
    dropped = ledger.prune(now=now)
    ids = {block.id for block in ledger.blocks}
    assert old_consumed.id not in ids
    assert fresh_open.id in ids
    assert stale_open.id in ids
    assert dropped >= 1
    reloaded = HandoffLedger(tmp_path)
    reloaded_ids = {block.id for block in reloaded.blocks}
    assert old_consumed.id not in reloaded_ids
    assert fresh_open.id in reloaded_ids
    assert stale_open.id in reloaded_ids
