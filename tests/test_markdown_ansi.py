from robber.ansi import parse_ansi
from robber.canvas import build_canvas
from robber.markdown_ansi import (
    ACT_SILENT_DIRECTIVE,
    colorize_for_emit,
    highlight_lines,
    render_markdown_ansi,
    render_syntax_ansi,
)
from robber.router import system_prompt_for


def test_canvas_omits_numbered_steps_in_act() -> None:
    act = build_canvas("scan files", mode="act", engine="x", tool="scan", live=True)
    assert "1. Recall SSOT" not in act
    plan = build_canvas("scan files", mode="plan", engine="x", tool="scan", live=True)
    assert "1. Recall SSOT" in plan


def test_act_system_prompt_forbids_numbered_workflow() -> None:
    prompt = system_prompt_for("act")
    assert ACT_SILENT_DIRECTIVE in prompt
    assert "numbered steps" in prompt
    assert "AGENT ROBBER" in prompt
    assert "UNRESTRICTED OS ACCESS" in prompt
    assert "workspace jail" in prompt.lower()
    assert "NEVER hallucinate" in prompt
    plan = system_prompt_for("plan")
    assert ACT_SILENT_DIRECTIVE not in plan
    inquiry = system_prompt_for("act", intent="inquiry")
    assert ACT_SILENT_DIRECTIVE not in inquiry
    assert "1-3 sentences" in inquiry
    assert "dummy projects" in inquiry


def test_diff_highlight_uses_green_and_red() -> None:
    raw = "--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new\nindex 0000000..e69de29\n"
    colored = highlight_lines(raw)
    assert "\033[32m+new" in colored
    assert "\033[31m-old" in colored
    plain, segments = parse_ansi(colored)
    assert "index 0000000..e69de29" in plain
    assert plain.splitlines()[-1] == "index 0000000..e69de29"
    colors = {item.get("color") for item in segments}
    assert "#22c55e" in colors
    assert "#ef4444" in colors


def test_syntax_diff_keeps_index_on_one_line() -> None:
    from robber.ansi import strip_ansi

    code = "diff --git a/x b/x\nindex 0000000..e69de29 100644\n--- a/x\n+++ b/x\n+hello\n"
    plain = strip_ansi(render_syntax_ansi(code, "diff", width=40))
    index_lines = [line for line in plain.splitlines() if "index 0000000" in line]
    assert index_lines
    assert all("e69de29" in line for line in index_lines)


def test_wrap_width_uses_safe_margins() -> None:
    from robber.markdown_ansi import wrap_width

    assert wrap_width(80) == 76
    assert wrap_width(40) == 60
    ansi = render_markdown_ansi("### Title\n\nbody text\n", width=80)
    assert "Title" in ansi
    assert "body text" in ansi


def test_colorize_preserves_source() -> None:
    from robber.ansi import strip_ansi

    src = "### Head\n+added\n"
    assert strip_ansi(colorize_for_emit(src)) == src
