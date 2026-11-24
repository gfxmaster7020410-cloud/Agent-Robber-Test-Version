"""AST-range file edits recorded through FileMutationManager."""



from __future__ import annotations



import re

from pathlib import Path



from robber.core.ast_parser import ASTCodeParser

from robber.jail import JailError, resolve_inside, workspace_root

from robber.mutations import get_mutations

from robber.tools.workspace import pick_read_target



SYMBOL_RE = re.compile(

    r"\b(?:function|def|class|symbol)\s+([A-Za-z_][\w]*)",

    re.IGNORECASE,

)

BODY_RE = re.compile(

    r"(?:with|body|replacement)\s*:\s*(.+)$",

    re.IGNORECASE | re.DOTALL,

)





def replace_symbol(query: str, *, root: Path | None = None) -> str:

    base = (root or workspace_root()).resolve()

    relative = pick_read_target(query)

    if relative is None:

        raise JailError("No source file path found for AST edit")

    match = SYMBOL_RE.search(query or "")

    if match is None:

        raise JailError("Name a function/class to replace, e.g. 'replace function foo in bar.py'")

    name = match.group(1)

    body_match = BODY_RE.search(query or "")

    if body_match is None:

        raise JailError("Provide a replacement body after 'with:'")

    new_body = body_match.group(1).strip()

    if not new_body:

        raise JailError("replacement body is empty")



    target = resolve_inside(base, relative)

    if not target.is_file():

        raise JailError(f"not a file: {relative}")

    source = target.read_text(encoding="utf-8")

    lines = source.splitlines(keepends=True)

    symbols = ASTCodeParser().extract_symbols(str(target))

    hit = next((item for item in symbols if str(item.get("name")) == name), None)

    if hit is None:

        raise JailError(f"symbol '{name}' not found in {relative}")

    start = max(1, int(hit.get("start_line") or 1))

    end = max(start, int(hit.get("end_line") or start))

    prefix = "".join(lines[: start - 1])

    suffix = "".join(lines[end:])

    chunk = new_body if new_body.endswith("\n") else f"{new_body}\n"

    updated = prefix + chunk + suffix

    rel = str(target)

    try:

        rel = target.relative_to(base).as_posix()

    except ValueError:

        pass

    mutation = get_mutations().write_file(rel, updated, action="edit")

    return f"AST edit {mutation.path} {name} L{start}-L{end} {mutation.detail()}"

