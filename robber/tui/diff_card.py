from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static
from textual.widget import Widget
from rich.syntax import Syntax
from rich.text import Text

from robber.diff_card import MiniDiff, looks_like_unified_diff
from robber.markdown_ansi import SYNTAX_THEME, lexer_name

ADD_STYLE = "#22c55e on #052e16"
DEL_STYLE = "#ef4444 on #450a0a"


class FileDiffCard(Widget, can_focus=True):
    """Compact, focusable mini-diff. Click or Enter toggles the full file."""

    BINDINGS = [
        Binding("enter", "toggle", "Expand", show=False),
        Binding("space", "toggle", "Expand", show=False),
    ]

    DEFAULT_CSS = """
    FileDiffCard {
        height: auto;
        width: 1fr;
        margin: 0 0 1 0;
        padding: 0 1;
        background: #171717;
        border: solid #404040;
        color: #e8e8ed;
    }
    FileDiffCard:focus {
        border: solid #ffd400;
    }
    FileDiffCard .fdc-head {
        height: auto;
        width: 1fr;
        color: #e8e8ed;
    }
    FileDiffCard .fdc-body {
        height: auto;
        width: 1fr;
        margin-top: 0;
    }
    """

    def __init__(self, model: MiniDiff) -> None:
        super().__init__()
        self.model = model
        self.expanded = False

    def compose(self) -> ComposeResult:
        yield Static(classes="fdc-head")
        yield Static(classes="fdc-body")

    def on_mount(self) -> None:
        self._paint()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.action_toggle()

    def action_toggle(self) -> None:
        self.expanded = not self.expanded
        self._paint()

    def update_model(self, model: MiniDiff) -> None:
        self.model = model
        self._paint()

    def _paint(self) -> None:
        hint = "Enter to collapse" if self.expanded else "Click or Press Enter to Expand"
        head = Text()
        head.append(f"[{self.model.marker}] ", "bold")
        head.append(self.model.path, "bold")
        head.append(" (")
        head.append(f"+{self.model.added}", "#22c55e")
        head.append(", ")
        head.append(f"-{self.model.removed}", "#ef4444")
        head.append(f") [{hint}]", "dim")
        self.query_one(".fdc-head", Static).update(head)
        body = self.query_one(".fdc-body", Static)
        if self.expanded:
            lang = "diff" if looks_like_unified_diff(self.model.full) else self.model.language
            try:
                renderable = Syntax(
                    self.model.full or " ",
                    lexer_name(lang),
                    theme=SYNTAX_THEME,
                    word_wrap=True,
                    line_numbers=True,
                    padding=0,
                )
            except Exception:
                renderable = Syntax(
                    self.model.full or " ",
                    "text",
                    theme=SYNTAX_THEME,
                    word_wrap=True,
                    line_numbers=True,
                    padding=0,
                )
            body.update(renderable)
            return
        preview = Text()
        shown = self.model.preview[:3]
        for kind, text in shown:
            preview.append("│ ", "dim")
            if kind == "add":
                preview.append(f"{text}\n", ADD_STYLE)
            elif kind == "del":
                preview.append(f"{text}\n", DEL_STYLE)
            else:
                preview.append(f"{text}\n", "dim")
        preview.append(f"│ ... ({len(shown) or 0} lines shown)", "dim")
        body.update(preview)
