"""Operator-injected SSOT notes — not secrets, persisted under .robber."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from robber.jail import workspace_root

NOTES_NAME = "notes.json"
CATEGORIES = ("system", "user", "session", "vector", "tool")


def notes_path() -> Path:
    return workspace_root() / ".robber" / NOTES_NAME


def load_notes() -> list[dict[str, object]]:
    path = notes_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = raw.get("notes", []) if isinstance(raw, dict) else []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def save_notes(rows: list[dict[str, object]]) -> None:
    path = notes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"notes": rows}, indent=2), encoding="utf-8")


def add_note(
    key: str,
    value: str,
    category: str = "user",
    tags: list[str] | None = None,
) -> dict[str, object]:
    key = key.strip()
    value = value.strip()
    category = category.strip().lower() or "user"
    if not key:
        raise ValueError("memory key is empty")
    if not value:
        raise ValueError("memory value is empty")
    if category not in CATEGORIES:
        category = "user"
    note = {
        "id": uuid.uuid4().hex[:12],
        "key": key,
        "value": value,
        "category": category,
        "tags": [tag.strip() for tag in (tags or []) if tag.strip()],
        "created": time.time(),
    }
    rows = load_notes()
    rows.insert(0, note)
    save_notes(rows)
    return note


def purge_notes() -> int:
    count = len(load_notes())
    save_notes([])
    return count


def search_notes(query: str) -> list[dict[str, object]]:
    needle = query.strip().lower()
    rows = load_notes()
    if not needle:
        return rows
    hits: list[dict[str, object]] = []
    for row in rows:
        blob = " ".join(
            [
                str(row.get("key", "")),
                str(row.get("value", "")),
                str(row.get("category", "")),
                " ".join(str(tag) for tag in row.get("tags", []) or []),
            ]
        ).lower()
        if needle in blob:
            hits.append(row)
    return hits
