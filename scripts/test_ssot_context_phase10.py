"""Phase 10 — adjustable SSOT context window (default 1 trillion)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def _ok(message: str) -> None:
    print(f"ok: {message}")


async def main() -> None:
    from robber.ssot_context import (
        DEFAULT_CONTEXT_WINDOW,
        format_window,
        pack_ssot_context,
        parse_window,
    )

    if parse_window("1t") != DEFAULT_CONTEXT_WINDOW:
        _fail(f"1t should be {DEFAULT_CONTEXT_WINDOW}, got {parse_window('1t')}")
    if parse_window("1e12") != DEFAULT_CONTEXT_WINDOW:
        _fail("1e12 should match default")
    if parse_window(2_000_000_000_000) != 2_000_000_000_000:
        _fail("int 2T rejected")
    if format_window(DEFAULT_CONTEXT_WINDOW) != "1T":
        _fail(f"format default, got {format_window(DEFAULT_CONTEXT_WINDOW)}")
    try:
        parse_window(0)
        _fail("zero window should be rejected")
    except ValueError:
        _ok("reject zero")
    try:
        parse_window(-8)
        _fail("negative window should be rejected")
    except ValueError:
        _ok("reject negative")
    _ok("parse 1t / 1e12")

    with tempfile.TemporaryDirectory(prefix="robber-ssot-ctx-") as raw:
        work = Path(raw)
        os.environ["ROBBER_WORKSPACE"] = str(work)
        (work / "seed.py").write_text(
            "def seed():\n    return '" + ("alpha " * 80) + "'\n",
            encoding="utf-8",
        )
        (work / "other.py").write_text(
            "def other():\n    return '" + ("beta " * 80) + "'\n",
            encoding="utf-8",
        )

        import robber.ssot as ssot_mod
        from robber.db.database import Database
        from robber.notes import add_note
        from robber.prefs import load_context_window, save_context_window

        ssot_mod._INDEX = None
        saved = save_context_window(DEFAULT_CONTEXT_WINDOW)
        if saved != DEFAULT_CONTEXT_WINDOW:
            _fail("prefs did not store default 1T")
        if load_context_window() != DEFAULT_CONTEXT_WINDOW:
            _fail("prefs round-trip failed")
        if save_context_window("2m") != 2_000_000:
            _fail("prefs 2m failed")
        _ok("prefs persist")

        db = Database(work / ".robber" / "accounts.db")
        await db.connect()
        if await db.get_ssot_window() is not None:
            _fail("fresh db should have no ssot_context row")
        await db.set_ssot_window(DEFAULT_CONTEXT_WINDOW)
        if await db.get_ssot_window() != DEFAULT_CONTEXT_WINDOW:
            _fail("sqlite did not store 1T")
        await db.set_ssot_packed(12, 3)
        row = await db.get_ssot_context_row()
        if row is None or int(row["packed_tokens"]) != 12 or int(row["hit_count"]) != 3:
            _fail(f"packed row wrong: {row}")
        if int(row["window_tokens"]) != DEFAULT_CONTEXT_WINDOW:
            _fail("set_ssot_packed overwrote the window")
        await db.close()
        _ok("sqlite persist")

        index = ssot_mod.get_index()
        stats = index.reindex()
        if int(stats["files"]) < 2:
            _fail(f"reindex failed: {stats}")
        add_note("operator", "prefer plan mode", category="user")
        packed = pack_ssot_context("seed function", window=50)
        if packed.configured != 50:
            _fail(f"configured {packed.configured}")
        if packed.packed > 50:
            _fail(f"packed {packed.packed} exceeded window 50")
        if packed.packed < 1:
            _fail("packed context was empty")
        _ok(f"pack cap {packed.packed}/50")

        wide = pack_ssot_context("seed function", window=DEFAULT_CONTEXT_WINDOW)
        if wide.configured != DEFAULT_CONTEXT_WINDOW:
            _fail("wide window not 1T")
        if wide.packed > wide.configured:
            _fail("packed exceeded 1T")
        if "@seed.py#L1-L2" not in pack_ssot_context(
            "work on @seed.py#L1-L2", window=10_000
        ).text:
            _fail("pinned anchor missing from packed context")
        _ok("pack 1T + pin")

        ssot_mod._INDEX = None

    _ok("phase 10 ssot context")


if __name__ == "__main__":
    asyncio.run(main())
