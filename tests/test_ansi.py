from robber.ansi import parse_ansi, strip_ansi


def test_parse_ansi_colors_and_strip() -> None:
    raw = "\x1b[32mhello\x1b[0m \x1b[31merr\x1b[0m"
    plain, segments = parse_ansi(raw)
    assert plain == "hello err"
    assert strip_ansi(raw) == "hello err"
    colors = [item.get("color") for item in segments if item["text"].strip()]
    assert "#22c55e" in colors
    assert "#ef4444" in colors


def test_plain_text_is_passthrough() -> None:
    plain, segments = parse_ansi("no codes")
    assert plain == "no codes"
    assert segments == [{"text": "no codes", "color": None}]
