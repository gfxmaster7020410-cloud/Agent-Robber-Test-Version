"""CPU / RSS profiling of sandboxed tests plus nested-loop bottleneck hints."""



from __future__ import annotations



import ast

import json

import time

from dataclasses import dataclass, field

from pathlib import Path

from typing import Any



from robber.config import COMBINED_RSS_LIMIT

from robber.jail import workspace_root

from robber.telemetry import rss_bytes





@dataclass

class Bottleneck:

    path: str

    line: int

    kind: str

    detail: str

    suggestion: str



    def public(self) -> dict[str, Any]:

        return {

            "path": self.path,

            "line": self.line,

            "kind": self.kind,

            "detail": self.detail,

            "suggestion": self.suggestion,

        }





@dataclass

class ProfileReport:

    ok: bool = True

    elapsed_ms: float = 0.0

    rss_bytes: int = 0

    rss_cap: int = COMBINED_RSS_LIMIT

    command: str = ""

    bottlenecks: list[Bottleneck] = field(default_factory=list)

    diffs: list[str] = field(default_factory=list)

    sandbox: dict[str, Any] = field(default_factory=dict)



    def public(self) -> dict[str, Any]:

        return {

            "ok": self.ok,

            "elapsed_ms": self.elapsed_ms,

            "rss_bytes": self.rss_bytes,

            "rss_cap": self.rss_cap,

            "command": self.command,

            "bottlenecks": [item.public() for item in self.bottlenecks],

            "diffs": list(self.diffs)[:4],

            "sandbox": dict(self.sandbox),

        }





class ExecutionProfiler:

    """Measure sandboxed test cost and flag O(N^2) nested scans."""



    def __init__(self, root: Path | str | None = None) -> None:

        self.root = Path(root).resolve() if root else workspace_root()



    def profile_suite(self, cmd: str = "python -m pytest -q", *, timeout_secs: int = 20) -> ProfileReport:

        started = time.perf_counter()

        before = int(rss_bytes())

        sandbox: dict[str, Any] = {}

        ok = True

        try:

            from robber.indexer_bridge import run_test_suite

            from robber.ssot_db import default_ssot_path



            sandbox = run_test_suite(

                self.root,

                cmd,

                working_dir=self.root,

                timeout_secs=timeout_secs,

                ssot_db=default_ssot_path(self.root),

            )

            ok = bool(sandbox.get("ok", True))

        except FileNotFoundError:

            sandbox = {"ok": True, "engine": "python-fallback"}

        except Exception as exc:

            sandbox = {"ok": False, "error": type(exc).__name__}

            ok = False

        elapsed = (time.perf_counter() - started) * 1000.0

        after = int(rss_bytes())

        report = ProfileReport(

            ok=ok and after < COMBINED_RSS_LIMIT,

            elapsed_ms=round(float(sandbox.get("elapsed_ms") or elapsed), 2),

            rss_bytes=max(after, int(sandbox.get("rss_bytes") or 0), before),

            command=cmd,

            sandbox={k: sandbox.get(k) for k in ("ok", "elapsed_ms", "timed_out", "memory_exceeded", "exit_code") if k in sandbox},

        )

        report.bottlenecks = self.scan_tree()

        report.diffs = [item.suggestion for item in report.bottlenecks[:4]]

        return report



    def record_suite(self, sandbox: dict[str, Any], files: list[str] | None = None) -> ProfileReport:

        report = ProfileReport(

            ok=bool(sandbox.get("ok", True)),

            elapsed_ms=float(sandbox.get("elapsed_ms") or 0.0),

            rss_bytes=int(rss_bytes()),

            command=str(sandbox.get("command") or sandbox.get("cmd") or ""),

            sandbox={

                "ok": sandbox.get("ok"),

                "elapsed_ms": sandbox.get("elapsed_ms"),

                "memory_exceeded": sandbox.get("memory_exceeded"),

                "timed_out": sandbox.get("timed_out"),

            },

        )

        report.bottlenecks = self.scan_tree(files)

        report.diffs = [self.propose_diff(item) for item in report.bottlenecks[:3] if self.propose_diff(item)]

        return report



    def scan_tree(self, files: list[str] | None = None) -> list[Bottleneck]:

        out: list[Bottleneck] = []

        paths: list[Path] = []

        if files:

            for rel in files[:12]:

                path = self.root / rel

                if path.is_file() and path.suffix == ".py":

                    paths.append(path)

        else:

            paths = [path for path in self.root.glob("*.py") if path.is_file()][:12]

        for path in paths:

            try:

                text = path.read_text(encoding="utf-8")

            except OSError:

                continue

            rel = path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else path.name

            out.extend(scan_nested_loops(text, rel))

        return out



    def propose_diff(self, item: Bottleneck) -> str:

        return item.suggestion





def scan_nested_loops(source: str, path: str = "") -> list[Bottleneck]:

    try:

        tree = ast.parse(source or "")

    except SyntaxError:

        return []

    out: list[Bottleneck] = []

    for node in ast.walk(tree):

        if not isinstance(node, (ast.For, ast.While, ast.AsyncFor)):

            continue

        for child in ast.walk(node):

            if child is node:

                continue

            if isinstance(child, (ast.For, ast.AsyncFor)) and _is_nested(node, child):

                line = int(getattr(child, "lineno", 1) or 1)

                out.append(

                    Bottleneck(

                        path=path,

                        line=line,

                        kind="nested_loop",

                        detail="O(N^2) nested iteration",

                        suggestion=(

                            f"# {path}:{line} replace inner scan with a set/dict index\n"

                            "# before = O(N^2) nested for-loops over large collections\n"

                            "index = {item: True for item in inner}\n"

                            "matched = [item for item in outer if item in index]\n"

                        ),

                    )

                )

                break

    return out[:8]





def _is_nested(parent: ast.AST, child: ast.AST) -> bool:

    for node in ast.iter_child_nodes(parent):

        if node is child:

            return True

        if _contains(node, child):

            return True

    return False





def _contains(tree: ast.AST, target: ast.AST) -> bool:

    for node in ast.walk(tree):

        if node is target:

            return True

    return False





def dump_report(report: ProfileReport) -> str:

    return json.dumps(report.public(), ensure_ascii=True)[:4000]

