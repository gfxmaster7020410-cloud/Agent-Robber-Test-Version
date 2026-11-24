"""Parse pytest, go test, cargo test, and Jest traces into repair targets."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PY_FILE_LINE = re.compile(r'File "([^"]+)", line (\d+)(?:, in ([^\n]+))?')
PYTEST_NODE = re.compile(
    r"^(?:ERROR|FAILED)\s+(\S+\.py)(?:::(\S+))?(?:\s+-\s+(.+))?$",
    re.MULTILINE,
)
PYTEST_SHORT = re.compile(
    r"^((?:[A-Za-z]:)?[^:\n]+\.py):(\d+):\s*(?:in\s+(\S+))?.*$",
    re.MULTILINE,
)
PYTEST_ASSERT = re.compile(r"^E\s+(assert\s+.+)$", re.MULTILINE)
E_ASSERT_EQ = re.compile(r"^E\s+assert\s+(.+?)\s*==\s+(.+?)\s*$", re.MULTILINE)
FAILED_ASSERT_EQ = re.compile(r"\bassert\s+(.+?)\s*==\s+(.+?)\s*$")
GO_FAIL = re.compile(r"^--- FAIL:\s+(\S+)", re.MULTILINE)
GO_FRAME = re.compile(r"^\s*((?:[A-Za-z]:)?[^:\n]+\.go):(\d+):\s*(.*)$", re.MULTILINE)
GO_GOT_WANT = re.compile(r"\bgot\s+(\S+)\s+want\s+(\S+)", re.IGNORECASE)
CARGO_TEST = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+FAILED", re.MULTILINE)
CARGO_PANIC = re.compile(
    r"panicked at(?: '([^']*)')?\s*((?:[A-Za-z]:)?[^:\n]+\.rs):(\d+):(\d+)",
)
CARGO_ARROW = re.compile(r"-->\s*((?:[A-Za-z]:)?[^:\n]+\.rs):(\d+):(\d+)")
CARGO_LEFT = re.compile(r"left:\s*`([^`]*)`")
CARGO_RIGHT = re.compile(r"right:\s*`([^`]*)`")
JEST_FAIL = re.compile(r"^\s*FAIL\s+(\S+)", re.MULTILINE)
JEST_TITLE = re.compile(r"^\s*●\s+(.+)$", re.MULTILINE)
JEST_FRAME = re.compile(
    r"at\s+(?:(\S+)\s+)?\(((?:[A-Za-z]:)?[^:\n]+):(\d+):(\d+)\)",
)
JEST_EXPECT = re.compile(r"^\s*Expected:\s*(.+)$", re.MULTILINE)
JEST_RECEIVED = re.compile(r"^\s*Received:\s*(.+)$", re.MULTILINE)
ASSERT_ERR = re.compile(r"AssertionError(?::\s*(.+))?")


@dataclass
class TestFailure:
    path: str = ""
    line: int = 0
    test_name: str = ""
    assertion: str = ""
    expected: str = ""
    received: str = ""
    frames: list[dict[str, Any]] = field(default_factory=list)
    runner: str = ""
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class TracebackParser:
    """Pinpoint file, line, assertion, and stack frames from framework traces."""

    def parse(self, text: str, *, root: Path | None = None) -> TestFailure:
        blob = text or ""
        hit = (
            self._pytest(blob)
            or self._go(blob)
            or self._cargo(blob)
            or self._jest(blob)
            or TestFailure(message=blob[-400:], runner="unknown")
        )
        hit.path = _rel_path(hit.path, root)
        cleaned: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for frame in hit.frames:
            path = _rel_path(str(frame.get("path") or ""), root)
            line = int(frame.get("line") or 0)
            key = (path, line)
            if not path or key in seen:
                continue
            seen.add(key)
            cleaned.append(
                {
                    "path": path,
                    "line": line,
                    "name": str(frame.get("name") or ""),
                }
            )
        hit.frames = cleaned[:16]
        if not hit.path and hit.frames:
            hit.path = str(hit.frames[-1].get("path") or "")
            if not hit.line:
                hit.line = int(hit.frames[-1].get("line") or 0)
        if not hit.assertion:
            hit.assertion = (hit.message or "")[:240]
        return hit

    def _pytest(self, blob: str) -> TestFailure | None:
        if "pytest" not in blob.lower() and "AssertionError" not in blob and "E       assert" not in blob:
            if not PYTEST_NODE.search(blob) and not PYTEST_SHORT.search(blob):
                return None
        hit = TestFailure(runner="pytest")
        node = PYTEST_NODE.search(blob)
        if node:
            hit.path = node.group(1).replace("\\", "/")
            hit.test_name = (node.group(2) or "").strip()
            hit.message = (node.group(3) or "").strip()
        assertion = PYTEST_ASSERT.search(blob)
        if assertion:
            hit.assertion = assertion.group(1).strip()
        pair = E_ASSERT_EQ.search(blob)
        if pair:
            hit.received = pair.group(1).strip()
            hit.expected = pair.group(2).strip()
        elif hit.assertion:
            fallback = FAILED_ASSERT_EQ.search(hit.assertion)
            if fallback:
                hit.received = fallback.group(1).strip()
                hit.expected = fallback.group(2).strip()
        elif hit.message:
            fallback = FAILED_ASSERT_EQ.search(hit.message)
            if fallback:
                hit.received = fallback.group(1).strip()
                hit.expected = fallback.group(2).strip()
        err = ASSERT_ERR.search(blob)
        if err and err.group(1) and not hit.message:
            hit.message = err.group(1).strip()
        for match in PY_FILE_LINE.finditer(blob):
            hit.frames.append(
                {
                    "path": match.group(1).replace("\\", "/"),
                    "line": int(match.group(2)),
                    "name": (match.group(3) or "").strip(),
                }
            )
        for match in PYTEST_SHORT.finditer(blob):
            path = match.group(1).replace("\\", "/")
            line = int(match.group(2))
            name = (match.group(3) or "").strip()
            hit.frames.append({"path": path, "line": line, "name": name})
            if not hit.path:
                hit.path = path
            if not hit.line:
                hit.line = line
            if name and not hit.test_name:
                hit.test_name = name
        if hit.frames and not hit.line:
            last = hit.frames[-1]
            hit.path = hit.path or str(last.get("path") or "")
            hit.line = int(last.get("line") or 0)
            if not hit.test_name:
                hit.test_name = str(last.get("name") or "")
        if hit.path or hit.test_name or hit.assertion:
            return hit
        return None

    def _go(self, blob: str) -> TestFailure | None:
        fail = GO_FAIL.search(blob)
        frames = list(GO_FRAME.finditer(blob))
        if fail is None and not frames:
            return None
        hit = TestFailure(runner="go")
        if fail:
            hit.test_name = fail.group(1)
        for match in frames:
            hit.frames.append(
                {
                    "path": match.group(1).replace("\\", "/"),
                    "line": int(match.group(2)),
                    "name": hit.test_name,
                }
            )
            hit.path = match.group(1).replace("\\", "/")
            hit.line = int(match.group(2))
            hit.assertion = match.group(3).strip()
            hit.message = hit.assertion
        got = GO_GOT_WANT.search(blob)
        if got:
            hit.received = got.group(1)
            hit.expected = got.group(2)
        return hit

    def _cargo(self, blob: str) -> TestFailure | None:
        test = CARGO_TEST.search(blob)
        panic = CARGO_PANIC.search(blob)
        arrow = CARGO_ARROW.search(blob)
        if test is None and panic is None and arrow is None:
            if "panicked at" not in blob and "FAILED" not in blob:
                return None
            if ".rs:" not in blob:
                return None
        hit = TestFailure(runner="cargo")
        if test:
            hit.test_name = test.group(1)
        if panic:
            hit.message = (panic.group(1) or "").strip()
            hit.path = panic.group(2).replace("\\", "/")
            hit.line = int(panic.group(3))
            hit.frames.append({"path": hit.path, "line": hit.line, "name": hit.test_name})
        elif arrow:
            hit.path = arrow.group(1).replace("\\", "/")
            hit.line = int(arrow.group(2))
            hit.frames.append({"path": hit.path, "line": hit.line, "name": hit.test_name})
        left = CARGO_LEFT.search(blob)
        right = CARGO_RIGHT.search(blob)
        if left:
            hit.received = left.group(1)
        if right:
            hit.expected = right.group(1)
        hit.assertion = hit.message or (f"left `{hit.received}` right `{hit.expected}`".strip())
        return hit

    def _jest(self, blob: str) -> TestFailure | None:
        fail = JEST_FAIL.search(blob)
        frames = list(JEST_FRAME.finditer(blob))
        if fail is None and "expect(" not in blob and not frames:
            return None
        if fail is None and not frames:
            return None
        hit = TestFailure(runner="jest")
        if fail:
            hit.path = fail.group(1).replace("\\", "/")
        title = JEST_TITLE.search(blob)
        if title:
            hit.test_name = title.group(1).strip()
        expected = JEST_EXPECT.search(blob)
        received = JEST_RECEIVED.search(blob)
        if expected:
            hit.expected = expected.group(1).strip()
        if received:
            hit.received = received.group(1).strip()
        hit.assertion = f"Expected {hit.expected} Received {hit.received}".strip()
        for match in frames:
            path = match.group(2).replace("\\", "/")
            line = int(match.group(3))
            hit.frames.append(
                {
                    "path": path,
                    "line": line,
                    "name": (match.group(1) or hit.test_name).strip(),
                }
            )
            hit.path = path
            hit.line = line
        return hit


def _rel_path(raw: str, root: Path | None) -> str:
    text = (raw or "").strip().replace("\\", "/")
    if not text:
        return ""
    try:
        path = Path(text)
        if root is not None:
            base = root.resolve()
            candidate = path if path.is_absolute() else (base / path)
            try:
                return candidate.resolve().relative_to(base).as_posix()
            except (ValueError, OSError):
                return path.as_posix().lstrip("/")
        return path.as_posix()
    except OSError:
        return text
