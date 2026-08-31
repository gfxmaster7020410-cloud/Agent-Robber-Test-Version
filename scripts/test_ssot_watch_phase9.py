"""Phase 9 SSOT file watcher — reindex when jailed files change."""

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
    with tempfile.TemporaryDirectory(prefix="robber-ssot-watch-") as raw:
        work = Path(raw)
        (work / "seed.py").write_text("def seed():\n    return 1\n", encoding="utf-8")
        os.environ["ROBBER_WORKSPACE"] = str(work)

        import robber.ssot as ssot_mod
        from robber.ssot_watch import SsotWatcher

        ssot_mod._INDEX = None
        index = ssot_mod.get_index()
        first = index.reindex()
        if int(first["files"]) < 1:
            _fail(f"seed reindex failed: {first}")
        _ok("seed index")

        hits: list[dict[str, object]] = []

        async def on_refresh(stats: dict[str, object]) -> None:
            hits.append(stats)

        watcher = SsotWatcher(interval=0.2, debounce=0.15)
        await watcher.start(on_refresh)
        await asyncio.sleep(0.45)
        (work / "watched.py").write_text("def watched():\n    return 2\n", encoding="utf-8")
        for _ in range(40):
            if hits:
                break
            await asyncio.sleep(0.15)
        await watcher.stop()
        if not hits:
            _fail("watcher did not reindex after a new jailed file")
        latest = hits[-1]
        if int(latest["files"]) < 2:
            _fail(f"expected at least 2 files after watch, got {latest}")
        if watcher.events < 1:
            _fail("watch_events stayed at 0")
        _ok("reindex on create")

        (work / ".env").write_text("SECRET=1\n", encoding="utf-8")
        before = watcher.events
        extra: list[dict[str, object]] = []

        async def on_secret(stats: dict[str, object]) -> None:
            extra.append(stats)

        await watcher.start(on_secret)
        await asyncio.sleep(0.45)
        (work / ".env").write_text("SECRET=2\n", encoding="utf-8")
        await asyncio.sleep(0.9)
        await watcher.stop()
        if extra:
            _fail("secret .env should not trigger SSOT reindex")
        _ok("secrets ignored")
        _ = before


if __name__ == "__main__":
    asyncio.run(main())
    print("ssot watch phase 9 tests passed")
