"""Render agent markdown/diff streams to ANSI without narrow wrap limits."""

from __future__ import annotations

import os
import re
from io import StringIO
from typing import Iterator

from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.theme import Theme

FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$")
DIFF_META_RE = re.compile(r"^(diff |index |--- |\+\+\+ |@@ )")

ACT_SILENT_DIRECTIVE = (
    "DO NOT print internal workflow execution options or numbered steps. "
    "Execute directly and stream the markdown/diff result."
)

LEXERS = {
    "py": "python",
    "python": "python",
    "ts": "typescript",
    "tsx": "typescript",
    "js": "javascript",
    "jsx": "jsx",
    "json": "json",
    "css": "css",
    "html": "html",
    "md": "markdown",
    "diff": "diff",
    "bash": "bash",
    "sh": "bash",
    "shell": "bash",
    "toml": "toml",
    "yaml": "yaml",
    "yml": "yaml",
    "rs": "rust",
    "go": "go",
}

_THEME = Theme(
    {
        "markdown.h1": "bold yellow",
        "markdown.h2": "bold yellow",
        "markdown.h3": "bold yellow",
        "markdown.h4": "bold cyan",
        "markdown.h5": "bold cyan",
        "markdown.h6": "bold cyan",
        "markdown.paragraph": "#e8e8ed",
        "markdown.item": "#e8e8ed",
        "markdown.code": "cyan",
        "markdown.block_quote": "italic #8b8b9a",
    }
)


def terminal_columns() -> int:
    for getter in (
        lambda: os.get_terminal_size(0).columns,
        lambda: os.get_terminal_size(1).columns,
        lambda: int(os.environ.get("COLUMNS") or "0"),
    ):
        try:
            columns = int(getter())
        except (OSError, ValueError, TypeError):
            continue
        if columns >= 40:
            return columns
    return 120


def wrap_width(columns: int | None = None) -> int:
    pane = terminal_columns() if columns is None else int(columns)
    return max(60, pane - 4)


SYNTAX_THEME = "monokai"


def lexer_name(language: str | None) -> str:
    key = (language or "").strip().lower()
    if not key or key in {"text", "txt", "plain"}:
        return "text"
    return LEXERS.get(key, key)


def looks_like_markdown(text: str) -> bool:
    raw = text or ""
    if "```" in raw:
        return True
    return bool(re.search(r"^#{1,6}\s|^\s*[-*+]\s|\| .+\ |^diff |^@@ ", raw, re.MULTILINE))


def iter_markdown_blocks(text: str) -> Iterator[tuple[str, str, str | None, str | None]]:
    """Yield (kind, payload, language, path) where kind is 'prose' or 'code'."""
    from robber.diff_card import parse_fence_header

    raw = text or ""
    cursor = 0
    for match in FENCE_RE.finditer(raw):
        if match.start() > cursor:
            prose = raw[cursor : match.start()].strip("\n")
            if prose:
                yield "prose", prose, None, None
        language, path = parse_fence_header(match.group(1) or "")
        yield "code", match.group(2).rstrip("\n"), language or "text", path
        cursor = match.end()
    rest = raw[cursor:].strip("\n")
    if rest:
        yield "prose", rest, None, None


def highlight_lines(text: str) -> str:
    """Cheap per-line colorizer for streaming chunks (no wrapping)."""
    out: list[str] = []
    for line in (text or "").splitlines(keepends=True):
        core, nl = (line[:-1], "\n") if line.endswith("\n") else (line, "")
        if DIFF_META_RE.match(core):
            out.append(f"\033[2;36m{core}\033[0m{nl}")
        elif core.startswith("+"):
            out.append(f"\033[32m{core}\033[0m{nl}")
        elif core.startswith("-"):
            out.append(f"\033[31m{core}\033[0m{nl}")
        else:
            header = HEADER_RE.match(core)
            if header:
                style = "1;36" if len(header.group(1)) >= 4 else "1;33"
                out.append(f"\033[{style}m{core}\033[0m{nl}")
            else:
                out.append(line)
    return "".join(out)


def _console(width: int, *, soft_wrap: bool) -> tuple[Console, StringIO]:
    buf = StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=max(40, width),
        highlight=False,
        soft_wrap=soft_wrap,
        theme=_THEME,
        legacy_windows=False,
    )
    return console, buf


def render_syntax_ansi(code: str, language: str | None = None, *, width: int | None = None) -> str:
    pane = wrap_width(width)
    console, buf = _console(pane, soft_wrap=True)
    try:
        renderable = Syntax(
            code or "",
            lexer_name(language),
            theme=SYNTAX_THEME,
            word_wrap=True,
            line_numbers=True,
            padding=0,
            background_color="default",
        )
    except Exception:
        renderable = Syntax(
            code or "",
            "text",
            theme=SYNTAX_THEME,
            word_wrap=True,
            line_numbers=True,
            padding=0,
            background_color="default",
        )
    console.print(renderable)
    return buf.getvalue()


def render_prose_ansi(text: str, *, width: int | None = None) -> str:
    pane = wrap_width(width)
    console, buf = _console(pane, soft_wrap=True)
    console.print(Markdown(text or "", hyperlinks=False, code_theme=SYNTAX_THEME))
    return buf.getvalue()


def render_markdown_ansi(text: str, *, width: int | None = None) -> str:
    """Render markdown to ANSI using the full terminal pane width."""
    if not (text or "").strip():
        return ""
    pane = wrap_width(width)
    if len(text) > 120_000:
        return highlight_lines(text)
    chunks: list[str] = []
    for kind, payload, language, _path in iter_markdown_blocks(text):
        if kind == "code":
            chunks.append(render_syntax_ansi(payload, language, width=pane).rstrip("\n"))
        else:
            chunks.append(render_prose_ansi(payload, width=pane).rstrip("\n"))
    return "\n".join(part for part in chunks if part) + ("\n" if text.endswith("\n") else "")


def colorize_for_emit(text: str, *, append: bool = False, width: int | None = None) -> str:
    """Preserve source text; add ANSI so diffs and headers survive strip/parse."""
    del append, width
    return highlight_lines(text or "")
