"""Token-budgeted AST context for swarm workers (Rust mmap first, Python AST fallback)."""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robber.config import AST_CONTEXT_TOKEN_CAP
from robber.jail import is_skipped_dir, workspace_root
from robber.memory.context_graph import extract_query_symbol
from robber.ssot_context import clip_to_token_budget, estimate_tokens

logger = logging.getLogger(__name__)

SKIP_DIRS = frozenset(
    {
        "node_modules",
        "dist",
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        ".robber",
        ".cursor",
        "target",
        ".pytest_cache",
        ".mypy_cache",
    }
)
_IDENT = re.compile(r"[A-Za-z_][\w]*")
MAX_FILES = 24
MAX_FILE_BYTES = 1_048_576
MAX_SYMBOLS = 8
MAX_SLICE_CHARS = 900


@dataclass
class ContextBlock:
    text: str
    tokens: int
    symbols: list[dict[str, Any]] = field(default_factory=list)
    engine: str = "python-ast"
    query: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "engine": self.engine,
            "tokens": self.tokens,
            "symbols": self.symbols[:MAX_SYMBOLS],
        }


class ASTContextExtractor:
    """Build lean LLM context from function/class nodes instead of whole files."""

    def __init__(self, root: Path | None = None, *, token_budget: int = AST_CONTEXT_TOKEN_CAP) -> None:
        self.root = (root or workspace_root()).resolve()
        self.token_budget = max(64, int(token_budget))

    def extract(self, task: str, files: list[str] | None = None, *, lang_filter: str | None = None) -> ContextBlock:
        query = extract_query_symbol(task) or (task or "").strip()
        symbols = self.search_symbols(query, lang_filter=lang_filter)
        if files:
            wanted = {str(item).replace("\\", "/") for item in files if str(item).strip()}
            preferred = [item for item in symbols if str(item.get("path") or "").replace("\\", "/") in wanted]
            if preferred:
                symbols = preferred + [item for item in symbols if item not in preferred]
        graph = self.references(query)
        parts: list[str] = []
        used: list[dict[str, Any]] = []
        for item in symbols[:MAX_SYMBOLS]:
            snippet = self._snippet(item)
            if not snippet:
                continue
            chunk = (
                f"{item.get('kind') or 'Symbol'} {item.get('name')} "
                f"{item.get('path')}:{item.get('line')}-{item.get('end_line')}\n"
                f"{item.get('signature') or ''}\n{snippet}"
            ).strip()
            parts.append(chunk)
            used.append(item)
        for label, rows in (("CALLERS", graph.get("callers") or []), ("CALLEES", graph.get("callees") or [])):
            names = [
                f"{row.get('name')} {row.get('path')}:{row.get('line')}"
                for row in rows[:4]
                if isinstance(row, dict)
            ]
            if names:
                parts.append(f"{label}: " + "; ".join(names))
        blob = clip_to_token_budget("\n\n".join(parts), self.token_budget)
        engine = str((used[0] or {}).get("engine") or graph.get("engine") or "python-ast") if used else str(
            graph.get("engine") or "python-ast"
        )
        return ContextBlock(
            text=blob,
            tokens=estimate_tokens(blob) if blob else 0,
            symbols=used,
            engine=engine,
            query=query,
        )

    def search_symbols(self, query: str, *, lang_filter: str | None = None) -> list[dict[str, Any]]:
        needle = (query or "").strip()
        if not needle:
            return []
        try:
            from robber.indexer_bridge import search_workspace_symbols

            rows = search_workspace_symbols(self.root, needle, lang_filter)
            if rows:
                for row in rows:
                    row["engine"] = "rust-ast"
                return rows
        except Exception:
            logger.debug("rust symbol search unavailable", exc_info=True)
        return self._python_search(needle, lang_filter)

    def references(self, symbol_name: str) -> dict[str, Any]:
        needle = (symbol_name or "").strip()
        if not needle:
            return {}
        try:
            from robber.indexer_bridge import find_symbol_references

            data = find_symbol_references(self.root, needle)
            if isinstance(data, dict) and (data.get("definitions") or data.get("callers")):
                data["engine"] = data.get("engine") or "rust-ast"
                return data
        except Exception:
            logger.debug("rust reference graph unavailable", exc_info=True)
        defs = self._python_search(needle, None)
        return {
            "symbol": needle,
            "definitions": defs,
            "callers": [],
            "callees": [],
            "engine": "python-ast",
        }

    def _snippet(self, item: dict[str, Any]) -> str:
        rel = str(item.get("path") or "")
        path = Path(rel)
        if not path.is_absolute():
            path = self.root / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        start_byte = int(item.get("start_byte") or 0)
        end_byte = int(item.get("end_byte") or 0)
        if 0 <= start_byte < end_byte <= len(text.encode("utf-8", errors="replace")):
            raw = text.encode("utf-8", errors="replace")[start_byte:end_byte].decode("utf-8", errors="replace")
            return raw[:MAX_SLICE_CHARS]
        start = max(1, int(item.get("line") or 1))
        end = max(start, int(item.get("end_line") or start))
        lines = text.splitlines()
        chunk = "\n".join(lines[start - 1 : end])
        return chunk[:MAX_SLICE_CHARS]

    def _python_search(self, query: str, lang_filter: str | None) -> list[dict[str, Any]]:
        if lang_filter and lang_filter.lower() not in {"python", "py", None}:
            if lang_filter.lower() not in {"python", "py"}:
                return []
        hits: list[dict[str, Any]] = []
        needle = query.lower()
        for path in _iter_py(self.root):
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                source = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
            except (OSError, SyntaxError):
                continue
            rel = path.relative_to(self.root).as_posix()
            for node, kind, owner in _iter_symbols(tree):
                name = str(getattr(node, "name", "") or "")
                if not name:
                    continue
                score = _fuzzy(needle, name.lower())
                if owner:
                    score = max(score, _fuzzy(needle, owner.lower()))
                if score == 0:
                    continue
                start = int(getattr(node, "lineno", 1) or 1)
                end = int(getattr(node, "end_lineno", start) or start)
                hits.append(
                    {
                        "name": f"{owner}.{name}" if owner and kind == "Method" else name,
                        "kind": kind,
                        "path": rel,
                        "line": start,
                        "end_line": end,
                        "start_byte": 0,
                        "end_byte": 0,
                        "signature": ast.get_source_segment(source, node) or "",
                        "docstring": (ast.get_docstring(node) or "")[:240],
                        "language": "python",
                        "score": score,
                        "engine": "python-ast",
                    }
                )
            if len(hits) >= 64:
                break
        hits.sort(key=lambda row: (-int(row.get("score") or 0), str(row.get("path") or "")))
        return hits[:40]


def _iter_py(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in SKIP_DIRS or is_skipped_dir(part) for part in path.parts):
            continue
        out.append(path)
        if len(out) >= MAX_FILES:
            break
    return out


def _iter_symbols(tree: ast.AST) -> list[tuple[ast.AST, str, str]]:
    rows: list[tuple[ast.AST, str, str]] = []
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, ast.ClassDef):
            rows.append((node, "Class", ""))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    rows.append((item, "Method", node.name))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            rows.append((node, "Function", ""))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            rows.append((node, "Import", ""))
    return rows


def _fuzzy(query: str, name: str) -> int:
    if not query or not name:
        return 0
    if name == query:
        return 100
    if name.startswith(query):
        return 90
    if query in name:
        return 70
    return 0
