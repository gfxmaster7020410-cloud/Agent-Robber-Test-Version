"""Persisted operator prefs — theme is shared by the TUI and the GUI."""

from __future__ import annotations

import json
from pathlib import Path

from robber.jail import workspace_root

PREFS_NAME = "prefs.json"
THEMES = ("dark", "light")
DEFAULT_THEME = "dark"
BATCHES = ("eco", "normal", "heavy")
DEFAULT_BATCH = "normal"
CONTROL_MODES = ("autonomous", "manual")
DEFAULT_CONTROL_MODE = "autonomous"


def prefs_path() -> Path:
    return workspace_root() / ".robber" / PREFS_NAME


def load_theme() -> str:
    theme = str(_read_prefs().get("theme", DEFAULT_THEME)).strip().lower()
    return theme if theme in THEMES else DEFAULT_THEME


def save_theme(theme: str) -> str:
    value = theme.strip().lower()
    if value not in THEMES:
        raise ValueError("theme must be dark or light")
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _read_prefs()
    payload["theme"] = value
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return value


def _read_prefs() -> dict[str, object]:
    path = prefs_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_batch() -> str:
    batch = str(_read_prefs().get("batch", DEFAULT_BATCH)).strip().lower()
    return batch if batch in BATCHES else DEFAULT_BATCH


def save_batch(batch: str) -> str:
    from robber.engine.presets import normalize_tier, tier_to_batch

    raw = batch.strip()
    try:
        value = tier_to_batch(normalize_tier(raw))
    except ValueError:
        value = raw.strip().lower()
        if value not in BATCHES:
            raise ValueError("batch must be eco, normal, or heavy (Eco / Balanced / Best)") from None
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _read_prefs()
    payload["batch"] = value
    payload["route_mode"] = {
        "eco": "ECO",
        "normal": "BALANCED",
        "heavy": "BEST",
    }.get(value, "BALANCED")
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return value


def load_sound() -> bool:
    return bool(_read_prefs().get("sound", True))


def save_sound(enabled: bool) -> bool:
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _read_prefs()
    payload["sound"] = bool(enabled)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return bool(enabled)


def load_control_mode() -> str:
    mode = str(_read_prefs().get("control_mode", DEFAULT_CONTROL_MODE)).strip().lower()
    return mode if mode in CONTROL_MODES else DEFAULT_CONTROL_MODE


def save_control_mode(mode: str) -> str:
    value = mode.strip().lower()
    if value not in CONTROL_MODES:
        raise ValueError("control mode must be autonomous or manual")
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _read_prefs()
    payload["control_mode"] = value
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return value


def load_context_window() -> int:
    from robber.ssot_context import DEFAULT_CONTEXT_WINDOW, parse_window

    raw = _read_prefs().get("context_window", DEFAULT_CONTEXT_WINDOW)
    try:
        return parse_window(raw)
    except ValueError:
        return DEFAULT_CONTEXT_WINDOW


def save_context_window(window: int | str) -> int:
    from robber.ssot_context import parse_window

    value = parse_window(window)
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _read_prefs()
    payload["context_window"] = value
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return value
