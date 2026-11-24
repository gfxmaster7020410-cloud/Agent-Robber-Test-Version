"""File/media attach + one-shot dictation helpers for the TUI prompt bar."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from robber.jail import workspace_root
from robber.paste_buffer import is_huge_paste, paste_dir, sanitize_paste, save_paste_buffer

_TEXT_EXT = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".json",
    ".md",
    ".txt",
    ".css",
    ".html",
    ".htm",
    ".toml",
    ".yml",
    ".yaml",
    ".log",
    ".csv",
    ".xml",
    ".rs",
    ".go",
    ".sql",
    ".sh",
    ".bat",
    ".ps1",
    ".ini",
    ".cfg",
    ".env",
    ".svg",
}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif"}
_VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}
_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}
_DOC_EXT = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".rtf", ".odt"}
_MEDIA_EXT = _IMAGE_EXT | _VIDEO_EXT | _AUDIO_EXT | _DOC_EXT
_FENCE = {
    ".py": "python",
    ".md": "markdown",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
}
_MEDIA_LIMIT = 80_000_000
_TEXT_LIMIT = 8_000_000


def pick_files() -> list[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        picked = filedialog.askopenfilenames(
            title="Attach file / media",
            filetypes=[
                ("All files", "*.*"),
                ("Images", "*.png *.jpg *.jpeg *.gif *.webp *.bmp *.heic"),
                ("Video", "*.mp4 *.webm *.mov *.mkv *.avi"),
                ("Audio", "*.mp3 *.wav *.m4a *.ogg *.flac"),
                ("Documents", "*.pdf *.doc *.docx *.txt *.md *.json *.csv"),
            ],
        )
        root.destroy()
        return [str(item) for item in picked if item]
    except Exception:
        return []


def _kind_for(suffix: str) -> str:
    if suffix in _IMAGE_EXT:
        return "image"
    if suffix in _VIDEO_EXT:
        return "video"
    if suffix in _AUDIO_EXT:
        return "audio"
    if suffix in _DOC_EXT:
        return "document"
    return "file"


def _unique_dest(name: str) -> Path:
    folder = paste_dir() / "attachments"
    folder.mkdir(parents=True, exist_ok=True)
    stem = Path(name).stem or "attach"
    suffix = Path(name).suffix
    candidate = folder / f"{stem}{suffix}"
    n = 1
    while candidate.exists():
        candidate = folder / f"{stem}-{n}{suffix}"
        n += 1
    return candidate


def stash_media(path: str | os.PathLike[str]) -> str:
    target = Path(path)
    dest = _unique_dest(target.name)
    shutil.copy2(target, dest)
    try:
        rel = dest.resolve().relative_to(workspace_root().resolve()).as_posix()
    except ValueError:
        rel = str(dest)
    kind = _kind_for(target.suffix.lower())
    size = dest.stat().st_size
    return (
        f"[Attached {kind}: {target.name}]\n"
        f"Path: {rel}\n"
        f"Size: {size} bytes\n"
        "Read this attached file in the workspace. For images, inspect the visual contents."
    )


def snippet_for_path(path: str | os.PathLike[str]) -> str:
    target = Path(path)
    name = target.name
    suffix = target.suffix.lower()
    try:
        size = target.stat().st_size
    except OSError as exc:
        return f"[Attached file missing: {name} ({exc})]"
    if suffix in _MEDIA_EXT:
        if size > _MEDIA_LIMIT:
            return f"[Attached file too large: {name} ({size} bytes)]"
        try:
            return stash_media(target)
        except OSError as exc:
            return f"[Attached file unreadable: {name} ({exc})]"
    if size > _TEXT_LIMIT:
        return f"[Attached file too large: {name} ({size} bytes)]"
    if suffix not in _TEXT_EXT:
        try:
            return stash_media(target)
        except OSError as exc:
            return f"[Attached file: {name} ({size} bytes)]"
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"[Attached file unreadable: {name} ({exc})]"
    if "\x00" in raw:
        try:
            return stash_media(target)
        except OSError:
            return f"[Attached binary: {name} ({size} bytes)]"
    chunk = sanitize_paste(raw)
    if is_huge_paste(chunk):
        stats = save_paste_buffer(chunk)
        return f"### Attached {name}\n{stats.marker}"
    fence = _FENCE.get(suffix, suffix.lstrip(".")[:8])
    return f"### Attached {name}\n```{fence}\n{chunk}\n```"


def transcribe_utterance(timeout: int = 25) -> str:
    if sys.platform != "win32":
        return ""
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine; "
        "$engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar)); "
        "$engine.SetInputToDefaultAudioDevice(); "
        "$engine.InitialSilenceTimeout = [TimeSpan]::FromSeconds(6); "
        "$result = $engine.Recognize(); "
        "if ($result) { [Console]::Out.Write($result.Text) }"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout or "").strip()
