"""Local Multi-Model Handoff Ledger (LMMHL).

Peer-to-peer specialist communication via mmap-backed JSON blocks. No broker.
"""

from __future__ import annotations

import json
import mmap
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from robber.config import HANDOFF_MAX_BLOCKS, HANDOFF_TTL_SECONDS, MMAP_WINDOW_BYTES
from robber.jail import workspace_root
from robber.ssot import QuantumSafeChecksum

_LEDGER: HandoffLedger | None = None
HANDOFF_KIND = "handoff"


@dataclass
class HandoffPayload:
    id: str
    source: str
    target: str
    kind: str
    body: str
    checksum: str
    created_at: float
    consumed_by: list[str] = field(default_factory=list)
    unhandled: bool = True

    def verify(self) -> bool:
        return QuantumSafeChecksum.verify(self.body, self.checksum)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "unhandled": self.unhandled,
            "consumed_by": list(self.consumed_by),
            "checksum": self.checksum,
            "created_at": self.created_at,
            "preview": (self.body or "").strip().splitlines()[0][:120] if (self.body or "").strip() else "",
        }


class HandoffLedger:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or workspace_root()).resolve()
        self.dir = self.root / ".robber" / "handoff"
        self.path = self.dir / "ledger.json"
        self.blocks: list[HandoffPayload] = []
        self._lock = threading.RLock()
        self._load()

    def post(
        self,
        body: str,
        *,
        source: str,
        target: str = "*",
        kind: str = "specialist",
    ) -> HandoffPayload:
        payload = HandoffPayload(
            id=uuid.uuid4().hex[:16],
            source=source,
            target=target or "*",
            kind=kind or "specialist",
            body=body or "",
            checksum=QuantumSafeChecksum.digest(body or ""),
            created_at=time.time(),
            unhandled=True,
        )
        with self._lock:
            self.blocks.append(payload)
            self.prune()
            self._save()
            self._sync_ssot(payload)
            return payload

    def consume(self, block_id: str, consumer: str) -> HandoffPayload | None:
        with self._lock:
            for block in self.blocks:
                if block.id != block_id:
                    continue
                if consumer not in block.consumed_by:
                    block.consumed_by.append(consumer)
                    block.unhandled = False
                    self._save()
                    self._sync_ssot(block)
                return block
            return None

    def pending(self, target: str | None = None) -> list[HandoffPayload]:
        return self.dump_for(target)

    def dump_for(self, agent: str | None = None) -> list[HandoffPayload]:
        needle = (agent or "").strip().lower()
        rows = [block for block in self.blocks if block.unhandled and block.verify()]
        if not needle:
            return rows
        matched = [
            block
            for block in rows
            if needle in block.target.lower()
            or needle in block.source.lower()
            or block.target in {"*", agent}
        ]
        return matched

    def format_dump(self, agent: str | None = None) -> str:
        rows = self.dump_for(agent)
        if not rows:
            who = agent or "any specialist"
            return f"No unconsumed handoff payloads for {who}."
        parts: list[str] = [f"LMMHL  {len(rows)} unconsumed payload(s)"]
        for block in rows:
            parts.append(f"### {block.source} → {block.target}  `{block.id}`")
            parts.append(f"- kind: {block.kind}")
            parts.append(f"- checksum: {block.checksum[:12]}...")
            body = (block.body or "").strip() or "(empty)"
            parts.append("```markdown")
            parts.append("\n".join(body.splitlines()[:40]))
            parts.append("```")
        return "\n".join(parts)

    def export_text(self) -> str:
        lines: list[str] = []
        for block in self.blocks:
            flag = "UNHANDLED" if block.unhandled else "consumed"
            lines.append(f"[{flag}] {block.source}->{block.target} {block.kind}")
            lines.extend((block.body or "").splitlines()[:12])
            lines.append("")
        return "\n".join(lines).strip()

    def prune(self, *, now: float | None = None) -> int:
        clock = time.time() if now is None else now
        kept: list[HandoffPayload] = []
        dropped_ids: list[str] = []
        for block in self.blocks:
            age = clock - float(block.created_at or 0.0)
            consumed = (not block.unhandled) or bool(block.consumed_by)
            if age > HANDOFF_TTL_SECONDS and consumed:
                dropped_ids.append(block.id)
                continue
            kept.append(block)
        if len(kept) > HANDOFF_MAX_BLOCKS:
            dropped_ids.extend(block.id for block in kept[:-HANDOFF_MAX_BLOCKS])
            kept = kept[-HANDOFF_MAX_BLOCKS:]
        dropped = len(dropped_ids)
        changed = dropped > 0 or len(kept) != len(self.blocks)
        self.blocks = kept
        if changed:
            self._save()
            for block_id in dropped_ids:
                self._drop_ssot(block_id)
        return dropped

    def iter_mmap_blocks(self) -> Iterator[bytes]:
        if not self.path.is_file():
            return
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size <= 0:
            return
        with self.path.open("rb") as handle:
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                length = mapped.size()
                step = max(4096, int(MMAP_WINDOW_BYTES))
                for start in range(0, length, step):
                    yield bytes(mapped[start : start + step])

    def _load(self) -> None:
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError, UnicodeError):
                raw = None
            rows = raw.get("blocks") if isinstance(raw, dict) else raw
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    self.blocks.append(
                        HandoffPayload(
                            id=str(row.get("id") or uuid.uuid4().hex[:16]),
                            source=str(row.get("source") or "unknown"),
                            target=str(row.get("target") or "*"),
                            kind=str(row.get("kind") or "specialist"),
                            body=str(row.get("body") or ""),
                            checksum=str(row.get("checksum") or ""),
                            created_at=float(row.get("created_at") or 0.0),
                            consumed_by=[str(item) for item in (row.get("consumed_by") or [])],
                            unhandled=bool(row.get("unhandled", True)),
                        )
                    )
        if not self.blocks:
            self._load_from_ssot()
        else:
            for block in self.blocks:
                self._sync_ssot(block)

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        blob = json.dumps({"blocks": [asdict(block) for block in self.blocks]}, indent=2)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(blob, encoding="utf-8")
        os.replace(tmp, self.path)

    def _ssot(self):
        from robber.ssot_db import get_ssot_db

        return get_ssot_db(self.root)

    def _sync_ssot(self, block: HandoffPayload) -> None:
        blob = json.dumps(asdict(block), ensure_ascii=True, separators=(",", ":"))
        self._ssot().upsert_memory(
            memory_id=block.id,
            kind=HANDOFF_KIND,
            source=f"{block.source}->{block.target}",
            body=blob,
            created_at=block.created_at,
        )

    def _drop_ssot(self, memory_id: str) -> None:
        try:
            self._ssot().delete_memory(memory_id)
        except Exception:
            return

    def _load_from_ssot(self) -> None:
        try:
            rows = self._ssot().list_memory(kind=HANDOFF_KIND, limit=HANDOFF_MAX_BLOCKS)
        except Exception:
            return
        seen: set[str] = {block.id for block in self.blocks}
        for row in rows:
            raw = row.get("body") or ""
            try:
                data = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            block_id = str(data.get("id") or row.get("id") or "")
            if not block_id or block_id in seen:
                continue
            seen.add(block_id)
            self.blocks.append(
                HandoffPayload(
                    id=block_id,
                    source=str(data.get("source") or "unknown"),
                    target=str(data.get("target") or "*"),
                    kind=str(data.get("kind") or "specialist"),
                    body=str(data.get("body") or ""),
                    checksum=str(data.get("checksum") or ""),
                    created_at=float(data.get("created_at") or row.get("created_at") or 0.0),
                    consumed_by=[str(item) for item in (data.get("consumed_by") or [])],
                    unhandled=bool(data.get("unhandled", True)),
                )
            )


def get_handoff_ledger() -> HandoffLedger:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = HandoffLedger()
    return _LEDGER


def bind_handoff_ledger(ledger: HandoffLedger | None) -> HandoffLedger:
    global _LEDGER
    _LEDGER = ledger if ledger is not None else HandoffLedger()
    return _LEDGER
