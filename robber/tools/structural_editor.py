"""AST node-level structural code editing engine with perimeter security."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from robber.jail import JailError, display_path, resolve_inside, workspace_root
from robber.mutations import get_mutations
from robber.security.perimeter import SelfModificationProtectedError, validate_file_mutation


class EditError(ValueError):
    """Raised when AST structural editing or syntax validation fails."""


def replace_ast_node(
    file_path: str,
    target_symbol: str,
    new_code_block: str,
    *,
    root: Path | None = None,
    is_dev_mode: bool = False,
) -> str:
    """Locate target AST symbol in file, replace node, verify syntax, and apply mutation."""
    base = (root or workspace_root()).resolve()
    target = resolve_inside(base, file_path)

    # 1. Enforce Self-Codebase Protection Boundary
    validate_file_mutation(str(target), is_dev_mode=is_dev_mode)

    if not target.is_file():
        raise EditError(f"Target file does not exist: {file_path}")

    source = target.read_text(encoding="utf-8")
    rel = display_path(target, base)

    # 2. Try native Tree-Sitter replacement first
    try:
        from robber.indexer_bridge import replace_ast_node as native_replace

        updated = native_replace(base, rel, target_symbol, new_code_block)
        if updated:
            _verify_python_syntax_if_py(target, updated)
            target.write_text(updated, encoding="utf-8")
            get_mutations().write_file(rel, updated, action="edit")
            return updated
    except SelfModificationProtectedError:
        raise
    except EditError:
        raise
    except Exception as exc:
        err_msg = str(exc)
        if "AstValidationError" in err_msg or "syntax" in err_msg.lower():
            raise EditError(f"AST validation error: {err_msg}") from exc
        # Fall through to Python structural editor

    # 3. Python AST-based structural replacement fallback
    updated = _python_replace_node(target, source, target_symbol, new_code_block)
    _verify_python_syntax_if_py(target, updated)
    target.write_text(updated, encoding="utf-8")
    get_mutations().write_file(rel, updated, action="edit")
    return updated


def _verify_python_syntax_if_py(path: Path, code: str) -> None:
    if path.suffix.lower() == ".py":
        try:
            ast.parse(code, filename=str(path))
            compile(code, str(path), "exec")
        except SyntaxError as exc:
            raise EditError(f"Syntax validation failed: {exc}") from exc


def _python_replace_node(path: Path, source: str, target_symbol: str, new_code: str) -> str:
    clean_sym = target_symbol.strip()
    clean_sym = re.sub(r"^(?:def|class|async\s+def)\s+", "", clean_sym).strip()
    parts = clean_sym.split(".")
    target_parent = parts[0] if len(parts) > 1 else ""
    target_member = parts[-1]

    lines = source.splitlines(keepends=True)

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise EditError(f"Cannot parse original file: {exc}") from exc

    found_node: ast.AST | None = None

    if target_parent:
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == target_parent:
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == target_member:
                        found_node = item
                        break
                if found_node is not None:
                    break
    else:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if getattr(node, "name", "") == target_member:
                    found_node = node
                    break

    if found_node is None:
        raise EditError(f"Symbol `{target_symbol}` not found in AST of {path.name}")

    start_line = getattr(found_node, "lineno", 1) - 1
    end_line = getattr(found_node, "end_lineno", start_line + 1)

    prefix_lines = lines[:start_line]
    suffix_lines = lines[end_line:]

    # Prepare formatted replacement
    formatted_block = new_code if new_code.endswith("\n") else f"{new_code}\n"
    updated = "".join(prefix_lines) + formatted_block + "".join(suffix_lines)

    return updated
