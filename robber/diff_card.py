"""Compact file-diff card models shared by the Textual TUI."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

PREVIEW_LIMIT = 4
META_LINE = re.compile(r"^(diff |index |--- |\+\+\+ |@@ )")
WRITE_PATH = re.compile(
    r"\b(?:Wrote to|Edit~to|Deleted|created|modified)\s+([^\s:]+)(?::)?",
    re.I,
)
EXT_LANG = {
    "py": "python",
    "ts": "typescript",
    "tsx": "typescript",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "json": "json",
    "css": "css",
    "html": "html",
    "md": "markdown",
    "diff": "diff",
    "patch": "diff",
    "sh": "bash",
    "bash": "bash",
    "rs": "rust",
    "go": "go",
    "toml": "toml",
    "yaml": "yaml",
    "yml": "yaml",
}


@dataclass(slots=True)
class MiniDiff:
    path: str
    added: int
    removed: int
    preview: list[tuple[str, str]] = field(default_factory=list)
    full: str = ""
    language: str = "text"
    marker: str = "+"
    action: str = "diff"


def parse_fence_header(raw: str) -> tuple[str, str | None]:
    meta = (raw or "").strip()
    if not meta:
        return "text", None
    path = None
    path_eq = re.search(r"(?:^|\s)path=(\S+)", meta, re.I)
    tokens = meta.split()
    first = tokens[0]
    language = first
    if ":" in first and not re.match(r"^https?:", first, re.I) and not re.match(r"^[A-Za-z]:[\\/]", first):
        language, rest = first.split(":", 1)
        language = language or first
        if rest:
            path = rest
    if path_eq:
        path = path_eq.group(1)
    if path is None:
        leftover = next((token for token in tokens[1:] if not token.lower().startswith("path=") and re.search(r"[./\\]", token)), None)
        if leftover:
            path = leftover
    if language in {"patch"}:
        language = "diff"
    return language or "text", path


def language_from_path(path: str, fallback: str = "text") -> str:
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return EXT_LANG.get(suffix, fallback)


def looks_like_unified_diff(text: str) -> bool:
    raw = text or ""
    return bool(re.search(r"^(diff --git |index |--- a/|\+\+\+ b/|@@ )", raw, re.M))


def looks_like_file_diff(code: str, language: str | None = None, path: str | None = None) -> bool:
    lang = (language or "").lower()
    if lang in {"diff", "patch"}:
        return True
    if path:
        return True
    return looks_like_unified_diff(code)


def write_path_from_text(text: str) -> str | None:
    match = WRITE_PATH.search(text or "")
    return match.group(1) if match else None


def count_diff_stats(text: str) -> tuple[int, int]:
    added = 0
    removed = 0
    for line in (text or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def path_from_diff(text: str) -> str | None:
    plus = re.search(r"^\+\+\+ (?:[ab]/)?(.+)$", text or "", re.M)
    if plus and plus.group(1) != "/dev/null":
        return plus.group(1).strip()
    minus = re.search(r"^--- (?:[ab]/)?(.+)$", text or "", re.M)
    if minus and minus.group(1) != "/dev/null":
        return minus.group(1).strip()
    git = re.search(r"^diff --git a/(.+) b/(.+)$", text or "", re.M)
    return git.group(2) if git else None


def preview_lines(text: str, limit: int = PREVIEW_LIMIT) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        if not line or META_LINE.match(line):
            continue
        if line.startswith("+"):
            out.append(("add", line))
        elif line.startswith("-"):
            out.append(("del", line))
        else:
            continue
        if len(out) >= limit:
            break
    if not out:
        for line in (text or "").splitlines():
            if not line.strip() or META_LINE.match(line):
                continue
            out.append(("ctx", line))
            if len(out) >= min(3, limit):
                break
    return out


def marker_for(added: int, removed: int, action: str | None = None) -> str:
    if action == "delete" or (removed > 0 and added == 0):
        return "-"
    if action == "create" or (added > 0 and removed == 0):
        return "+"
    return "~"


def _prefixed(lines: list[str], kind: str, mark: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in lines:
        if not line:
            continue
        rows.append((kind, line if line.startswith(mark) else f"{mark}{line}"))
        if len(rows) >= PREVIEW_LIMIT:
            break
    return rows


def _bag_stats(before: str, after: str) -> tuple[int, int]:
    bag: dict[str, int] = {}
    for line in before.splitlines():
        bag[line] = bag.get(line, 0) + 1
    added = 0
    for line in after.splitlines():
        have = bag.get(line, 0)
        if have > 0:
            bag[line] = have - 1
        else:
            added += 1
    removed = sum(bag.values())
    return added, removed


def mini_diff_from_unified(code: str, *, language: str | None = None, path: str | None = None) -> MiniDiff:
    added, removed = count_diff_stats(code)
    resolved = path or path_from_diff(code) or "file"
    lang_hint = (language or "").lower()
    lang = (
        lang_hint
        if lang_hint and lang_hint not in {"diff", "patch", "text"}
        else language_from_path(resolved, "diff")
    )
    if removed and added:
        action = "diff"
    elif removed:
        action = "delete"
    else:
        action = "create"
    return MiniDiff(
        path=resolved,
        added=added,
        removed=removed,
        preview=preview_lines(code),
        full=code,
        language=lang,
        marker=marker_for(added, removed, action),
        action=action,
    )


def mini_diff_from_file_card(card: dict) -> MiniDiff:
    after = str(card.get("after") or "")
    before = str(card.get("before") or "")
    path = str(card.get("path") or "file")
    language = str(card.get("language") or "") or language_from_path(path)
    if looks_like_unified_diff(after) or looks_like_unified_diff(before):
        return mini_diff_from_unified(after or before, language=language, path=path)
    action = str(card.get("action") or "edit")
    if action == "delete":
        removed = max(1, len(before.splitlines()) or 1)
        return MiniDiff(
            path=path,
            added=0,
            removed=removed,
            preview=_prefixed(before.splitlines(), "del", "-"),
            full=before,
            language=language,
            marker="-",
            action="delete",
        )
    if action == "create" or not before:
        added = max(1, len(after.splitlines()) or 1)
        return MiniDiff(
            path=path,
            added=added,
            removed=0,
            preview=_prefixed(after.splitlines(), "add", "+"),
            full=after,
            language=language,
            marker="+",
            action="create",
        )
    added, removed = _bag_stats(before, after)
    preview = _prefixed(before.splitlines(), "del", "-")[:2] + _prefixed(after.splitlines(), "add", "+")[:2]
    return MiniDiff(
        path=path,
        added=added,
        removed=removed,
        preview=preview[:PREVIEW_LIMIT],
        full=after,
        language=language,
        marker=marker_for(added, removed, "edit"),
        action="edit",
    )


def mini_diff_from_code(code: str, *, language: str | None = None, path: str | None = None) -> MiniDiff | None:
    if not looks_like_file_diff(code, language, path):
        return None
    lang = (language or "").lower()
    if looks_like_unified_diff(code) or lang in {"diff", "patch"}:
        return mini_diff_from_unified(code, language=language, path=path)
    lines = (code or "").splitlines()
    return MiniDiff(
        path=path or "file",
        added=max(1, len(lines) or 1),
        removed=0,
        preview=_prefixed(lines, "add", "+"),
        full=code,
        language=language or language_from_path(path or ""),
        marker="+",
        action="create",
    )


def parse_file_card(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fenced:
        candidates.append(fenced.group(1).strip())
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            candidates.append(raw[start : end + 1])
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            row = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("path"), str):
            return row
    return None
