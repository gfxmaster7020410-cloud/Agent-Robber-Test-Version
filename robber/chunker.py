"""AST-aware code chunking. Python uses tree-sitter; other languages use regex."""

from __future__ import annotations

import re
from dataclasses import dataclass

from tree_sitter import Language, Parser

try:
    import tree_sitter_python as tspython

    _PY_PARSER = Parser(Language(tspython.language()))
except Exception:  # pragma: no cover - grammar optional
    _PY_PARSER = None

TS_HEAD = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:function|class|interface|type|const)\s+\w+",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Chunk:
    path: str
    kind: str
    start: int
    end: int
    text: str


def _slice_lines(text: str, start: int, end: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[start:end])


def _collect_python(path: str, source: str, node, chunks: list[Chunk]) -> None:
    if node.type in {"function_definition", "class_definition"}:
        start = node.start_point[0]
        end = node.end_point[0] + 1
        body = _slice_lines(source, start, end)
        if body.strip():
            kind = "class" if node.type == "class_definition" else "function"
            chunks.append(Chunk(path, kind, start + 1, end, body))
    for child in node.children:
        _collect_python(path, source, child, chunks)


def chunk_python(path: str, source: str) -> list[Chunk]:
    if _PY_PARSER is None:
        return chunk_windows(path, source, kind="python")
    tree = _PY_PARSER.parse(source.encode("utf-8"))
    chunks: list[Chunk] = []
    _collect_python(path, source, tree.root_node, chunks)
    preamble = _slice_lines(source, 0, min(40, source.count("\n") + 1))
    if preamble.strip():
        end = preamble.count("\n") + 1
        chunks.insert(0, Chunk(path, "module", 1, end, preamble))
    if len(chunks) <= 1:
        return chunk_windows(path, source, kind="python")
    return chunks


def chunk_windows(path: str, source: str, *, kind: str, size: int = 40) -> list[Chunk]:
    lines = source.splitlines()
    if not lines:
        return []
    chunks: list[Chunk] = []
    for start in range(0, len(lines), size):
        end = min(len(lines), start + size)
        body = "\n".join(lines[start:end])
        if body.strip():
            chunks.append(Chunk(path, kind, start + 1, end, body))
    return chunks


def chunk_typescript(path: str, source: str) -> list[Chunk]:
    matches = list(TS_HEAD.finditer(source))
    if not matches:
        return chunk_windows(path, source, kind="typescript")
    lines = source.splitlines()
    starts = [source[: m.start()].count("\n") for m in matches]
    chunks: list[Chunk] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        body = "\n".join(lines[start:end])
        if body.strip():
            chunks.append(Chunk(path, "symbol", start + 1, end, body))
    return chunks


def chunk_file(path: str, source: str) -> list[Chunk]:
    lower = path.lower()
    if lower.endswith(".py"):
        return chunk_python(path, source)
    if lower.endswith((".ts", ".tsx", ".js", ".jsx")):
        return chunk_typescript(path, source)
    return chunk_windows(path, source, kind="text", size=50)
