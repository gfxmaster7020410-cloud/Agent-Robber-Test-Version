from pathlib import Path

from robber.paste_buffer import (
    PasteStats,
    context_marker,
    ingest_notice,
    is_huge_paste,
    prepare_prompt,
    sanitize_paste,
    word_count,
)


def test_sanitize_keeps_newlines() -> None:
    text = sanitize_paste("one\r\ntwo\rthree\x00")
    assert text == "one\ntwo\nthree"
    assert not is_huge_paste("short line\nsecond line")


def test_prepare_prompt_offloads_huge_paste(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ROBBER_WORKSPACE", str(tmp_path))
    huge = ("word " * 10_200) + ("\nparagraph line of context\n" * 50)
    assert word_count(huge) >= 10_000
    prompt, notice = prepare_prompt(huge)
    assert "[Context File Attached: paste_buffer.txt" in prompt
    assert notice and notice.startswith("✦ Initiating: Ingested large paste payload")
    saved = tmp_path / ".agent_robber" / "tmp_context" / "paste_buffer.txt"
    assert saved.is_file()
    assert huge.strip()[:40] in saved.read_text(encoding="utf-8")
    again, notice2 = prepare_prompt(prompt)
    assert notice2 is None
    assert again == prompt


def test_marker_and_notice_shape() -> None:
    mark = context_marker(120)
    assert mark == "[Context File Attached: paste_buffer.txt (120 pages)]"
    stats = PasteStats(
        words=100_000,
        bytes=400_000,
        pages=120,
        tokens=120_000,
        path=".agent_robber/tmp_context/paste_buffer.txt",
        marker=mark,
        offloaded=True,
    )
    line = ingest_notice(stats)
    assert "120+ pages" in line
    assert "120,000 tokens" in line
