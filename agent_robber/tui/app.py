"""Agent Robber — Terminal UI (Module 03).

4-section TUI with Brain Orchestrator:
  [Header]      System title, active model count, UTC clock
  [Brain Pane]  Active task prompt + subtask tree with status badges
  [Model List]  Configured models with health badges
  [Footer]      Command input — any non-command text triggers orchestration
"""

from __future__ import annotations

import httpx
from datetime import datetime, timezone

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import (
    Header,
    Label,
    Select,
    Static,
    Button,
    Input,
)

API_BASE = "http://127.0.0.1:8765"
REFRESH_INTERVAL = 3.0


# ---------------------------------------------------------------------------
# Widgets — StatusBar (header)
# ---------------------------------------------------------------------------

class StatusBar(Static):
    clock = reactive("")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        self.clock = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    def render(self) -> str:
        return f" {self.clock} "


# ---------------------------------------------------------------------------
# Widgets — Brain Pane (top viewport section)
# ---------------------------------------------------------------------------

class BrainPane(Static):
    """Displays the active task prompt and its subtask decomposition tree."""

    task_data = reactive[dict | None](None)

    def render(self) -> str:
        if self.task_data is None:
            return (
                "  [bold yellow]BRAIN ORCHESTRATOR[/bold yellow]\n"
                "  [dim]No active task. Type a prompt in the footer to start.[/dim]"
            )

        t = self.task_data
        title = str(t.get("title", "Untitled"))
        prompt = str(t.get("prompt", ""))
        status = str(t.get("status", "IN_PROGRESS"))
        created = str(t.get("created_at", ""))
        subtasks = t.get("subtasks", [])

        lines: list[str] = []
        lines.append(f"  [bold yellow]BRAIN ORCHESTRATOR[/bold yellow]  [dim]{status}[/dim]")
        lines.append(f"  [bold]{title}[/bold]")
        if prompt:
            display_prompt = prompt[:100] + "..." if len(prompt) > 100 else prompt
            lines.append(f"  [dim]{display_prompt}[/dim]")
        if created:
            lines.append(f"  [dim]Started: {created[:19]}[/dim]")

        if subtasks:
            lines.append("")
            for st in subtasks:
                st_status = str(st.get("status", "QUEUED"))
                st_title = str(st.get("title", ""))
                model_alias = str(st.get("assigned_model_alias", ""))

                # Status badge
                badge_map = {
                    "QUEUED": "[dim][QUEUED][/dim]",
                    "IN_PROGRESS": "[yellow][IN_PROGRESS][/yellow]",
                    "COMPLETED": "[green][COMPLETED][/green]",
                    "FAILED": "[red][FAILED][/red]",
                }
                badge = badge_map.get(st_status, f"[dim][{st_status}][/dim]")

                # Model badge
                model_badge = f"  [cyan][Model: {model_alias}][/cyan]" if model_alias else ""

                lines.append(f"    {badge} {st_title}{model_badge}")
        else:
            lines.append("  [dim]No subtasks generated.[/dim]")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Widgets — Model List (bottom viewport section)
# ---------------------------------------------------------------------------

class ModelCard(Static):
    def __init__(self, model: dict, **kwargs) -> None:
        super().__init__(**kwargs)
        self.model = model

    def render(self) -> str:
        m = self.model
        alias = str(m.get("alias", ""))[:22]
        provider = str(m.get("provider", ""))[:12]
        model_str = str(m.get("model_string", ""))[:28]
        key = str(m.get("api_key", ""))[:16]
        health = str(m.get("health_status", "UNKNOWN"))

        badge_map = {
            "HEALTHY": "[green]HEALTHY[/green]",
            "OFFLINE": "[red]OFFLINE[/red]",
            "DEGRADED": "[yellow]DEGRADED[/yellow]",
            "UNKNOWN": "[dim]UNKNOWN[/dim]",
        }
        badge = badge_map.get(health, f"[dim]{health}[/dim]")

        return (
            f"  [bold cyan]{alias:<22}[/bold cyan]  "
            f"[magenta]{provider:<12}[/magenta]  "
            f"{model_str:<28}  "
            f"{key:<16}  "
            f"{badge}"
        )


class ModelList(Static):
    models = reactive[list[dict]]([])

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="model_scroll")

    def watch_models(self, new_models: list[dict]) -> None:
        scroll = self.query_one("#model_scroll", VerticalScroll)
        scroll.clear()
        if not new_models:
            scroll.mount(Static("  [dim]No models configured. Use /add to add one.[/dim]"))
            return
        for m in new_models:
            scroll.mount(ModelCard(m))


