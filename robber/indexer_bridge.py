"""Load the Rust indexer cdylib when it has been built.

The C ABI lives in indexer/src/ffi.rs and indexer/src/py_bindings.rs.
`cargo test` / `cargo build -p robber-indexer` produces the DLL.
"""

from __future__ import annotations

import json
import os
from ctypes import CDLL, c_char_p, c_int32, c_uint32, create_string_buffer
from pathlib import Path
from typing import Any

_DLL: CDLL | None = None

_CDYLIB_NAMES = ("robber_indexer.dll", "librobber_indexer.so", "librobber_indexer.dylib")
_TARGET_TRIPLES = (
    "",
    "x86_64-pc-windows-gnu",
    "x86_64-pc-windows-msvc",
    "x86_64-unknown-linux-gnu",
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
)


def install_root() -> Path:
    return Path(__file__).resolve().parents[1]


def cdylib_candidates(root: Path | None = None) -> list[Path]:
    """Ordered search for the indexer cdylib (profile dir first, then deps/)."""
    install = Path(root).resolve() if root is not None else install_root()
    names = _CDYLIB_NAMES
    out: list[Path] = []
    seen: set[Path] = set()
    extra = (os.environ.get("CARGO_TARGET_DIR") or "").strip()
    roots = [install / "indexer" / "target"]
    if extra:
        roots.append(Path(extra))
    for base in roots:
        for triple in _TARGET_TRIPLES:
            stem = base / triple if triple else base
            for profile in ("release", "debug"):
                folder = stem / profile
                for name in names:
                    for path in (folder / name, folder / "deps" / name, stem / name):
                        resolved = path if path.is_absolute() else path.resolve()
                        if resolved in seen:
                            continue
                        seen.add(resolved)
                        out.append(resolved)
    return out


def _candidates() -> list[Path]:
    return cdylib_candidates()


def _bind_dll(dll: CDLL) -> CDLL:
    dll.robber_index_workspace.restype = c_int32
    dll.robber_mmap_search.restype = c_int32
    dll.robber_last_error.restype = c_char_p
    if hasattr(dll, "robber_run_tool_sandboxed"):
        dll.robber_run_tool_sandboxed.restype = c_int32
        dll.robber_run_tool_sandboxed.argtypes = [
            c_char_p,
            c_char_p,
            c_char_p,
            c_char_p,
            c_uint32,
            c_char_p,
            c_char_p,
            c_uint32,
        ]
    if hasattr(dll, "robber_apply_diff_atomic"):
        dll.robber_apply_diff_atomic.restype = c_int32
        dll.robber_apply_diff_atomic.argtypes = [
            c_char_p,
            c_char_p,
            c_char_p,
            c_char_p,
            c_uint32,
        ]
    if hasattr(dll, "robber_quick_search"):
        dll.robber_quick_search.restype = c_int32
        dll.robber_quick_search.argtypes = [c_char_p, c_char_p, c_uint32, c_char_p, c_uint32]
    if hasattr(dll, "robber_get_symbol_context"):
        dll.robber_get_symbol_context.restype = c_int32
        dll.robber_get_symbol_context.argtypes = [
            c_char_p,
            c_char_p,
            c_uint32,
            c_char_p,
            c_uint32,
        ]
    if hasattr(dll, "robber_run_test_suite"):
        dll.robber_run_test_suite.restype = c_int32
        dll.robber_run_test_suite.argtypes = [
            c_char_p,
            c_char_p,
            c_char_p,
            c_uint32,
            c_char_p,
            c_char_p,
            c_uint32,
        ]
    if hasattr(dll, "robber_security_audit"):
        dll.robber_security_audit.restype = c_int32
        dll.robber_security_audit.argtypes = [
            c_char_p,
            c_char_p,
            c_char_p,
            c_uint32,
        ]
    if hasattr(dll, "robber_ast_merge"):
        dll.robber_ast_merge.restype = c_int32
        dll.robber_ast_merge.argtypes = [
            c_char_p,
            c_char_p,
            c_char_p,
            c_char_p,
            c_uint32,
        ]
    if hasattr(dll, "robber_commit_message"):
        dll.robber_commit_message.restype = c_int32
        dll.robber_commit_message.argtypes = [
            c_char_p,
            c_char_p,
            c_char_p,
            c_uint32,
        ]
    if hasattr(dll, "robber_feature_branch_name"):
        dll.robber_feature_branch_name.restype = c_int32
        dll.robber_feature_branch_name.argtypes = [
            c_char_p,
            c_char_p,
            c_char_p,
            c_uint32,
        ]
    if hasattr(dll, "robber_conventional_commit"):
        dll.robber_conventional_commit.restype = c_int32
        dll.robber_conventional_commit.argtypes = [
            c_char_p,
            c_char_p,
            c_char_p,
            c_char_p,
            c_char_p,
            c_uint32,
        ]
    if hasattr(dll, "robber_search_symbols"):
        dll.robber_search_symbols.restype = c_int32
        dll.robber_search_symbols.argtypes = [
            c_char_p,
            c_char_p,
            c_char_p,
            c_char_p,
            c_uint32,
        ]
    if hasattr(dll, "robber_find_references"):
        dll.robber_find_references.restype = c_int32
        dll.robber_find_references.argtypes = [
            c_char_p,
            c_char_p,
            c_char_p,
            c_uint32,
        ]
    if hasattr(dll, "robber_replace_ast_node"):
        dll.robber_replace_ast_node.restype = c_int32
        dll.robber_replace_ast_node.argtypes = [
            c_char_p,
            c_char_p,
            c_char_p,
            c_char_p,
            c_char_p,
            c_uint32,
        ]
    if hasattr(dll, "robber_release_caches"):
        dll.robber_release_caches.restype = c_int32
    return dll


