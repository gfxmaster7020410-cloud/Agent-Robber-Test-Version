"""Native Textual workbench — terminal replica of the web dashboard."""

from __future__ import annotations

import argparse
import time
from typing import Any

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.geometry import Size
from textual.widgets import Static, TextArea

from robber.ansi import strip_ansi
from robber.diff_card import (
    MiniDiff,
    looks_like_unified_diff,
    mini_diff_from_code,
    mini_diff_from_file_card,
    parse_file_card,
    write_path_from_text,
)
from robber.markdown_ansi import SYNTAX_THEME, iter_markdown_blocks, lexer_name, looks_like_markdown
from robber.thoughts import looks_like_thought_leak, strip_thoughts
from robber.ssot_context import format_window
from robber.tui.client import DEFAULT_URL, DaemonClient
from robber.tui.diff_card import FileDiffCard
from robber.tui.prompt_io import pick_files, snippet_for_path, transcribe_utterance
from robber.tui.protocol import (
    KIND_LABEL,
    WorkbenchState,
    classify_stream,
    format_elapsed,
    mutation_rows,
    pretty_mode,
    session_id,
)

STAR = "✦"


class PromptArea(TextArea):
    """Enter dispatches the prompt. Shift+Enter inserts a newline. Paste never submits."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.app.action_submit_prompt()
            return
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key == "ctrl+o":
            event.stop()
            event.prevent_default()
            self.app.action_attach_files()
            return
        if event.key == "ctrl+m":
            event.stop()
            event.prevent_default()
            self.app.action_voice_input()
            return
        await super()._on_key(event)


def _kind_style(kind: str) -> str:
    if kind == "error":
        return "bold red"
    if kind == "terminal":
        return "bold green"
    if kind == "writing":
        return "bold cyan"
    if kind == "reply":
        return "bold white"
    return "bold yellow"


class HeaderBar(Static):
    def show(self, workspace: str, status: str) -> None:
        mark = "online" if status == "online" else "Offline"
        color = "green" if status == "online" else "red"
        self.update(
            Text.assemble(
                (" AGENT ROBBER ", "bold yellow"),
                (workspace or "workspace", "dim"),
                "  ",
                (mark, color),
            )
        )


def _folded_text(text: str = "", style: str = "") -> Text:
    renderable = Text(text or "", overflow="fold", no_wrap=False)
    if style:
        renderable.stylize(style)
    return renderable


class StreamChunk(Static):
    """Full-width stream paragraph. Wrap uses the stream pane, not the token length."""

    DEFAULT_CSS = """
    StreamChunk {
        width: 100%;
        max-width: 100%;
        min-width: 100%;
        height: auto;
        padding-right: 2;
        text-wrap: wrap;
        overflow-x: hidden;
    }
    """

    def __init__(self, renderable: object | None = None) -> None:
        super().__init__(markup=False, expand=True, shrink=True)
        self.raw = ""
        if renderable is not None:
            self.update(renderable)

    def get_content_width(self, container: Size, viewport: Size) -> int:
        parent = self.parent
        if parent is not None and parent.size.width >= 8:
            return max(8, parent.size.width - 4)
        if container.width >= 8:
            return container.width
        return max(60, viewport.width - 32)

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return super().get_content_height(container, viewport, max(width, 40))


class StreamLog(VerticalScroll):
    MAX_CHILDREN = 400

    def __init__(self) -> None:
        super().__init__(id="stream")
        self._last_kind: str | None = None
        self._cards: dict[str, FileDiffCard] = {}
        self._body: StreamChunk | None = None
        self._body_raw = ""
        self._body_ansi: Text | None = None

    def clear(self) -> None:
        self.remove_children()
        self._last_kind = None
        self._cards.clear()
        self._reset_body()

    def seed(self) -> None:
        self.clear()
        self.write_step("initiating", "Waiting for a prompt")
        self.write_step("writing", "File writes land here as cards + diffs")
        self.write_step("terminal", "Shell output streams into this column")
        self._last_kind = None
        self._reset_body()

    def _reset_body(self) -> None:
        self._body = None
        self._body_raw = ""
        self._body_ansi = None

    def _emit(self, renderable: object, *, as_body: bool = False) -> StreamChunk:
        chunk = StreamChunk(renderable)
        self.mount(chunk)
        if as_body:
            self._body = chunk
        self._trim()
        self.scroll_end(animate=False)
        return chunk

    def _trim(self) -> None:
        extras = len(self.children) - self.MAX_CHILDREN
        if extras <= 0:
            return
        for child in list(self.children[:extras]):
            if child is self._body:
                self._reset_body()
            if isinstance(child, FileDiffCard):
                self._cards.pop(child.model.path, None)
            child.remove()

    def _mount_diff(self, model: MiniDiff) -> None:
        existing = self._cards.get(model.path)
        if existing is not None:
            if existing.model.full == model.full and existing.model.added == model.added:
                return
            existing.update_model(model)
            self.scroll_end(animate=False)
            return
        card = FileDiffCard(model)
        self._cards[model.path] = card
        self.mount(card)
        self._reset_body()
        self._trim()
        self.scroll_end(animate=False)

    def _append_ansi(self, ansi: list[dict[str, Any]]) -> None:
        painted = self._body_ansi if self._body_ansi is not None else _folded_text()
        for span in ansi:
            text = str(span.get("text") or "")
            if not text:
                continue
            color = span.get("color")
            painted.append(text, style=str(color) if color else "")
        painted.overflow = "fold"
        painted.no_wrap = False
        self._body_ansi = painted

    def _paint_body(self, kind: str) -> None:
        raw = self._body_raw
        chunk = self._body
        if chunk is None:
            return
        chunk.raw = raw
        card = parse_file_card(raw)
        if card:
            chunk.remove()
            self._reset_body()
            self._mount_diff(mini_diff_from_file_card(card))
            return
        if looks_like_unified_diff(raw) and "```" not in raw:
            model = mini_diff_from_code(raw, language="diff")
            if model:
                chunk.remove()
                self._reset_body()
                self._mount_diff(model)
                return
        if (looks_like_markdown(raw) or "```" in raw) and raw.count("```") % 2 == 0:
            chunk.update(Markdown(raw, hyperlinks=False, code_theme=SYNTAX_THEME))
            return
        if self._body_ansi is not None:
            chunk.update(self._body_ansi)
            return
        style = "red" if kind == "error" else "default"
        chunk.update(_folded_text(raw, style))

    def write_step(
        self,
        kind: str,
        body: str,
        *,
        append: bool = False,
        ansi: list[dict[str, Any]] | None = None,
    ) -> None:
        plain = strip_thoughts(strip_ansi(body or ""))
        if looks_like_thought_leak(plain):
            return
        if not plain and append:
            return
        if not append or kind != self._last_kind:
            label = KIND_LABEL.get(kind) or (
                "return error!"
                if kind == "error"
                else "Terminal"
                if kind == "terminal"
                else "Writing"
                if kind == "writing"
                else "Reply"
                if kind == "reply"
                else "Initiating"
            )
            self._emit(_folded_text(f"{STAR} {label}", _kind_style(kind)))
            self._last_kind = kind
            self._reset_body()
        if not plain:
            return
        if append and self._body is not None:
            self._body_raw += plain
            if ansi:
                self._append_ansi(ansi)
            elif "\x1b[" in (body or "") and self._body_ansi is not None:
                extra = Text.from_ansi(body)
                extra.overflow = "fold"
                extra.no_wrap = False
                assembled = Text.assemble(self._body_ansi, extra)
                assembled.overflow = "fold"
                assembled.no_wrap = False
                self._body_ansi = assembled
            self._paint_body(kind)
            self.scroll_end(animate=False)
            return
        card = parse_file_card(plain)
        if card:
            self._mount_diff(mini_diff_from_file_card(card))
            return
        if looks_like_unified_diff(plain) and "```" not in (body or ""):
            model = mini_diff_from_code(plain, language="diff")
            if model:
                self._mount_diff(model)
                return
        self._body_raw = plain
        self._body_ansi = None
        if ansi:
            self._append_ansi(ansi)
            chunk = self._emit(self._body_ansi or _folded_text(plain), as_body=True)
            chunk.raw = plain
            return
        if kind != "error" and (looks_like_markdown(plain) or "```" in (body or "")):
            self._write_markdown(plain)
            return
        if "\x1b[" in (body or ""):
            painted = Text.from_ansi(body)
            painted.overflow = "fold"
            painted.no_wrap = False
            self._body_ansi = painted
            chunk = self._emit(painted, as_body=True)
            chunk.raw = plain
            return
        chunk = self._emit(
            _folded_text(plain, "red" if kind == "error" else "default"),
            as_body=True,
        )
        chunk.raw = plain

    def _write_markdown(self, body: str) -> None:
        last_path: str | None = None
        last_prose: StreamChunk | None = None
        accumulated = ""
        for kind, payload, language, path in iter_markdown_blocks(body):
            if kind == "code":
                last_prose = None
                hint = path or last_path
                model = mini_diff_from_code(payload, language=language, path=hint)
                if model:
                    self._mount_diff(model)
                    last_path = None
                    continue
                try:
                    renderable = Syntax(
                        payload,
                        lexer_name(language),
                        theme=SYNTAX_THEME,
                        word_wrap=True,
                        line_numbers=True,
                        padding=0,
                    )
                except Exception:
                    renderable = Syntax(
                        payload,
                        "text",
                        theme=SYNTAX_THEME,
                        word_wrap=True,
                        line_numbers=True,
                        padding=0,
                    )
                self._emit(renderable)
            else:
                last_path = write_path_from_text(payload) or last_path
                if looks_like_unified_diff(payload):
                    model = mini_diff_from_code(payload, language="diff")
                    if model:
                        self._mount_diff(model)
                        last_prose = None
                        continue
                last_prose = self._emit(
                    Markdown(payload, hyperlinks=False, code_theme=SYNTAX_THEME),
                    as_body=True,
                )
                last_prose.raw = payload
                accumulated = payload
        if last_prose is not None:
            self._body = last_prose
            self._body_raw = accumulated
            self._body_ansi = None
        else:
            self._reset_body()


class SidebarPane(Static):
    def render_state(self, state: WorkbenchState) -> None:
        packed = format_window(state.context_packed) if state.context_packed else "0"
        window = format_window(state.context_window) if state.context_window else "200K"
        lines: list[str] = [
            "[bold]Unified Context & Memory (SSOT)[/bold]",
            f"[dim]{packed} / {window}[/dim]",
            f"{state.ssot_files} files - {state.ssot_chunks} chunks · {state.ssot_backend}",
            "",
            "[bold]MCP Servers & Tools[/bold]",
            f"[dim]Active MCPs: {state.mcp_online} Online[/dim]",
        ]
        if state.mcp_servers:
            for row in state.mcp_servers[:8]:
                name = str(row.get("label") or row.get("id") or "?")
                flag = "online" if row.get("enabled") else "off"
                color = "green" if row.get("enabled") else "dim"
                lines.append(f"  {name}  [{color}]{flag}[/]")
        else:
            lines.append("  [dim]No MCP servers linked[/dim]")
        lines.extend(
            [
                "",
                "[bold]Active Workspace & Modified Files[/bold]",
                f"[dim]{len(state.mutations)} Files Changed[/dim]",
            ]
        )
        if state.mutations:
            for item in state.mutations[:16]:
                lines.append(item.get("line") or f"[{item.get('marker', '~')}] {item.get('path', '')} {item.get('detail', '')}")
        else:
            lines.append("[dim]No file mutations this session[/dim]")
        tps = state.tps if state.tps else 0
        lines.extend(
            [
                "",
                "[bold]Token Tracker[/bold]",
                f"{tps:g} t/s",
                f"[dim]{state.ttft_ms}ms ttft[/dim]",
                f"[dim]loop {state.loop_phase}  {state.turn_tokens}/{state.max_tokens or '-'} tok[/dim]",
                f"[dim]locks stale {state.stale_locks}  graph {state.graph_nodes} {state.graph_head}[/dim]",
                f"[dim]router {state.router_engine or '-'}[/dim]",
                (
                    f"[dim]horizon {state.horizon_status} {state.horizon_cursor}/{state.horizon_total}[/dim]"
                    if state.horizon_total
                    else "[dim]horizon -[/dim]"
                ),
            ]
        )
        self.update("\n".join(lines))


class TimerLine(Static):
    def show(self, elapsed: str) -> None:
        self.update(Text(elapsed, style="dim"))


class BadgeLine(Static):
    def show(self, state: WorkbenchState, session: str) -> None:
        model = "Hybrid Multi-LLM" if state.hybrid else (state.model or "Hybrid LLM")
        control = "Manual" if state.control == "manual" else "Autonomous"
        self.update(
            Text.assemble(
                (f"[{pretty_mode(state.mode)} Mode]", "bold"),
                (" | ", "dim"),
                (f"[{model}]", "bold"),
                (" | ", "dim"),
                (f"[{control}]", "bold"),
                (" | ", "dim"),
                (f"[{state.loop_phase} {state.turn_tokens} tok]", "dim"),
                (" | ", "dim"),
                (f"session ID-{session}", "dim"),
                ("  ", ""),
                ("[F1: Beast] [F2: History] [F3: Kill Task]", "yellow"),
            )
        )


class StepLine(Static):
    DEFAULT_CSS = """
    StepLine { color: #8b8b9a; }
    """

    def on_mount(self) -> None:
        self.update(
            "[Ctrl+O Attach File/Media]  [Ctrl+M Voice]  [Ctrl+Z Undo]  [Ctrl+Y Redo]  "
            "[Ctrl+A Accept All]  [Ctrl+R Revert All]  [Tab Sidebar]"
        )


class HistoryPane(Static):
    def show(self, rows: list[str]) -> None:
        if not rows:
            self.update("[dim]No prompts yet[/dim]")
            return
        body = "\n".join(f"  {item}" for item in reversed(rows[-24:]))
        self.update(f"[bold yellow]History[/bold yellow]\n{body}")


class WorkbenchApp(App[None]):
    TITLE = "AGENT ROBBER"
    CSS = """
    Screen { background: #181820; color: #e8e8ed; }
    #header { dock: top; height: 1; background: #181820; color: #ffd400; padding: 0 1; }
    #body { height: 1fr; }
    #main { width: 7fr; height: 1fr; }
    #stream {
        width: 1fr;
        min-width: 1fr;
        height: 1fr;
        background: #181820;
        padding: 0 2;
        scrollbar-size-vertical: 1;
        overflow-x: hidden;
        overflow-y: auto;
    }
    #stream > StreamChunk,
    #stream > FileDiffCard {
        width: 100%;
        max-width: 100%;
        min-width: 100%;
        height: auto;
        padding-right: 2;
        text-wrap: wrap;
    }
    #history {
        display: none;
        dock: top;
        height: 10;
        background: #232330;
        border: solid #2d2d3d;
        padding: 0 1;
    }
    #history.-open { display: block; }
    #sidebar {
        width: 3fr;
        min-width: 28;
        height: 1fr;
        background: #1c1c28;
        border-left: solid #2d2d3d;
        padding: 1;
        overflow-y: auto;
    }
    #sidebar.-hidden { display: none; width: 0; min-width: 0; }
    #chrome { dock: bottom; height: 9; background: #181820; border-top: solid #2d2d3d; }
    #timer { height: 1; color: #8b8b9a; padding: 0 1; }
    #badges { height: 1; padding: 0 1; }
    #prompt { height: 5; margin: 0 1; border: solid #2d2d3d; background: #232330; }
    #prompt:focus { border: solid #ffd400; }
    #steps { height: 1; padding: 0 1; }
    """

    BINDINGS = [
        Binding("ctrl+c", "kill_task", "Kill Task", show=True, priority=True),
        Binding("f3", "kill_task", "Kill Task", show=False, priority=True),
        Binding("ctrl+b", "beast", "Beast", show=True, priority=True),
        Binding("f1", "beast", "Beast", show=False, priority=True),
        Binding("f2", "history", "History", show=False, priority=True),
        Binding("ctrl+z", "undo_step", "Undo", show=True, priority=True),
        Binding("ctrl+y", "redo_step", "Redo", show=True, priority=True),
        Binding("ctrl+a", "accept_all", "Accept All", show=True, priority=True),
        Binding("ctrl+r", "revert_all", "Revert All", show=True, priority=True),
        Binding("ctrl+s", "toggle_sidebar", "Sidebar", show=True, priority=True),
        Binding("tab", "toggle_sidebar", "Sidebar", show=False, priority=True),
        Binding("ctrl+p", "plan_mode", "Plan", show=False, priority=True),
        Binding("ctrl+l", "clear_stream", "Clear", show=False, priority=True),
        Binding("ctrl+enter", "submit_prompt", "Send", show=True, priority=True),
        Binding("ctrl+o", "attach_files", "Attach", show=True, priority=True),
        Binding("ctrl+m", "voice_input", "Voice", show=True, priority=True),
        Binding("ctrl+q", "quit", "Quit", show=True, priority=True),
    ]

    def __init__(self, url: str = DEFAULT_URL, *, connect: bool = True) -> None:
        super().__init__()
        self.url = url
        self._connect = connect
        self.state = WorkbenchState()
        self.session = session_id()
        self.client = DaemonClient(url, on_frame=self._on_frame, on_status=self._on_status)
        self._turn_started: float | None = None
        self._frozen_ms = 0
        self._beast = False
        self._listening = False
        self._history_loaded = False

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield HistoryPane(id="history")
                yield StreamLog()
            with VerticalScroll(id="sidebar"):
                yield SidebarPane(id="side-body")
        with Vertical(id="chrome"):
            yield TimerLine(id="timer")
            yield BadgeLine(id="badges")
            yield PromptArea(
                "",
                id="prompt",
                show_line_numbers=False,
                tab_behavior="focus",
            )
            yield StepLine(id="steps")

    def on_mount(self) -> None:
        self.query_one(StreamLog).seed()
        self._paint()
        self.set_interval(0.25, self._tick_timer)
        self.query_one("#prompt", TextArea).focus()
        if self._connect:
            self._ws_loop()

    def on_unmount(self) -> None:
        self.client.stop()

    @work(exclusive=True, group="ws")
    async def _ws_loop(self) -> None:
        await self.client.run()

    async def _on_status(self, status: str) -> None:
        self.state.status = status
        self._paint()

    async def _on_frame(self, frame: dict[str, Any]) -> None:
        kind = str(frame.get("type") or "")
        event = str(frame.get("event") or "")
        frame_id = str(frame.get("id") or "")
        if kind == "ping":
            return
        if frame_id:
            if frame_id in self.state.seen_ids:
                return
            self.state.seen_ids.add(frame_id)
            if len(self.state.seen_ids) > 4000:
                self.state.seen_ids = set(list(self.state.seen_ids)[-2000:])

        kind_l = kind.lower()
        event_l = event.upper()
        if kind_l == "loop_state":
            meta = frame.get("metadata") if isinstance(frame.get("metadata"), dict) else {}
            self.state.ingest_loop(meta)
            self._paint()
            return
        if kind_l == "history":
            self._apply_history(frame)
            return
        is_log = kind_l in {"message", "error", "log"} or event_l == "LOG_STREAM"
        if is_log and kind_l != "state":
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            role = str(frame.get("role") or ("error" if kind_l == "error" else "system"))
            content = str(frame.get("content") or payload.get("content") or "")
            if role.lower() == "reasoning" and not content.lstrip().startswith("goal"):
                if kind_l != "state":
                    return
            content = strip_thoughts(content)
            stream_kind = str(frame.get("stream_kind") or payload.get("type") or "")
            append = frame.get("append") is True
            if content:
                mapped, _label = classify_stream(role=role, content=content, stream_kind=stream_kind)
                ansi = frame.get("ansi")
                if not isinstance(ansi, list) and isinstance(payload, dict):
                    ansi = payload.get("ansi")
                self.query_one(StreamLog).write_step(
                    mapped,
                    content,
                    append=append,
                    ansi=ansi if isinstance(ansi, list) else None,
                )
                if mapped == "initiating" and role == "user":
                    first = content.strip().splitlines()[0] if content.strip() else ""
                    if first and not first.startswith("/") and "Mode switched" not in first:
                        self.state.remember_prompt(first)
            if kind_l != "state":
                return

        if kind_l == "state" or event_l == "STATE_SYNC":
            self.state.ingest_state(frame)
            if not self.state.busy:
                self.state.busy_label = ""
            self._paint()
            return

        if kind == "mutations" or event in {"FILE_MUTATION_UPDATE", "MUTATION_SYNC"}:
            payload = frame.get("payload") or frame.get("mutations") or frame
            self.state.mutations = mutation_rows(payload)
            self._paint_sidebar()
            return

        if kind == "busy":
            self.state.busy = True
            self.state.busy_label = str(frame.get("label") or "Working...")
            self._paint()
            return

    def _apply_history(self, frame: dict[str, Any]) -> None:
        if self._history_loaded:
            return
        self._history_loaded = True
        raw_turns = frame.get("turns")
        turns = raw_turns if isinstance(raw_turns, list) else []
        if not turns:
            return
        stream = self.query_one(StreamLog)
        stream.clear()
        for item in turns[-50:]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "")
            if not content.strip():
                continue
            role = str(item.get("role") or "system").strip().lower()
            if role == "error":
                kind = "error"
            elif role in {"assistant", "success"}:
                kind = "reply"
            else:
                kind = "initiating"
            stream.write_step(kind, content)

    def _tick_timer(self) -> None:
        working = self.state.busy or bool(self.state.busy_label)
        clock = time.monotonic()
        if working and self._turn_started is None:
            self._turn_started = clock
        if not working and self._turn_started is not None:
            self._frozen_ms = int((clock - self._turn_started) * 1000)
            self._turn_started = None
        if working and self._turn_started is not None:
            ms = int((clock - self._turn_started) * 1000)
        else:
            ms = int(self.state.elapsed_ms or self._frozen_ms)
        try:
            self.query_one("#timer", TimerLine).show(format_elapsed(ms))
        except NoMatches:
            pass

    def _paint(self) -> None:
        try:
            self.query_one("#header", HeaderBar).show(self.state.workspace, self.state.status)
            self.query_one("#badges", BadgeLine).show(self.state, self.session)
            self._paint_sidebar()
            history = self.query_one("#history", HistoryPane)
            history.show(self.state.history)
        except NoMatches:
            pass

    def _paint_sidebar(self) -> None:
        try:
            self.query_one("#side-body", SidebarPane).render_state(self.state)
        except NoMatches:
            pass

    def _send(self, payload: dict[str, Any], offline: str) -> None:
        if self.client.send(payload):
            return
        self.query_one(StreamLog).write_step("error", offline)

    def action_toggle_sidebar(self) -> None:
        self.query_one("#sidebar").toggle_class("-hidden")

    def action_history(self) -> None:
        pane = self.query_one("#history", HistoryPane)
        pane.toggle_class("-open")
        pane.show(self.state.history)

    def action_kill_task(self) -> None:
        self._send({"type": "KILL_TASK"}, "Daemon offline — cannot kill the task.")

    def action_beast(self) -> None:
        self._beast = not self._beast
        mode = "Beast Mode" if self._beast else "Autonomous"
        self._send({"type": "SET_MODE", "mode": mode}, "Daemon offline — cannot switch Beast routing.")

    def action_plan_mode(self) -> None:
        self._beast = False
        self._send({"type": "SET_MODE", "mode": "Plan Mode"}, "Daemon offline — cannot switch to plan mode.")

    def action_undo_step(self) -> None:
        self._send({"type": "TRIGGER_STEP_ACTION", "action": "undo"}, "Daemon offline — cannot undo.")

    def action_redo_step(self) -> None:
        self._send({"type": "TRIGGER_STEP_ACTION", "action": "redo"}, "Daemon offline — cannot redo.")

    def action_accept_all(self) -> None:
        self.client.send({"type": "heal_approve"})
        self._send({"type": "TRIGGER_STEP_ACTION", "action": "accept_all"}, "Daemon offline — cannot accept.")

    def action_revert_all(self) -> None:
        self.client.send({"type": "heal_reject"})
        self._send({"type": "TRIGGER_STEP_ACTION", "action": "revert_all"}, "Daemon offline — cannot revert.")

    def action_clear_stream(self) -> None:
        self.query_one(StreamLog).seed()

    def _append_prompt(self, chunk: str) -> None:
        area = self.query_one("#prompt", TextArea)
        current = (area.text or "").rstrip()
        gap = "\n\n" if current else ""
        area.load_text(f"{current}{gap}{chunk}")
        area.focus()

    def action_attach_files(self) -> None:
        paths = pick_files()
        if not paths:
            return
        parts = [snippet_for_path(path) for path in paths]
        self._append_prompt("\n\n".join(parts))

    def action_voice_input(self) -> None:
        if self._listening:
            self._listening = False
            return
        self._listening = True
        self.query_one(StreamLog).write_step("initiating", "Voice input listening — speak, or Ctrl+M to cancel")
        self._voice_loop()

    @work(exclusive=True, group="voice", thread=True)
    def _voice_loop(self) -> None:
        heard = transcribe_utterance() if self._listening else ""

        def finish() -> None:
            self._listening = False
            if heard:
                current = self.query_one("#prompt", TextArea).text or ""
                gap = " " if current and not current.endswith((" ", "\n")) else ""
                self.query_one("#prompt", TextArea).load_text(f"{current}{gap}{heard}")
                self.query_one("#prompt", TextArea).focus()
            else:
                self.query_one(StreamLog).write_step(
                    "error",
                    "Voice input heard nothing. Allow microphone access and try Ctrl+M again.",
                )

        self.call_from_thread(finish)

    def on_paste(self, event: events.Paste) -> None:
        from robber.paste_buffer import ingest_notice, is_huge_paste, save_paste_buffer

        chunk = event.text or ""
        if not is_huge_paste(chunk):
            return
        event.stop()
        stats = save_paste_buffer(chunk)
        area = self.query_one("#prompt", TextArea)
        existing = (area.text or "").rstrip()
        area.load_text(f"{existing}\n{stats.marker}".strip())
        self.query_one(StreamLog).write_step("initiating", ingest_notice(stats))

    def action_submit_prompt(self) -> None:
        area = self.query_one("#prompt", TextArea)
        text = (area.text or "").strip()
        if not text:
            return
        area.load_text("")
        self._dispatch_prompt(text)

    def _dispatch_prompt(self, text: str) -> None:
        if not text:
            return
        if self.state.busy and not text.startswith("/"):
            self.query_one(StreamLog).write_step("error", "Agent is busy — wait for the current turn")
            return
        if text in {"/exit", "/quit"}:
            self.exit()
            return
        if text == "/clear":
            self.action_clear_stream()
            return
        if text in {"/help", "/h", "/?"}:
            self.query_one(StreamLog).write_step(
                "initiating",
                "Shortcuts: Enter send · Shift+Enter newline · Ctrl+O attach file/media · Ctrl+M voice · "
                "Ctrl+C/F3 Kill · F1 Beast · F2 History · "
                "Ctrl+Z Undo · Ctrl+Y Redo · Ctrl+A Accept · Ctrl+R Revert · Tab/Ctrl+S Sidebar · Ctrl+Q Quit\n"
                "Slash: /clear /status /mode /memory /window /mcp /snapshots /restore /purge /help /exit",
            )
            return
        if text.startswith("/"):
            name, *rest = text[1:].split()
            if not self.client.send(
                {
                    "type": "slash_command",
                    "command": name,
                    "args": rest,
                    "session_id": self.session,
                    "text": text,
                }
            ):
                self.query_one(StreamLog).write_step("error", f"Daemon offline — cannot run {text}")
            return
        self.state.remember_prompt(text)
        if not self.client.send({"type": "EXECUTE_PROMPT", "text": text, "prompt": text}):
            self.query_one(StreamLog).write_step("initiating", text)
            self.query_one(StreamLog).write_step(
                "error",
                "Daemon unreachable — start it with python server.py, then this TUI reconnects.",
            )


def run(url: str = DEFAULT_URL) -> None:
    WorkbenchApp(url=url).run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Robber native workbench TUI")
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="Daemon WebSocket URL (default: ws://127.0.0.1:8787/ws/dashboard)",
    )
    args = parser.parse_args()
    run(url=args.url)
