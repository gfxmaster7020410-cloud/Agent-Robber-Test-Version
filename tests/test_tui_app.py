import asyncio
from pathlib import Path

from robber.tui.app import StreamChunk, StreamLog, WorkbenchApp
from robber.tui.diff_card import FileDiffCard
from textual.widgets import TextArea


def test_workbench_layout_and_sidebar_toggle(tmp_path: Path, monkeypatch) -> None:
    async def go() -> None:
        app = WorkbenchApp(url="ws://127.0.0.1:9/ws", connect=False)
        async with app.run_test(size=(140, 40)) as pilot:
            assert app.query_one("#prompt")
            assert app.query_one("#stream")
            assert app.query_one("#timer")
            assert app.query_one("#badges")
            assert app.query_one("#steps")
            sidebar = app.query_one("#sidebar")
            assert not sidebar.has_class("-hidden")
            await pilot.press("ctrl+s")
            assert sidebar.has_class("-hidden")
            await pilot.press("tab")
            assert not sidebar.has_class("-hidden")
            await pilot.press("f2")
            assert app.query_one("#history").has_class("-open")
            prompt = app.query_one("#prompt", TextArea)
            prompt.load_text("scan the project files")
            await pilot.press("enter")
            assert "scan the project files" in app.state.history
            prompt.load_text("line one")
            await pilot.press("shift+enter")
            assert "line one" in prompt.text
            assert "\n" in prompt.text
            assert "line one" not in app.state.history
            sample = tmp_path / "attach.txt"
            sample.write_text("hello attach\n", encoding="utf-8")
            monkeypatch.setattr("robber.tui.app.pick_files", lambda: [str(sample)])
            app.action_attach_files()
            assert "Attached attach.txt" in prompt.text
            assert "hello attach" in prompt.text
            footer = str(app.query_one("#steps").render())
            assert "Ctrl+O Attach File/Media" in footer
            assert "📎" not in footer
            assert "🎤" not in footer

    asyncio.run(go())


def test_file_diff_card_toggles_on_enter() -> None:
    async def go() -> None:
        app = WorkbenchApp(url="ws://127.0.0.1:9/ws", connect=False)
        async with app.run_test(size=(140, 40)) as pilot:
            stream = app.query_one(StreamLog)
            stream.write_step(
                "writing",
                "```diff\n--- a/gui/src/lib/orchestration.ts\n+++ b/gui/src/lib/orchestration.ts\n"
                "@@ -1,4 +1,3 @@\n-// raw response stream\n+import { stripThoughts } from './parser';\n+two\n```\n",
            )
            await pilot.pause()
            card = app.query_one(FileDiffCard)
            assert card.model.path == "gui/src/lib/orchestration.ts"
            assert card.expanded is False
            assert card.model.added >= 1
            card.focus()
            await pilot.press("enter")
            assert card.expanded is True
            await pilot.press("enter")
            assert card.expanded is False

    asyncio.run(go())


def test_writing_stream_concatenates_and_uses_pane_width() -> None:
    async def go() -> None:
        app = WorkbenchApp(url="ws://127.0.0.1:9/ws", connect=False)
        async with app.run_test(size=(140, 40)) as pilot:
            stream = app.query_one(StreamLog)
            stream.write_step("writing", "The agent will now scan ")
            stream.write_step("writing", "the entire workspace for config ", append=True)
            stream.write_step("writing", "files and report what it finds.", append=True)
            await pilot.pause()
            bodies = [child for child in stream.query(StreamChunk) if "The agent will now scan" in (child.raw or "")]
            assert len(bodies) == 1
            body = bodies[0]
            assert "the entire workspace for config" in body.raw
            assert "files and report what it finds." in body.raw
            assert body.size.width >= 70
            assert stream.size.width >= 80
            measured = body.get_content_width(stream.size, app.size)
            assert measured >= 70

    asyncio.run(go())


def test_history_frame_replaces_seed_once() -> None:
    async def go() -> None:
        app = WorkbenchApp(url="ws://127.0.0.1:9/ws", connect=False)
        async with app.run_test(size=(140, 40)) as pilot:
            await app._on_frame(
                {
                    "type": "history",
                    "session_id": "current_session",
                    "turns": [
                        {"id": 1, "role": "user", "content": "hello from db"},
                        {"id": 2, "role": "assistant", "content": "reply from db"},
                    ],
                }
            )
            await pilot.pause()
            stream = app.query_one(StreamLog)
            bodies = " ".join(child.raw or "" for child in stream.query(StreamChunk))
            assert "hello from db" in bodies
            assert "reply from db" in bodies
            assert "Waiting for a prompt" not in bodies
            assert app._history_loaded is True
            await app._on_frame(
                {
                    "type": "history",
                    "turns": [{"id": 3, "role": "user", "content": "should skip"}],
                }
            )
            await pilot.pause()
            later = " ".join(child.raw or "" for child in stream.query(StreamChunk))
            assert "should skip" not in later

    asyncio.run(go())
