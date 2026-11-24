"""Real-time DOM, console and network failure inspector for browser sessions."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from robber.jail import is_skipped_dir, workspace_root
from robber.tools.browser_engine import BrowserSnapshot

logger = logging.getLogger(__name__)

STACK_LINE_RE = re.compile(
    r"""at\s+(?:(?P<fn>[\w$.<>]+)\s+\()?(?:(?P<path>[A-Za-z]:[\\/][^:]+|/[^:]+|\./[^:]+|[\w./\\-]+(?:\.(?:tsx|jsx|js|ts|vue|html)))):(?P<line>\d+)(?::(?P<col>\d+))?\)?|"""
    r"""(?P<direct_path>[A-Za-z]:[\\/][^:]+|/[\w./\\-]+|[\w./\\-]+(?:\.(?:tsx|jsx|js|ts|vue|html))):(?P<direct_line>\d+)(?::(?P<direct_col>\d+))?""",
    re.IGNORECASE,
)

COMPONENT_RE = re.compile(
    r"""<(?:[A-Z][\w$]+|button|input|form|a|div|span|select|textarea)\b[^>]*>""",
    re.IGNORECASE,
)

ERROR_PATTERNS = [
    re.compile(r"Uncaught\s+(?:TypeError|ReferenceError|SyntaxError|RangeError):\s*(?P<msg>.+)", re.I),
    re.compile(r"TypeError:\s*(?P<msg>.+)", re.I),
    re.compile(r"ReferenceError:\s*(?P<msg>.+)", re.I),
    re.compile(r"Cannot\s+read\s+propert(?:y|ies)\s+of\s+(?:undefined|null)\s+\(reading\s+['\"](?P<prop>\w+)['\"]\)", re.I),
    re.compile(r"(?P<fn>\w+)\s+is\s+not\s+a\s+function", re.I),
    re.compile(r"(?P<var>\w+)\s+is\s+not\s+defined", re.I),
    re.compile(r"Failed\s+to\s+load\s+resource:\s+the\s+server\s+responded\s+with\s+a\s+status\s+of\s+(?P<code>\d+)", re.I),
]


@dataclass
class DOMTargetMatch:
    component: str = ""
    file_path: str = ""
    line: int = 0
    column: int = 0
    selector: str = ""
    snippet: str = ""
    source_context: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BrowserInspectionReport:
    url: str
    has_errors: bool
    summary: str
    console_errors: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[dict[str, Any]] = field(default_factory=list)
    failed_requests: list[dict[str, Any]] = field(default_factory=list)
    dom_target: DOMTargetMatch = field(default_factory=DOMTargetMatch)
    suggested_fix: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "has_errors": self.has_errors,
            "summary": self.summary,
            "console_errors": self.console_errors,
            "page_errors": self.page_errors,
            "failed_requests": self.failed_requests,
            "dom_target": self.dom_target.as_dict(),
            "suggested_fix": self.suggested_fix,
        }


class BrowserInspector:
    """Telemetry analyzer mapping JS crashes, network drops, and DOM components to source code."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or workspace_root()).resolve()

    def inspect_snapshot(self, snapshot: BrowserSnapshot) -> BrowserInspectionReport:
        console_errs = [e for e in snapshot.console_errors if str(e.get("type", "")).lower() in ("error", "pageerror")]
        page_errs = list(snapshot.page_errors)
        failed_reqs = list(snapshot.failed_requests)

        has_errors = bool(console_errs or page_errs or failed_reqs or not snapshot.ok)

        # Collect raw error text to locate DOM / source targets
        err_texts: list[str] = []
        for e in console_errs:
            err_texts.append(str(e.get("text", "")))
        for p in page_errs:
            err_texts.append(str(p.get("message", "")))
        for f in failed_reqs:
            err_texts.append(f"HTTP Failed: {f.get('method', 'GET')} {f.get('url', '')} - {f.get('failure', '')}")

        joined_errors = "\n".join(err_texts)
        dom_target = self.locate_error_in_dom(joined_errors, snapshot.dom_html)
        summary = self._build_summary(console_errs, page_errs, failed_reqs, snapshot.status_code)
        suggested_fix = self._suggest_fix(joined_errors, dom_target)

        return BrowserInspectionReport(
            url=snapshot.url,
            has_errors=has_errors,
            summary=summary,
            console_errors=console_errs,
            page_errors=page_errs,
            failed_requests=failed_reqs,
            dom_target=dom_target,
            suggested_fix=suggested_fix,
        )

    def locate_error_in_dom(self, error_text: str, dom_html: str) -> DOMTargetMatch:
        """Parse stack trace or error message to identify source file and DOM element."""
        if not error_text.strip():
            return DOMTargetMatch()

        # 1. Look for stack trace lines pointing to source files
        for match in STACK_LINE_RE.finditer(error_text):
            p = match.group("path") or match.group("direct_path") or ""
            line_str = match.group("line") or match.group("direct_line") or "1"
            fn = match.group("fn") or ""
            if p:
                file_cand = self._resolve_source_file(p)
                if file_cand:
                    line_no = int(line_str) if line_str.isdigit() else 1
                    snippet = self._read_source_snippet(file_cand, line_no)
                    return DOMTargetMatch(
                        component=fn or file_cand.stem,
                        file_path=str(file_cand.relative_to(self.root)).replace("\\", "/"),
                        line=line_no,
                        selector=f"#{fn}" if fn else f".{file_cand.stem.lower()}",
                        snippet=snippet,
                        source_context=snippet,
                    )

        # 2. Look for identifier names in error text (e.g. handleClick, onClick, renderHeader)
        for pattern in ERROR_PATTERNS:
            m = pattern.search(error_text)
            if m:
                d = m.groupdict()
                name = d.get("prop") or d.get("fn") or d.get("var") or ""
                if name:
                    # Search workspace for this identifier
                    found = self._find_symbol_in_workspace(name)
                    if found:
                        file_path, line_no, snippet = found
                        return DOMTargetMatch(
                            component=name,
                            file_path=str(file_path.relative_to(self.root)).replace("\\", "/"),
                            line=line_no,
                            selector=f"[data-component='{name}']",
                            snippet=snippet,
                            source_context=snippet,
                        )

        # 3. Look in DOM html for matching elements
        if dom_html:
            elements = COMPONENT_RE.findall(dom_html)
            if elements:
                return DOMTargetMatch(
                    component="DOMRoot",
                    selector="body",
                    snippet=elements[0][:120],
                )

        return DOMTargetMatch()

    def _resolve_source_file(self, raw_path: str) -> Path | None:
        p = Path(raw_path.strip().strip("'\""))
        if p.is_file():
            return p
        candidate = self.root / p
        if candidate.is_file():
            return candidate
        # Search by file name in workspace
        name = p.name
        if name:
            for cand in self.root.rglob(name):
                if cand.is_file() and not any(is_skipped_dir(part) for part in cand.parts):
                    return cand
        return None

    def _read_source_snippet(self, path: Path, line: int) -> str:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(0, line - 2)
            end = min(len(lines), line + 3)
            return "\n".join(lines[start:end])
        except Exception:
            return ""

    def _find_symbol_in_workspace(self, symbol_name: str) -> tuple[Path, int, str] | None:
        for ext in ("*.tsx", "*.jsx", "*.js", "*.ts", "*.vue", "*.html", "*.py"):
            for cand in self.root.rglob(ext):
                if any(is_skipped_dir(part) for part in cand.parts) or not cand.is_file():
                    continue
                try:
                    text = cand.read_text(encoding="utf-8", errors="replace")
                    if symbol_name in text:
                        for idx, line in enumerate(text.splitlines(), start=1):
                            if symbol_name in line:
                                snippet = self._read_source_snippet(cand, idx)
                                return cand, idx, snippet
                except Exception:
                    continue
        return None

    def _build_summary(
        self,
        console_errors: list[dict[str, Any]],
        page_errors: list[dict[str, Any]],
        failed_requests: list[dict[str, Any]],
        status_code: int,
    ) -> str:
        parts: list[str] = []
        if status_code >= 400:
            parts.append(f"HTTP Status {status_code}")
        if page_errors:
            parts.append(f"{len(page_errors)} uncaught page error(s)")
        if console_errors:
            parts.append(f"{len(console_errors)} console error(s)")
        if failed_requests:
            parts.append(f"{len(failed_requests)} failed network request(s)")
        if not parts:
            return "Web UI rendering clean with 0 console or network errors."
        return "Browser inspection detected: " + ", ".join(parts)

    def _suggest_fix(self, error_text: str, target: DOMTargetMatch) -> str:
        if not error_text.strip():
            return ""
        if "Cannot read property" in error_text or "Cannot read properties" in error_text:
            return f"Add optional chaining (?.) or null-safety check in {target.file_path or 'target component'}."
        if "is not a function" in error_text:
            return f"Verify handler signature or import binding in {target.file_path or 'target component'}."
        if "is not defined" in error_text:
            return f"Import or declare the missing identifier in {target.file_path or 'target component'}."
        if "HTTP Failed" in error_text:
            return "Verify API endpoint URL and ensure backend CORS / mock routes are active."
        return "Review DOM component source and error traceback to apply structural AST fix."
