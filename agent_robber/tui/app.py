"""Agent Robber — Terminal UI (Module 01).

3-pane Textual TUI:
  [Header]  System title, active model count, UTC clock
  [Viewport] Live model list fetched from the local API server
  [Footer]  Command input accepting /health and /exit
"""

from __future__ import annotations

import asyncio
import httpx
from datetime import datetime, timezone

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Footer, Header, Label, Static

API_BASE = "http://127.0.0.1:8765"
REFRESH_INTERVAL = 3.0  # seconds


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class StatusBar(Static):
    """Header-right status + UTC clock."""

    clock = reactive("")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        self.clock = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    def render(self) -> str:
        return f" {self.clock} "


class ModelList(Static):
    """Main viewport — displays configured models."""

    models = reactive[list[dict]]([])

    def render(self) -> str:
        if not self.models:
            return "\n  [dim]No models configured.[/dim]\n  [dim]POST /api/v1/models to add one.[/dim]\n"

        lines: list[str] = []
        lines.append(f"  {'ID':<10} {'Alias':<20} {'Provider':<14} {'Model':<30} {'Key'}")
        lines.append(f"  {'─'*10} {'─'*20} {'─'*14} {'─'*30} {'─'*15}")
        for m in self.models:
            mid = str(m.get("id", ""))[:8]
            alias = str(m.get("alias", ""))[:20]
            provider = str(m.get("provider", ""))[:14]
            model_str = str(m.get("model_string", ""))[:30]
            key = str(m.get("api_key", ""))[:15]
            active = "✓" if m.get("is_active") else "✗"
            lines.append(f"  {mid:<10} {alias:<20} {provider:<14} {model_str:<30} {key}  {active}")
        return "\n".join(lines)


class CommandInput(Static):
    """Footer command bar."""

    prompt = reactive("> ")
    output = reactive("")

    def render(self) -> str:
        if self.output:
            return f"  {self.output}"
        return f"  {self.prompt}"


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class RobberTUI(App):
    """Agent Robber Terminal User Interface."""

    TITLE = "AGENT ROBBER v1.0.0"
    CSS = """
    Screen { background: $surface }
    #status_bar { dock: top; height: 1; background: $primary; color: $text; text-align: right; }
    #model_list { height: 1fr; overflow-y: auto; }
    #command_input { dock: bottom; height: 3; background: $accent-darken-2; }
    #header_title { dock: top; height: 1; background: $primary; color: $text; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, api_base: str = API_BASE) -> None:
        super().__init__()
        self.api_base = api_base
        self._refresh_timer: Timer | None = None
        self._cmd_output = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield StatusBar(id="status_bar")
        yield ModelList(id="model_list")
        yield CommandInput(id="command_input")

    async def on_mount(self) -> None:
        self._refresh_timer = self.set_interval(REFRESH_INTERVAL, self._refresh_models)
        await self._refresh_models()

    async def _refresh_models(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self.api_base}/api/v1/models")
                resp.raise_for_status()
                models = resp.json()
            self.query_one("#model_list", ModelList).models = models
            header = self.query_one("#header_title", Label)
            header.update(f"  AGENT ROBBER v1.0.0  |  Models: {len(models)}")
        except Exception:
            pass

    async def on_key(self, event) -> None:
        """Handle keystrokes in command mode."""
        cmd_widget = self.query_one("#command_input", CommandInput)

        if event.key == "enter":
            text = cmd_widget.output.strip()
            cmd_widget.output = ""
            await self._handle_command(text)
            return

        if event.key == "backspace":
            cmd_widget.output = cmd_widget.output[:-1]
            event.prevent_default()
            return

        if event.character and len(event.character) == 1:
            cmd_widget.output += event.character
            event.prevent_default()

    async def _handle_command(self, cmd: str) -> None:
        cmd_widget = self.query_one("#command_input", CommandInput)

        if not cmd:
            return

        if cmd == "/exit" or cmd == "/quit":
            self.exit()
            return

        if cmd == "/health":
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(f"{self.api_base}/health")
                    resp.raise_for_status()
                    data = resp.json()
                cmd_widget.output = f"  status={data['status']}  db={data['db_connected']}  ts={data['timestamp']}"
            except Exception as exc:
                cmd_widget.output = f"  [red]Error: {exc}[/red]"
            return

        cmd_widget.output = f"  Unknown command: {cmd}  (try /health or /exit)"


# ---------------------------------------------------------------------------
# Standalone entry
# ---------------------------------------------------------------------------

def run_tui(api_base: str = API_BASE) -> None:
    app = RobberTUI(api_base=api_base)
    app.run()


if __name__ == "__main__":
    run_tui()
