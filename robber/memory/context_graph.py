"""Precision AST context: Rust symbol graph first, Python mmap fallback."""

from __future__ import annotations

import ast
import json
import logging
import mmap
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robber.config import GRAPH_PROMPT_TOKEN_CAP, GRAPH_SLICE_TOKEN_CAP
from robber.jail import is_skipped_dir, workspace_root
from robber.ssot_context import estimate_tokens

logger = logging.getLogger(__name__)

_GRAPH_CACHE: dict[tuple[str, str, int], dict[str, Any]] = {}
_GRAPH_CACHE_MAX = 8


def clear_graph_cache() -> None:
    _GRAPH_CACHE.clear()

MAX_PARSE_FILES = 24
MAX_FILE_BYTES = 1_048_576
MAX_SLICE_CHARS = 900
MAX_SLICES = 8
DEPTH_CAP = 4
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
_STOP = frozenset(
    {
        "where",
        "what",
        "when",
        "why",
        "how",
        "the",
        "this",
        "that",
        "these",
        "those",
        "used",
        "across",
        "codebase",
        "instantiated",
        "and",
        "for",
        "with",
        "from",
        "into",
        "is",
        "are",
        "was",
        "were",
        "does",
        "did",
        "not",
        "any",
        "all",
    }
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUOTED = re.compile(r"""[`'"]([A-Za-z_][A-Za-z0-9_]*)[`'"]""")
_DOCSTRING = re.compile(
    r'(?P<prefix>^[ \t]*)(?P<q>"""|\'\'\')(?:[\s\S]*?)(?P=q)',
    re.MULTILINE,
)


def extract_query_symbol(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    quoted = _QUOTED.search(raw)
    if quoted:
        return quoted.group(1)
    if _IDENT.fullmatch(raw) and raw.lower() not in _STOP:
        return raw
    best = ""
    best_score = -1
    for tok in _IDENT.findall(raw):
        if tok.lower() in _STOP:
            continue
        score = _ident_score(tok)
        if score > best_score:
            best = tok
            best_score = score
    return best


def _ident_score(tok: str) -> int:
    if not tok:
        return -1
    n = len(tok)
    internals = sum(1 for c in tok[1:] if c.isupper())
    if tok[0].isupper() and internals >= 1:
        return 2000 + n + internals * 10
    if "_" in tok:
        return 800 + n
    if tok[0].isupper() and tok[1:].islower():
        return 20 + n
    return n


@dataclass
class PackedGraph:
    query: str = ""
    depth: int = 0
    engine: str = "python"
    elapsed_ms: float = 0.0
    elapsed_us: int = 0
    tokens: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    text: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "depth": self.depth,
            "engine": self.engine,
            "elapsed_ms": self.elapsed_ms,
            "elapsed_us": self.elapsed_us,
            "tokens": self.tokens,
        }


class ContextGraphEngine:
    """Ask the Rust indexer for pinpoint slices, then clip to a token budget."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        token_budget: int = GRAPH_SLICE_TOKEN_CAP,
        prompt_budget: int = GRAPH_PROMPT_TOKEN_CAP,
    ) -> None:
        self.root = Path(root).resolve() if root else workspace_root()
        self.token_budget = max(256, int(token_budget))
        self.prompt_budget = max(256, int(prompt_budget))

    def get_symbol_context(self, query_symbol: str, depth: int = 2) -> dict[str, Any]:
        started = time.perf_counter()
        query = extract_query_symbol(query_symbol) or (query_symbol or "").strip()
        hops = max(0, min(int(depth), DEPTH_CAP))
        cache_key = (str(self.root), query or "", hops)
        cached = _GRAPH_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)
        payload: dict[str, Any] | None = None
        try:
            from robber.indexer_bridge import get_symbol_context

            raw = get_symbol_context(self.root, query_symbol, hops)
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            logger.debug("rust symbol context unavailable", exc_info=False)
        if payload is None:
            payload = self._python_graph(query, hops, started)
        payload.setdefault("query", query)
        payload.setdefault("depth", hops)
        payload.setdefault("engine", payload.get("engine") or "python")
        if "elapsed_ms" not in payload:
            elapsed = time.perf_counter() - started
            payload["elapsed_ms"] = elapsed * 1000.0
            payload["elapsed_us"] = int(elapsed * 1_000_000)
        if len(_GRAPH_CACHE) >= _GRAPH_CACHE_MAX:
            _GRAPH_CACHE.pop(next(iter(_GRAPH_CACHE)))
        _GRAPH_CACHE[cache_key] = dict(payload)
        return payload

    def query(self, query_symbol: str, depth: int = 2, *, budget: int | None = None) -> PackedGraph:
        payload = self.get_symbol_context(query_symbol, depth)
        cap = max(256, int(budget or self.token_budget))
        text = self.allocate(self._format(payload), cap)
        packed = PackedGraph(
            query=str(payload.get("query") or extract_query_symbol(query_symbol)),
            depth=int(payload.get("depth") or depth),
            engine=str(payload.get("engine") or "python"),
            elapsed_ms=float(payload.get("elapsed_ms") or 0.0),
            elapsed_us=int(payload.get("elapsed_us") or 0),
            tokens=estimate_tokens(text),
            payload=payload,
            text=text,
        )
        return packed

    def prompt_slice(self, query_symbol: str, depth: int = 2) -> PackedGraph:
        return self.query(query_symbol, depth, budget=self.prompt_budget)

    def worker_context(
        self,
        query_symbol: str,
        *,
        files: list[str] | None = None,
        depth: int = 1,
    ) -> PackedGraph:
        parts = [query_symbol or ""]
        for rel in files or []:
            stem = Path(str(rel)).stem
            if stem and stem not in parts:
                parts.append(stem)
        blob = " ".join(part for part in parts if part).strip()
        return self.query(blob, depth, budget=min(self.token_budget, GRAPH_SLICE_TOKEN_CAP))

    def allocate(self, text: str, max_tokens: int) -> str:
        cap = max(1, int(max_tokens))
        blob = text or ""
        limit = cap * 4
        if len(blob) > limit:
            blob = blob[:limit].rstrip()
        while estimate_tokens(blob) > cap and len(blob) > 8:
            blob = blob[: max(8, len(blob) - 8)].rstrip()
        return blob

    def prune_boilerplate(self, source: str) -> str:
        text = source or ""
        if not text.strip():
            return ""
        try:
            tree = ast.parse(text)
            self._strip_docs(tree)
            self._drop_unused_imports(tree)
            text = ast.unparse(tree)
        except SyntaxError:
            text = _DOCSTRING.sub("", text, count=3)
            text = self._drop_unused_import_lines(text)
        lines = [line for line in text.splitlines() if line.strip() != "pass" or text.count("\n") < 4]
        return "\n".join(lines).strip()

    def _format(self, payload: dict[str, Any]) -> str:
        query = str(payload.get("query") or "")
        lines = [
            f"AST graph {query} depth={payload.get('depth')} "
            f"engine={payload.get('engine')} elapsed_ms={float(payload.get('elapsed_ms') or 0):.3f}"
        ]
        for label, key in (
            ("DEFINITIONS", "definitions"),
            ("CALLERS", "callers"),
            ("CALLEES", "callees"),
        ):
            rows = payload.get(key) if isinstance(payload.get(key), list) else []
            if not rows:
                continue
            lines.append(label)
            for row in rows[:12]:
                if not isinstance(row, dict):
                    continue
                sig = str(row.get("signature") or "").strip()
                lines.append(
                    f"- {row.get('path')}:{row.get('line')} {row.get('kind')} "
                    f"{row.get('name')} {sig}".rstrip()
                )
        imports = payload.get("imports") if isinstance(payload.get("imports"), list) else []
        if imports:
            lines.append("IMPORTS " + ", ".join(str(item) for item in imports[:12]))
        sites = payload.get("callsites") if isinstance(payload.get("callsites"), list) else []
        if sites:
            lines.append("CALLSITES")
            for row in sites[:12]:
                if isinstance(row, dict):
                    lines.append(
                        f"- {row.get('path')}:{row.get('line')} {str(row.get('snippet') or '')[:80]}"
                    )
        slices = payload.get("slices") if isinstance(payload.get("slices"), list) else []
        for item in slices[:MAX_SLICES]:
            if not isinstance(item, dict):
                continue
            snippet = self.prune_boilerplate(str(item.get("snippet") or ""))
            if not snippet:
                snippet = str(item.get("snippet") or "")[:MAX_SLICE_CHARS]
            lines.append(
                f"SLICE {item.get('path')}:{item.get('line')}-{item.get('end_line')} "
                f"{item.get('kind')} {item.get('name')}"
            )
            lines.append(snippet[:MAX_SLICE_CHARS])
        return "\n".join(lines)

    def _python_graph(self, query: str, depth: int, started: float) -> dict[str, Any]:
        if not query:
            elapsed = time.perf_counter() - started
            return _empty_payload(query, depth, elapsed)
        files = self._hit_files(query.encode("utf-8"))
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, str]] = []
        imports: list[str] = []
        heritage: list[dict[str, str]] = []
        def_lines: set[tuple[str, int]] = set()
        for rel, path in files:
            self._parse_file(rel, path, query, nodes, edges, imports, heritage, def_lines)
        name_to_id: dict[str, str] = {}
        for nid, node in nodes.items():
            name_to_id.setdefault(str(node.get("name") or ""), nid)
        for edge in edges:
            target = edge.get("to") or ""
            if target not in nodes and target in name_to_id:
                edge["to"] = name_to_id[target]
        seeds = [nid for nid, node in nodes.items() if node.get("name") == query]
        keep = _bfs(seeds, edges, depth)
        definitions = [nodes[i] for i in seeds if i in nodes]
        callers: list[dict[str, Any]] = []
        callees: list[dict[str, Any]] = []
        seed_set = set(seeds)
        kept_edges: list[dict[str, str]] = []
        for edge in edges:
            if edge["from"] not in keep and edge["to"] not in keep:
                continue
            if edge["from"] in keep and edge["to"] in keep:
                kept_edges.append(edge)
            if edge["kind"] != "call":
                continue
            if edge["to"] in seed_set or edge["to"] == query or str(edge["to"]).endswith(f"::{query}"):
                if edge["from"] in nodes:
                    callers.append(nodes[edge["from"]])
            if edge["from"] in seed_set:
                if edge["to"] in nodes:
                    callees.append(nodes[edge["to"]])
                else:
                    callees.append(
                        {
                            "id": edge["to"],
                            "name": edge["to"],
                            "kind": "call",
                            "path": "",
                            "line": 0,
                            "signature": "",
                        }
                    )
        callsites: list[dict[str, Any]] = []
        for rel, path in files:
            for line_no, snippet in _mmap_line_hits(path, query.encode("utf-8")):
                if (rel, line_no) in def_lines:
                    continue
                callsites.append({"path": rel, "line": line_no, "snippet": snippet})
                if len(callsites) >= 16:
                    break
            if len(callsites) >= 16:
                break
        slices: list[dict[str, Any]] = []
        ranked = sorted(
            (n for n in nodes.values() if n.get("id") in keep or n.get("name") == query),
            key=lambda n: (0 if n.get("name") == query else 1, n.get("path"), n.get("line")),
        )
        for node in ranked:
            if node.get("kind") == "module":
                continue
            snippet = prune_slice_text(
                _mmap_line_range(
                    self.root / str(node.get("path") or ""),
                    int(node.get("line") or 1),
                    int(node.get("end_line") or node.get("line") or 1),
                    MAX_SLICE_CHARS,
                )
            )
            if not snippet.strip():
                continue
            slices.append(
                {
                    "path": node.get("path"),
                    "name": node.get("name"),
                    "kind": node.get("kind"),
                    "line": node.get("line"),
                    "end_line": node.get("end_line"),
                    "signature": node.get("signature") or "",
                    "snippet": snippet,
                }
            )
            if len(slices) >= MAX_SLICES:
                break
        elapsed = time.perf_counter() - started
        payload = {
            "query": query,
            "depth": depth,
            "engine": "python",
            "elapsed_ms": elapsed * 1000.0,
            "elapsed_us": int(elapsed * 1_000_000),
            "definitions": definitions[:12],
            "callers": _dedupe_locs(callers)[:12],
            "callees": _dedupe_locs(callees)[:12],
            "imports": sorted(set(imports))[:16],
            "heritage": heritage[:8],
            "callsites": callsites[:16],
            "slices": slices,
            "edges": kept_edges[:32],
            "tokens": 0,
        }
        blob = json.dumps(payload, ensure_ascii=True)
        payload["tokens"] = max(1, (len(blob) + 3) // 4)
        return payload

    def _hit_files(self, needle: bytes) -> list[tuple[str, Path]]:
        out: list[tuple[str, Path]] = []
        if not needle:
            return out
        root = self.root
        bases = [root / "robber", root / "tests"]
        if not any(base.is_dir() for base in bases):
            bases = [root]
        for base in bases:
            if not base.is_dir():
                continue
            for ext in ("*.py", "*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs", "*.go", "*.rs"):
                for path in base.rglob(ext):
                    if not path.is_file():
                        continue
                    if any(part in SKIP_DIRS or is_skipped_dir(part) for part in path.parts):
                        continue
                    try:
                        if path.stat().st_size > MAX_FILE_BYTES:
                            continue
                    except OSError:
                        continue
                    if not _mmap_contains(path, needle):
                        continue
                    rel = path.relative_to(root).as_posix()
                    out.append((rel, path))
                    if len(out) >= MAX_PARSE_FILES:
                        return out
        return out

    def _parse_file(
        self,
        rel: str,
        path: Path,
        query: str,
        nodes: dict[str, dict[str, Any]],
        edges: list[dict[str, str]],
        imports: list[str],
        heritage: list[dict[str, str]],
        def_lines: set[tuple[str, int]],
    ) -> ast.AST | None:
        if path.suffix.lower() != ".py":
            return None
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        if len(source) > MAX_FILE_BYTES:
            return None
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            return None
        mod_id = f"{rel}::__module__"
        nodes.setdefault(
            mod_id,
            {
                "id": mod_id,
                "name": rel,
                "kind": "module",
                "path": rel,
                "line": 1,
                "end_line": 1,
                "signature": "",
            },
        )
        current = ["__module__"]

        def add_symbol(name: str, kind: str, node: ast.AST, signature: str) -> str:
            nid = f"{rel}::{name}"
            if nid not in nodes:
                line = int(getattr(node, "lineno", 1) or 1)
                end = int(getattr(node, "end_lineno", line) or line)
                nodes[nid] = {
                    "id": nid,
                    "name": name,
                    "kind": kind,
                    "path": rel,
                    "line": line,
                    "end_line": end,
                    "signature": signature[:160],
                }
                if name == query:
                    def_lines.add((rel, line))
            return nid

        class Visitor(ast.NodeVisitor):
            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                nid = add_symbol(node.name, "class", node, f"class {node.name}")
                for base in node.bases:
                    parent = _expr_name(base)
                    if parent:
                        edges.append({"from": nid, "to": parent, "kind": "inherit"})
                        heritage.append({"child": nid, "parent": parent})
                current.append(node.name)
                self.generic_visit(node)
                current.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._fn(node, "function")

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._fn(node, "function")

            def _fn(self, node: ast.AST, kind: str) -> None:
                name = str(getattr(node, "name", "") or "")
                nid = add_symbol(name, kind, node, f"def {name}")
                current.append(name)
                self.generic_visit(node)
                current.pop()
                _ = nid

            def visit_Call(self, node: ast.Call) -> None:
                callee = _expr_name(node.func)
                caller = current[-1] if current else "__module__"
                if callee:
                    src = f"{rel}::{caller}" if caller != "__module__" else mod_id
                    edges.append({"from": src, "to": callee, "kind": "call"})
                    target = f"{rel}::{callee}"
                    if target in nodes:
                        edges[-1]["to"] = target
                self.generic_visit(node)

            def visit_Import(self, node: ast.Import) -> None:
                for alias in node.names:
                    imports.append(alias.name)
                    edges.append({"from": mod_id, "to": alias.name, "kind": "import"})

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                mod = node.module or ""
                if mod:
                    imports.append(mod)
                    edges.append({"from": mod_id, "to": mod, "kind": "import"})

        Visitor().visit(tree)
        return tree

    def _strip_docs(self, tree: ast.AST) -> None:
        if isinstance(tree, ast.Module):
            body: list[ast.stmt] = []
            dropped = False
            for stmt in tree.body:
                if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    body.append(stmt)
                    continue
                if (
                    not dropped
                    and isinstance(stmt, ast.Expr)
                    and isinstance(getattr(stmt, "value", None), ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    dropped = True
                    continue
                dropped = True
                body.append(stmt)
            tree.body = body or [ast.Pass()]
        for node in ast.walk(tree):
            if isinstance(node, ast.Module):
                continue
            body = getattr(node, "body", None)
            if not isinstance(body, list) or not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(getattr(first, "value", None), ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]

    def _drop_unused_imports(self, tree: ast.Module) -> None:
        used = _load_names(tree)
        keep: list[ast.stmt] = []
        for stmt in tree.body:
            if isinstance(stmt, ast.Import):
                stmt.names = [alias for alias in stmt.names if (alias.asname or alias.name.split(".", 1)[0]) in used]
                if stmt.names:
                    keep.append(stmt)
                continue
            if isinstance(stmt, ast.ImportFrom):
                if stmt.module == "__future__":
                    keep.append(stmt)
                    continue
                stmt.names = [
                    alias
                    for alias in stmt.names
                    if alias.name == "*" or (alias.asname or alias.name) in used
                ]
                if stmt.names:
                    keep.append(stmt)
                continue
            keep.append(stmt)
        tree.body = keep or [ast.Pass()]

    def _drop_unused_import_lines(self, text: str) -> str:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return text
        used = _load_names(tree)
        out: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("from __future__"):
                out.append(line)
                continue
            if stripped.startswith("import ") or stripped.startswith("from "):
                names = _IDENT.findall(stripped)
                if names and not any(name in used for name in names if name not in {"from", "import", "as"}):
                    continue
            out.append(line)
        return "\n".join(out)


def _empty_payload(query: str, depth: int, elapsed: float) -> dict[str, Any]:
    return {
        "query": query,
        "depth": depth,
        "engine": "python",
        "elapsed_ms": elapsed * 1000.0,
        "elapsed_us": int(elapsed * 1_000_000),
        "definitions": [],
        "callers": [],
        "callees": [],
        "imports": [],
        "heritage": [],
        "callsites": [],
        "slices": [],
        "edges": [],
        "tokens": 0,
    }


def _expr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _load_names(tree: ast.AST) -> set[str]:
    used: set[str] = set()

    class Collector(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load):
                used.add(node.id)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            self.visit(node.value)

        def visit_Import(self, node: ast.Import) -> None:
            return

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            return

    Collector().visit(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            used.add(node.name)
    return used


def _dedupe_locs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = str(row.get("id") or row.get("name") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _bfs(seeds: list[str], edges: list[dict[str, str]], depth: int) -> set[str]:
    seen: set[str] = set(seeds)
    q: deque[tuple[str, int]] = deque((s, 0) for s in seeds)
    while q:
        nid, hop = q.popleft()
        if hop >= depth:
            continue
        for edge in edges:
            nxt = edge["to"] if edge["from"] == nid else edge["from"] if edge["to"] == nid else ""
            if not nxt or nxt in seen:
                continue
            seen.add(nxt)
            q.append((nxt, hop + 1))
            if len(seen) >= 40:
                return seen
    return seen


def _mmap_contains(path: Path, needle: bytes) -> bool:
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, 2)
            if size <= 0 or size > 8_000_000:
                return False
            fh.seek(0)
            if size <= 4096:
                return needle in fh.read()
            mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                return mm.find(needle) >= 0
            finally:
                mm.close()
    except OSError:
        return False


def _mmap_line_hits(path: Path, needle: bytes) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, 2)
            if size <= 0 or size > 8_000_000:
                return out
            fh.seek(0)
            data = fh.read() if size <= 4096 else None
            if data is None:
                mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
                try:
                    data = mm[:]
                finally:
                    mm.close()
    except OSError:
        return out
    if not data:
        return out
    start = 0
    line_no = 1
    while True:
        idx = data.find(needle, start)
        if idx < 0:
            break
        line_no = data.count(b"\n", 0, idx) + 1
        lo = data.rfind(b"\n", 0, idx) + 1
        hi = data.find(b"\n", idx)
        if hi < 0:
            hi = len(data)
        snippet = data[lo:hi].decode("utf-8", "replace").strip()[:80]
        out.append((line_no, snippet))
        start = idx + len(needle)
        if len(out) >= 16:
            break
    return out


def _mmap_line_range(path: Path, start_line: int, end_line: int, max_chars: int) -> str:
    if not path.is_file():
        return ""
    start_line = max(1, int(start_line))
    end_line = max(start_line, int(end_line))
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, 2)
            if size <= 0 or size > 8_000_000:
                return ""
            fh.seek(0)
            if size <= 4096:
                data = fh.read()
            else:
                mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
                try:
                    starts = [0]
                    for i, b in enumerate(mm):
                        if b == 10 and i + 1 < len(mm):
                            starts.append(i + 1)
                    lo = starts[min(start_line - 1, len(starts) - 1)]
                    hi = starts[end_line] if end_line < len(starts) else len(mm)
                    hi = min(hi, lo + max_chars)
                    chunk = mm[lo:hi]
                finally:
                    mm.close()
                return prune_slice_text(chunk.decode("utf-8", "replace"))
    except OSError:
        return ""
    starts = [0]
    for i, b in enumerate(data):
        if b == 10 and i + 1 < len(data):
            starts.append(i + 1)
    lo = starts[min(start_line - 1, len(starts) - 1)]
    hi = starts[end_line] if end_line < len(starts) else len(data)
    hi = min(hi, lo + max_chars)
    return prune_slice_text(data[lo:hi].decode("utf-8", "replace"))


def prune_slice_text(raw: str) -> str:
    lines = list((raw or "").splitlines())
    while lines:
        t = lines[0].strip()
        if not t or t.startswith("#") or t.startswith("//"):
            lines.pop(0)
            continue
        if t.startswith('"""') or t.startswith("'''"):
            opener = '"""' if t.startswith('"""') else "'''"
            one = t.count(opener) >= 2 and len(t) > 6
            lines.pop(0)
            if not one:
                while lines:
                    done = opener in lines[0]
                    lines.pop(0)
                    if done:
                        break
            continue
        break
    blob = "\n".join(lines)
    return blob[:MAX_SLICE_CHARS]
