from robber.thoughts import ThoughtGate, looks_like_thought_leak, strip_thoughts


def test_strip_thought_tags() -> None:
    raw = "<think>plan NodeA.ts</think>\nYes, I can write files."
    assert strip_thoughts(raw) == "Yes, I can write files."
    assert "NodeA" not in strip_thoughts(raw)


def test_strip_thinking_fence() -> None:
    raw = "```thinking\nsecret plan\n```\n\nHello"
    assert strip_thoughts(raw) == "Hello"


def test_thought_gate_holds_until_close() -> None:
    gate = ThoughtGate()
    assert gate.push("<thi") == ""
    assert gate.push("nk>hidden</th") == ""
    assert gate.push("ink>Visible") == "Visible"
    assert "hidden" not in gate.text


def test_looks_like_thought_leak() -> None:
    assert looks_like_thought_leak("<thinking>abc</thinking>")
    assert not looks_like_thought_leak("goal    do the thing")
