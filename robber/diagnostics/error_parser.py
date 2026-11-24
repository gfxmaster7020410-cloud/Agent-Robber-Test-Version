"""Parse compiler, test, and tool traces into a repair target."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

TRACE_FILE = re.compile(r'File "([^"]+)", line (\d+)')
PY_SYNTAX = re.compile(r"SyntaxError:\s*(.+)")
INDENT = re.compile(r"IndentationError:\s*(.+)")
MODULE_MISSING = re.compile(r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]")
NAME_MISSING = re.compile(r"NameError: name ['\"]([^'\"]+)['\"] is not defined")
TEST_FAIL = re.compile(r"\b(FAILED|ERROR|AssertionError|E\s+assert)\b")
HEALABLE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}
BLOCK_START = re.compile(
    r"^(\s*)(def|async def|class|if|elif|else|for|while|try|except|finally|with)\b"
)


@dataclass
class ParsedError:
    path: Path | None = None
    line_no: int = 0
    kind: str = "unknown"
    message: str = ""
    missing_module: str = ""
    missing_name: str = ""
    test_failure: bool = False
    raw: str = ""
    facts: list[str] = field(default_factory=list)

    @property
    def healable(self) -> bool:
        return self.path is not None and self.path.suffix.lower() in HEALABLE_SUFFIXES


def parse_error_log(trace: str, *, root: Path | None = None) -> ParsedError:
    text = trace or ""
    parsed = ParsedError(raw=text[-8000:])
    syntax = PY_SYNTAX.search(text)
    indent = INDENT.search(text)
    missing = MODULE_MISSING.search(text)
    name = NAME_MISSING.search(text)
    if syntax:
        parsed.kind = "syntax"
        parsed.message = syntax.group(1).strip()
    elif indent:
        parsed.kind = "indent"
        parsed.message = indent.group(1).strip()
    elif missing:
        parsed.kind = "import"
        parsed.missing_module = missing.group(1).strip()
        parsed.message = f"missing module {parsed.missing_module}"
    elif name:
        parsed.kind = "name"
        parsed.missing_name = name.group(1).strip()
        parsed.message = f"undefined name {parsed.missing_name}"
    if TEST_FAIL.search(text):
        parsed.test_failure = True
        if parsed.kind == "unknown":
            parsed.kind = "test"
            parsed.message = parsed.message or "test failure"
    best: tuple[Path, int] | None = None
    for match in TRACE_FILE.finditer(text):
        candidate = _resolve_frame(match.group(1), root)
        if candidate is None:
            continue
        best = (candidate, int(match.group(2)))
    if best is not None:
        parsed.path = best[0]
        parsed.line_no = best[1]
    parsed.facts = _facts(parsed)
    return parsed


def _resolve_frame(raw: str, root: Path | None) -> Path | None:
    try:
        candidate = Path(raw)
        if not candidate.is_absolute():
            if root is None:
                return None
            candidate = (root / raw).resolve()
        else:
            candidate = candidate.resolve()
        if root is not None:
            candidate.relative_to(root.resolve())
        if candidate.suffix.lower() not in HEALABLE_SUFFIXES:
            return None
        if not candidate.is_file():
            return None
        return candidate
    except (ValueError, OSError):
        return None


def _facts(parsed: ParsedError) -> list[str]:
    out: list[str] = []
    if parsed.path is not None:
        out.append(f"file={parsed.path.name}")
    if parsed.line_no:
        out.append(f"line={parsed.line_no}")
    if parsed.kind != "unknown":
        out.append(f"kind={parsed.kind}")
    if parsed.message:
        out.append(parsed.message[:120])
    return out


def looks_like_missing_colon(message: str) -> bool:
    text = (message or "").lower()
    return "expected ':'" in text or "expected an indented block" in text or "invalid syntax" in text
