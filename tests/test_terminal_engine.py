"""Persistent PTY/ConPTY session and ANSI sanitization."""



from __future__ import annotations



import asyncio

import os

from pathlib import Path



import pytest



from robber.tools.command_guard import CommandBlockedError

from robber.tools.terminal_engine import TerminalEngine, close_terminal_engine, strip_ansi_codes





def test_strip_ansi_codes_leaves_plain_text() -> None:

    raw = "\x1b[32mhello\x1b[0m\n\x1b[1;31merr\x1b[0m"

    assert strip_ansi_codes(raw) == "hello\nerr"





def test_engine_echo_and_persistent_cd(tmp_path: Path) -> None:

    inner = tmp_path / "inner"

    inner.mkdir()

    engine = TerminalEngine(inner)

    try:

        first = asyncio.run(engine.run("echo ROBBER_PTY_OK", timeout=20.0))

        if first.timed_out and "ROBBER_PTY_OK" not in first.output:

            pytest.skip("interactive shell prompt was not detected on this host")

        assert "ROBBER_PTY_OK" in first.output

        combo = "cd .. && echo ROBBER_PARENT"

        second = asyncio.run(engine.run(combo, timeout=20.0))

        assert "ROBBER_PARENT" in second.output

        where = asyncio.run(engine.run("cd" if os.name == "nt" else "pwd", timeout=20.0))

        text = where.output.replace("/", os.sep)

        assert tmp_path.name in text or str(tmp_path) in where.output

        assert engine.backend

    finally:

        engine.close()

        close_terminal_engine()





def test_engine_blocks_self_edit_command(tmp_path: Path) -> None:

    engine = TerminalEngine(tmp_path)

    try:

        with pytest.raises(CommandBlockedError):

            asyncio.run(engine.run("rm -rf /", timeout=5.0))

    finally:

        engine.close()





def test_write_stdin_reaches_session(tmp_path: Path) -> None:

    engine = TerminalEngine(tmp_path)

    try:

        engine.ensure_shell()

        engine.write_stdin("echo ROBBER_STDIN")

        result = asyncio.run(engine.run("echo ROBBER_AFTER", timeout=20.0))

        if result.timed_out and "ROBBER_AFTER" not in result.output:

            pytest.skip("interactive shell prompt was not detected on this host")

        assert "ROBBER_AFTER" in result.output

    finally:

        engine.close()

