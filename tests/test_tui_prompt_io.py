from pathlib import Path

from robber.tui.prompt_io import snippet_for_path


def test_snippet_for_text_file(tmp_path: Path) -> None:
    target = tmp_path / "notes.py"
    target.write_text("print('hi')\n", encoding="utf-8")
    body = snippet_for_path(target)
    assert "### Attached notes.py" in body
    assert "print('hi')" in body
    assert "```python" in body


def test_snippet_for_missing_file(tmp_path: Path) -> None:
    body = snippet_for_path(tmp_path / "gone.txt")
    assert "missing" in body.lower()


def test_snippet_for_image_copies_into_workspace(tmp_path: Path, monkeypatch) -> None:
    from robber.tui import prompt_io

    monkeypatch.setattr(prompt_io, "paste_dir", lambda: tmp_path / "ctx")
    monkeypatch.setattr(prompt_io, "workspace_root", lambda: tmp_path)
    photo = tmp_path / "shot.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    body = snippet_for_path(photo)
    assert "[Attached image: shot.png]" in body
    assert "shot.png" in body
    copied = list((tmp_path / "ctx" / "attachments").glob("shot*.png"))
    assert copied