# ---------------------------------------------------------------------------
# Widgets — Command Input (footer)
# ---------------------------------------------------------------------------

class CommandInput(Static):
    input_text = reactive("")

    def render(self) -> str:
        return f"  > {self.input_text}_"


# ---------------------------------------------------------------------------
# Onboarding Screen (Module 02 carry-over)
# ---------------------------------------------------------------------------

class AddModelScreen(ModalScreen[dict | None]):
    CSS = """
    AddModelScreen { align: center middle; }
    #add_form {
        width: 72; height: auto; max-height: 30;
        background: $surface; border: solid $primary; padding: 1 2;
    }
    #add_form Label { margin-top: 1; }
    #add_form Input { margin-bottom: 0; }
    #add_form Select { margin-bottom: 0; }
    #form_status { margin-top: 1; height: 1; }
    #form_actions { margin-top: 1; height: 3; align: center middle; }
    #form_actions Button { margin: 0 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, providers: list[dict], default_url_map: dict[str, str]) -> None:
        super().__init__()
        self.providers = providers
        self.default_url_map = default_url_map

    def compose(self) -> ComposeResult:
        with Vertical(id="add_form"):
            yield Label("[bold]Add New Model[/bold]", id="form_title")
            provider_options = [
                (f"{p['preset_name']} ({p['category']})", p["id"])
                for p in self.providers
            ]
            yield Select(provider_options, id="sel_provider", prompt="Provider", allow_blank=False)
            yield Input(placeholder="e.g. my-gpt4o", id="inp_alias", title="Alias")
            yield Input(placeholder="e.g. gpt-4o", id="inp_model", title="Model String")
            yield Input(placeholder="https://api.openai.com/v1", id="inp_url", title="Base URL")
            yield Input(placeholder="sk-...", id="inp_key", title="API Key", password=True)
            yield Static("", id="form_status")
            with Horizontal(id="form_actions"):
                yield Button("[T] Test (Ping)", id="btn_ping", variant="default")
                yield Button("[S] Save", id="btn_save", variant="primary")
                yield Button("[Esc] Cancel", id="btn_cancel", variant="error")

    @on(Select.Changed, "#sel_provider")
    def _on_provider_changed(self, event: Select.Changed) -> None:
        pid = event.value
        url_input = self.query_one("#inp_url", Input)
        if not url_input.value or url_input.value in self.default_url_map.values():
            url_input.value = self.default_url_map.get(str(pid), "")

    @on(Button.Pressed, "#btn_ping")
    async def _on_ping(self) -> None:
        status = self.query_one("#form_status", Static)
        provider = str(self.query_one("#sel_provider", Select).value)
        base_url = self.query_one("#inp_url", Input).value
        api_key = self.query_one("#inp_key", Input).value
        model_str = self.query_one("#inp_model", Input).value or "gpt-4o-mini"
        status.update("[yellow]Pinging...[/yellow]")
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(f"{API_BASE}/api/v1/models/ping", json={
                    "provider": provider, "base_url": base_url,
                    "api_key": api_key, "model_string": model_str,
                })
                resp.raise_for_status()
                data = resp.json()
            status.update(f"[green]OK — {data['detail']}[/green]" if data.get("success")
                          else f"[red]FAIL — {data.get('detail', '?')}[/red]")
        except Exception as exc:
            status.update(f"[red]Error: {exc}[/red]")

    @on(Button.Pressed, "#btn_save")
    def _on_save(self) -> None:
        status = self.query_one("#form_status", Static)
        alias = self.query_one("#inp_alias", Input).value.strip()
        provider = str(self.query_one("#sel_provider", Select).value)
        model_str = self.query_one("#inp_model", Input).value.strip()
        base_url = self.query_one("#inp_url", Input).value.strip()
        api_key = self.query_one("#inp_key", Input).value.strip()
        if not alias:
            status.update("[red]Alias is required[/red]"); return
        if not model_str:
            status.update("[red]Model string is required[/red]"); return
        if provider == "Select":
            status.update("[red]Pick a provider[/red]"); return
        self.dismiss({"alias": alias, "provider": provider, "model_string": model_str,
                       "base_url": base_url, "api_key": api_key, "protocol": "openai"})

    @on(Button.Pressed, "#btn_cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class RobberTUI(App):
    """Agent Robber Terminal User Interface — Module 03 with Brain Orchestrator."""

    TITLE = "AGENT ROBBER v1.0.0"
    CSS = """
    Screen { background: $surface }
    #status_bar { dock: top; height: 1; background: $primary; color: $text; text-align: right; }
    #brain_pane { height: auto; max-height: 14; background: $surface-darken-1; border-bottom: solid $warning; }
    #model_list { height: 1fr; }
    #model_scroll { height: 1fr; }
    #command_input { dock: bottom; height: 3; background: $accent-darken-2; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+a", "add_model", "Add Model", show=True),
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(self, api_base: str = API_BASE) -> None:
        super().__init__()
        self.api_base = api_base
        self._refresh_timer: Timer | None = None
        self._providers: list[dict] = []
        self._default_urls: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield StatusBar(id="status_bar")
        yield BrainPane(id="brain_pane")
        yield ModelList(id="model_list")
        yield CommandInput(id="command_input")

    async def on_mount(self) -> None:
        await self._load_providers()
        self._refresh_timer = self.set_interval(REFRESH_INTERVAL, self._tick_refresh)
        await self._refresh_models()
        await self._refresh_brain()

    async def _load_providers(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self.api_base}/api/v1/providers")
                resp.raise_for_status()
                self._providers = resp.json()
                self._default_urls = {p["id"]: p["default_base_url"] for p in self._providers}
        except Exception:
            self._providers = [{"id": "openai", "preset_name": "OpenAI", "category": "cloud"}]
            self._default_urls = {"openai": "https://api.openai.com/v1"}

    async def _tick_refresh(self) -> None:
        await self._refresh_models()
        await self._refresh_brain()

    async def _refresh_models(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self.api_base}/api/v1/models")
                resp.raise_for_status()
                self.query_one("#model_list", ModelList).models = resp.json()
        except Exception:
            pass

    async def _refresh_brain(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self.api_base}/api/v1/tasks/latest")
                if resp.status_code == 200:
                    data = resp.json()
                    self.query_one("#brain_pane", BrainPane).task_data = data
        except Exception:
            pass

    # -- Key handling -------------------------------------------------------

    async def on_key(self, event) -> None:
        if self.screen.__class__.__name__ == "AddModelScreen":
            return

        cmd = self.query_one("#command_input", CommandInput)

        if event.key == "enter":
            text = cmd.input_text.strip()
            cmd.input_text = ""
            await self._handle_command(text)
            event.prevent_default()
            return

        if event.key == "backspace":
            cmd.input_text = cmd.input_text[:-1]
            event.prevent_default()
            return

        if event.character and len(event.character) == 1:
            cmd.input_text += event.character
            event.prevent_default()

    async def _handle_command(self, text: str) -> None:
        if not text:
            return

        # Slash commands
        if text in ("/exit", "/quit"):
            self.exit()
            return

        if text == "/health":
            await self._cmd_health()
            return

        if text == "/add":
            await self.action_add_model()
            return

        # Anything else → orchestrate
        await self._cmd_orchestrate(text)

    async def _cmd_health(self) -> None:
        cmd = self.query_one("#command_input", CommandInput)
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self.api_base}/health")
                resp.raise_for_status()
                data = resp.json()
            cmd.input_text = f"status={data['status']}  db={data['db_connected']}"
            self.set_timer(3.0, lambda: setattr(cmd, "input_text", ""))
        except Exception:
            cmd.input_text = "Server unreachable"
            self.set_timer(3.0, lambda: setattr(cmd, "input_text", ""))

    async def _cmd_orchestrate(self, prompt: str) -> None:
        cmd = self.query_one("#command_input", CommandInput)
        brain = self.query_one("#brain_pane", BrainPane)

        cmd.input_text = "Orchestrating..."
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(f"{self.api_base}/api/v1/orchestrate", json={"prompt": prompt})
                resp.raise_for_status()
                data = resp.json()

            # Show result in brain pane immediately
            task_data = {
                "title": data["title"],
                "prompt": data["prompt"],
                "status": "IN_PROGRESS",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "subtasks": data["subtasks"],
            }
            brain.task_data = task_data

            n = len(data["subtasks"])
            cmd.input_text = f"Decomposed into {n} subtask{'s' if n != 1 else ''}"
            self.set_timer(3.0, lambda: setattr(cmd, "input_text", ""))
        except Exception as exc:
            cmd.input_text = f"Error: {exc}"
            self.set_timer(4.0, lambda: setattr(cmd, "input_text", ""))

    async def action_add_model(self) -> None:
        screen = AddModelScreen(self._providers, self._default_urls)
        result = await self.push_screen_wait(screen)
        if result is None:
            return
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(f"{self.api_base}/api/v1/models", json=result)
                resp.raise_for_status()
            await self._refresh_models()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Standalone entry
# ---------------------------------------------------------------------------

def run_tui(api_base: str = API_BASE) -> None:
    app = RobberTUI(api_base=api_base)
    app.run()


if __name__ == "__main__":
    run_tui()
