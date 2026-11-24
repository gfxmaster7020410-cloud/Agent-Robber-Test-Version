from __future__ import annotations

import asyncio
from pathlib import Path

from robber.mutations import FileMutationManager, bind_mutations
from robber.tools.analyze import analyze_file
from robber.tools.ast_edit import replace_symbol
from robber.tools.dispatcher import (
    FrontierToolDispatcher,
    looks_like_compiler_error,
    select_frontier_tool,
)
from robber.tools.workspace import select_tool


def test_select_frontier_and_workspace_tools() -> None:
    assert select_frontier_tool("analyze code in robber/ssot.py") == "AST Analyze"
    assert select_tool("analyze code in robber/ssot.py") == "AST Analyze"
    assert select_frontier_tool("replace function foo in bar.py with: pass") == "AST Edit"
    assert select_frontier_tool("scrape https://example.com") == "Web Scrape"
    assert looks_like_compiler_error("Traceback (most recent call last):\nSyntaxError: bad")


def test_analyze_and_ast_edit(tmp_path: Path) -> None:
    bind_mutations(FileMutationManager(tmp_path))
    target = tmp_path / "mod.py"
    target.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n", encoding="utf-8")
    digest = analyze_file("analyze code in mod.py", root=tmp_path)
    assert "foo" in digest
    assert "bar" in digest
    note = replace_symbol(
        "replace function foo in mod.py with:\ndef foo():\n    return 99\n",
        root=tmp_path,
    )
    assert "foo" in note
    assert "return 99" in target.read_text(encoding="utf-8")


def test_dispatcher_analyze(tmp_path: Path) -> None:
    bind_mutations(FileMutationManager(tmp_path))
    (tmp_path / "mod.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    dispatcher = FrontierToolDispatcher(tmp_path)

    async def go() -> None:
        report = await dispatcher.dispatch_query("analyze code in mod.py")
        assert report.ok
        assert report.name == "AST Analyze"
        assert "foo" in report.output

    asyncio.run(go())