def load_indexer() -> CDLL | None:
    global _DLL
    if _DLL is not None:
        return _DLL
    best: CDLL | None = None
    best_score = -1
    for path in _candidates():
        if not path.is_file():
            continue
        try:
            dll = _bind_dll(CDLL(str(path)))
        except OSError:
            continue
        if not hasattr(dll, "robber_index_workspace"):
            continue
        score = 1
        if hasattr(dll, "robber_get_symbol_context"):
            score += 2
        if hasattr(dll, "robber_run_test_suite"):
            score += 4
        if score > best_score:
            best = dll
            best_score = score
    _DLL = best
    return best


def index_workspace(root: str | Path, ssot_db: str | Path) -> dict[str, Any]:
    dll = load_indexer()
    if dll is None:
        raise FileNotFoundError("robber indexer dll is not built")
    buf = create_string_buffer(4096)
    rc = dll.robber_index_workspace(
        str(root).encode("utf-8"),
        str(ssot_db).encode("utf-8"),
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        err = dll.robber_last_error()
        raise RuntimeError(err.decode("utf-8", "replace") if err else "indexer failed")
    return json.loads(buf.value.decode("utf-8"))


def mmap_search(root: str | Path, pattern: str, max_hits: int = 64) -> list[dict[str, Any]]:
    dll = load_indexer()
    if dll is None:
        raise FileNotFoundError("robber indexer dll is not built")
    buf = create_string_buffer(1 << 16)
    rc = dll.robber_mmap_search(
        str(root).encode("utf-8"),
        pattern.encode("utf-8"),
        c_uint32(max_hits),
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        err = dll.robber_last_error()
        raise RuntimeError(err.decode("utf-8", "replace") if err else "search failed")
    return json.loads(buf.value.decode("utf-8") or "[]")


def _err(dll: CDLL, fallback: str) -> RuntimeError:
    err = dll.robber_last_error()
    return RuntimeError(err.decode("utf-8", "replace") if err else fallback)


def run_tool_sandboxed(
    root: str | Path,
    program: str,
    args: list[str] | None = None,
    cwd: str | Path | None = None,
    timeout_ms: int = 8000,
    ssot_db: str | Path | None = None,
) -> dict[str, Any]:
    """Invoke the Rust sandbox in-process (ctypes, no extra IPC)."""
    dll = load_indexer()
    if dll is None or not hasattr(dll, "robber_run_tool_sandboxed"):
        raise FileNotFoundError("robber indexer dll is not built")
    buf = create_string_buffer(256 * 1024)
    rc = dll.robber_run_tool_sandboxed(
        str(root).encode("utf-8"),
        program.encode("utf-8"),
        json.dumps(list(args or [])).encode("utf-8"),
        str(cwd).encode("utf-8") if cwd is not None else None,
        c_uint32(timeout_ms),
        str(ssot_db).encode("utf-8") if ssot_db is not None else None,
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        raise _err(dll, "sandboxed tool failed")
    return json.loads(buf.value.decode("utf-8"))


def apply_diff_atomic(
    root: str | Path,
    diff: str,
    ssot_db: str | Path | None = None,
) -> dict[str, Any]:
    dll = load_indexer()
    if dll is None or not hasattr(dll, "robber_apply_diff_atomic"):
        raise FileNotFoundError("robber indexer dll is not built")
    buf = create_string_buffer(64 * 1024)
    rc = dll.robber_apply_diff_atomic(
        str(root).encode("utf-8"),
        diff.encode("utf-8"),
        str(ssot_db).encode("utf-8") if ssot_db is not None else None,
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        raise _err(dll, "atomic diff failed")
    return json.loads(buf.value.decode("utf-8"))


def quick_search(root: str | Path, pattern: str, max_hits: int = 64) -> list[dict[str, Any]]:
    dll = load_indexer()
    if dll is None or not hasattr(dll, "robber_quick_search"):
        return mmap_search(root, pattern, max_hits)
    buf = create_string_buffer(1 << 16)
    rc = dll.robber_quick_search(
        str(root).encode("utf-8"),
        pattern.encode("utf-8"),
        c_uint32(max_hits),
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        raise _err(dll, "search failed")
    return json.loads(buf.value.decode("utf-8") or "[]")


def run_test_suite(
    root: str | Path,
    cmd: str,
    working_dir: str | Path | None = None,
    timeout_secs: int = 30,
    ssot_db: str | Path | None = None,
) -> dict[str, Any]:
    """ctypes stand-in for PyO3 `run_test_suite(cmd, working_dir, timeout_secs)`."""
    dll = load_indexer()
    if dll is None or not hasattr(dll, "robber_run_test_suite"):
        raise FileNotFoundError("robber indexer dll is not built")
    buf = create_string_buffer(512 * 1024)
    cwd = str(working_dir) if working_dir is not None else str(root)
    rc = dll.robber_run_test_suite(
        str(root).encode("utf-8"),
        (cmd or "").encode("utf-8"),
        cwd.encode("utf-8"),
        c_uint32(max(0, int(timeout_secs))),
        str(ssot_db).encode("utf-8") if ssot_db is not None else None,
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        raise _err(dll, "test suite failed")
    return json.loads(buf.value.decode("utf-8") or "{}")


def get_symbol_context(root: str | Path, query_symbol: str, depth: int = 2) -> str:
    """ctypes stand-in for PyO3 `get_symbol_context(query_symbol, depth) -> str`."""
    dll = load_indexer()
    if dll is None or not hasattr(dll, "robber_get_symbol_context"):
        raise FileNotFoundError("robber indexer dll is not built")
    buf = create_string_buffer(256 * 1024)
    rc = dll.robber_get_symbol_context(
        str(root).encode("utf-8"),
        (query_symbol or "").encode("utf-8"),
        c_uint32(max(0, int(depth))),
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        raise _err(dll, "symbol context failed")
    return buf.value.decode("utf-8") or "{}"


def ast_merge(base: str, ours: str, theirs: str) -> dict[str, Any]:
    dll = load_indexer()
    if dll is None or not hasattr(dll, "robber_ast_merge"):
        raise FileNotFoundError("robber indexer dll is not built")
    buf = create_string_buffer(256 * 1024)
    rc = dll.robber_ast_merge(
        (base or "").encode("utf-8"),
        (ours or "").encode("utf-8"),
        (theirs or "").encode("utf-8"),
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        raise _err(dll, "ast merge failed")
    return json.loads(buf.value.decode("utf-8") or "{}")


def commit_message(base: str, ours: str) -> str:
    dll = load_indexer()
    if dll is None or not hasattr(dll, "robber_commit_message"):
        raise FileNotFoundError("robber indexer dll is not built")
    buf = create_string_buffer(4096)
    rc = dll.robber_commit_message(
        (base or "").encode("utf-8"),
        (ours or "").encode("utf-8"),
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        raise _err(dll, "commit message failed")
    return buf.value.decode("utf-8") or ""


def feature_branch_name(task_id: str, slug: str) -> str:
    dll = load_indexer()
    if dll is None or not hasattr(dll, "robber_feature_branch_name"):
        raise FileNotFoundError("robber indexer dll is not built")
    buf = create_string_buffer(256)
    rc = dll.robber_feature_branch_name(
        (task_id or "").encode("utf-8"),
        (slug or "").encode("utf-8"),
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        raise _err(dll, "feature branch name failed")
    return buf.value.decode("utf-8") or ""


def conventional_commit(base: str, ours: str, paths: str = "", scope: str = "") -> str:
    dll = load_indexer()
    if dll is None or not hasattr(dll, "robber_conventional_commit"):
        raise FileNotFoundError("robber indexer dll is not built")
    buf = create_string_buffer(4096)
    rc = dll.robber_conventional_commit(
        (base or "").encode("utf-8"),
        (ours or "").encode("utf-8"),
        (paths or "").encode("utf-8"),
        (scope or "").encode("utf-8"),
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        raise _err(dll, "conventional commit failed")
    return buf.value.decode("utf-8") or ""


def security_audit(source: str, path: str = "") -> list[dict[str, Any]]:
    dll = load_indexer()
    if dll is None or not hasattr(dll, "robber_security_audit"):
        raise FileNotFoundError("robber indexer dll is not built")
    buf = create_string_buffer(64 * 1024)
    rc = dll.robber_security_audit(
        (source or "").encode("utf-8"),
        (path or "").encode("utf-8"),
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        raise _err(dll, "security audit failed")
    data = json.loads(buf.value.decode("utf-8") or "[]")
    return data if isinstance(data, list) else list(data.get("findings") or [])


def search_workspace_symbols(
    root: str | Path,
    query: str,
    lang_filter: str | None = None,
) -> list[dict[str, Any]]:
    dll = load_indexer()
    if dll is None or not hasattr(dll, "robber_search_symbols"):
        raise FileNotFoundError("robber indexer dll is not built")
    buf = create_string_buffer(256 * 1024)
    lang_b = lang_filter.encode("utf-8") if lang_filter else None
    rc = dll.robber_search_symbols(
        str(root).encode("utf-8"),
        (query or "").encode("utf-8"),
        lang_b,
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        raise _err(dll, "search workspace symbols failed")
    return json.loads(buf.value.decode("utf-8") or "[]")


def find_symbol_references(
    root: str | Path,
    symbol_name: str,
) -> dict[str, Any]:
    dll = load_indexer()
    if dll is None or not hasattr(dll, "robber_find_references"):
        raise FileNotFoundError("robber indexer dll is not built")
    buf = create_string_buffer(256 * 1024)
    rc = dll.robber_find_references(
        str(root).encode("utf-8"),
        (symbol_name or "").encode("utf-8"),
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        raise _err(dll, "find symbol references failed")
    return json.loads(buf.value.decode("utf-8") or "{}")


def replace_ast_node(
    root: str | Path,
    file_path: str,
    target_symbol: str,
    new_code: str,
) -> str:
    dll = load_indexer()
    if dll is None or not hasattr(dll, "robber_replace_ast_node"):
        raise FileNotFoundError("robber indexer dll is not built")
    buf = create_string_buffer(512 * 1024)
    rc = dll.robber_replace_ast_node(
        str(root).encode("utf-8"),
        str(file_path).encode("utf-8"),
        str(target_symbol).encode("utf-8"),
        str(new_code).encode("utf-8"),
        buf,
        c_uint32(len(buf)),
    )
    if rc < 0:
        raise _err(dll, "replace ast node failed")
    data = json.loads(buf.value.decode("utf-8") or "{}")
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "replace ast node error")
    return str(data.get("updated") or "")


def release_caches() -> None:
    """Drop the loaded cdylib so AST/mmap working sets can be reclaimed."""
    global _DLL
    dll = _DLL
    if dll is not None and hasattr(dll, "robber_release_caches"):
        try:
            dll.robber_release_caches()
        except Exception:
            pass
    try:
        from robber.memory.context_graph import clear_graph_cache

        clear_graph_cache()
    except Exception:
        pass
    _DLL = None
