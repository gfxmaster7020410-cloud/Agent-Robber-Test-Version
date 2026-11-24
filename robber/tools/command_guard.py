"""Parse and block high-risk shell before it reaches the PTY session."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from robber.jail import workspace_root
from robber.security.perimeter import is_protected_path

_FENCE = re.compile(r"```(?:bash|sh|zsh|shell|cmd|powershell|ps1)?\s*\n(.*?)```", re.I | re.S)
_DOLLAR = re.compile(r"^\$\s+(.+)$", re.M)
_WRITE_OP = re.compile(
    r"(>>|>\s*|tee\s+-a|tee\s+|out-file|set-content|add-content|new-item|\bni\b|"
    r"\bset-content\b|\badd-content\b|\bmove-item\b|\bcopy-item\b|\bredirect\b)",
    re.I,
)
_DELETE_OP = re.compile(
    r"(?:\brm\b|\bdel\b|\berase\b|\bremove-item\b|\brmdir\b|\brd\b|\bunlink\b)",
    re.I,
)
_PATH_TOKEN = re.compile(
    r"""(?P<q>["'])(?P<quoted>.*?)(?P=q)|(?P<bare>[A-Za-z]:[\\/][^\s|&><;]+|(?:[\w./\\-]+(?:\.\w+)+))"""
)
_CD = re.compile(r"(?:^|[;&|\n]|\s&&\s)\s*cd(?:\s+/d|\s+-Path)?\s+(\"([^\"]+)\"|'([^']+)'|([^\s;&|]+))", re.I)
_SHELLISH = re.compile(
    r"^(cd|pwd|ls|dir|echo|export|set|conda|source|pushd|popd|\./)",
    re.I,
)

_BLOCK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\s+-[rf]{1,2}\s+(--no-preserve-root\s+)?[/\\](\s|$)", re.I), "refusing recursive delete of filesystem root"),
    (re.compile(r"\brm\s+-[rf]{1,2}\s+[/\\]\*", re.I), "refusing recursive delete of filesystem root"),
    (re.compile(r"\bformat\s+[a-z]:", re.I), "refusing disk format"),
    (re.compile(r"\bformat-volume\b", re.I), "refusing disk format"),
    (re.compile(r"\bmkfs(\.\w+)?\b", re.I), "refusing mkfs"),
    (re.compile(r"\bdd\s+.*\bof\s*=\s*[/\\]dev[/\\]", re.I), "refusing raw disk write"),
    (re.compile(r"\bdiskpart\b", re.I), "refusing diskpart"),
    (re.compile(r"\bdel\s+/[fsq]+\s+[c-z]:\\?\s*$", re.I), "refusing wipe of a drive root"),
    (re.compile(r"\bshutdown\b|\breboot\b|\binit\s+0\b|\bpoweroff\b", re.I), "refusing shutdown/reboot"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;"), "refusing fork bomb"),
    (re.compile(r"\bwhile\s+(true|:|1)\b", re.I), "refusing unbounded loop"),
    (re.compile(r"\bfor\s*\(\s*;\s*;\s*\)", re.I), "refusing unbounded loop"),
)

_SYSTEM_PREFIXES = (
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "/bin",
    "/sbin",
    "/usr",
    "/etc",
    "/dev",
    "/proc",
    "/sys",
    "/boot",
)


@dataclass(frozen=True)
class CommandSafetyResult:
    ok: bool
    command: str
    reason: str = ""
    blocked: bool = False

    @property
    def allowed(self) -> bool:
        return self.ok and not self.blocked


class CommandBlockedError(ValueError):
    """Raised when sanitize_command rejects a payload."""


def looks_like_shell_command(query: str) -> bool:
    text = (query or "").strip()
    if not text:
        return False
    if text.startswith("$") or text.startswith("!"):
        return True
    if _FENCE.search(text):
        return True
    if " && " in text or text.endswith(" &"):
        return True
    if re.search(r"\bpython\s+-i\b", text, re.I):
        return True
    return bool(_SHELLISH.match(text))


def extract_shell_command(query: str) -> str | None:
    text = (query or "").strip()
    if not text:
        return None
    fence = _FENCE.search(text)
    if fence:
        body = fence.group(1).strip()
        return body or None
    dollar = _DOLLAR.search(text)
    if dollar:
        return dollar.group(1).strip()
    if text.startswith("!") and len(text) > 1:
        return text[1:].strip()
    if looks_like_shell_command(text) and not re.search(r"\b(please|could you|can you)\b", text, re.I):
        return text
    return None


def sanitize_command(command: str, *, workspace: Path | None = None) -> CommandSafetyResult:
    raw = (command or "").strip()
    if not raw:
        return CommandSafetyResult(False, "", "empty command", True)
    if len(raw) > 16_000:
        return CommandSafetyResult(False, raw, "command too large", True)
    compact = " ".join(raw.splitlines())
    for pattern, reason in _BLOCK_PATTERNS:
        if pattern.search(raw) or pattern.search(compact):
            return CommandSafetyResult(False, raw, reason, True)
    root = (workspace or workspace_root()).resolve()
    cd_reason = _blocked_cd(raw, root)
    if cd_reason:
        return CommandSafetyResult(False, raw, cd_reason, True)
    write_reason = _blocked_mutation(raw, root)
    if write_reason:
        return CommandSafetyResult(False, raw, write_reason, True)
    return CommandSafetyResult(True, raw, "", False)


def sanitize_argv(argv: list[str], *, workspace: Path | None = None) -> CommandSafetyResult:
    if not argv:
        return CommandSafetyResult(False, "", "empty command", True)
    joined = subprocess_join(argv)
    return sanitize_command(joined, workspace=workspace)


def subprocess_join(argv: list[str]) -> str:
    parts: list[str] = []
    for item in argv:
        text = str(item)
        if re.search(r"[\s\"']", text):
            parts.append('"' + text.replace('"', '\\"') + '"')
        else:
            parts.append(text)
    return " ".join(parts)


def _blocked_cd(command: str, workspace: Path) -> str:
    for match in _CD.finditer(command):
        token = match.group(2) or match.group(3) or match.group(4) or ""
        token = token.strip().strip('"').strip("'")
        if not token or token in {".", "./"}:
            continue
        if token.startswith("..") or not _looks_absolute(token):
            continue
        try:
            target = Path(token).expanduser().resolve()
        except OSError:
            continue
        if _is_system_path(target):
            return f"refusing cd into system path: {token}"
        if not _inside(target, workspace) and not _inside(workspace, target):
            # Absolute jump that is neither workspace nor a parent of it.
            if _is_system_path(target) or target.anchor and target == target.anchor:
                return f"refusing cd outside workspace: {token}"
            drive_root = Path(target.anchor) if target.anchor else None
            if drive_root is not None and target == drive_root:
                return f"refusing cd to drive root: {token}"
    return ""


def _blocked_mutation(command: str, workspace: Path) -> str:
    writes = bool(_WRITE_OP.search(command))
    deletes = bool(_DELETE_OP.search(command))
    if not writes and not deletes:
        return ""
    for token in _path_tokens(command):
        try:
            candidate = Path(token)
            resolved = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
        except OSError:
            continue
        if is_protected_path(resolved):
            return f"refusing to mutate protected engine path: {token}"
        if writes and _is_system_path(resolved):
            return f"refusing write to system path: {token}"
        if writes and not _inside(resolved, workspace) and resolved.is_absolute():
            if _is_system_path(resolved.parent if resolved.suffix else resolved):
                return f"refusing write outside workspace: {token}"
    return ""


def _path_tokens(command: str) -> list[str]:
    out: list[str] = []
    for match in _PATH_TOKEN.finditer(command):
        token = match.group("quoted") or match.group("bare") or ""
        if token:
            out.append(token)
    return out


def _looks_absolute(token: str) -> bool:
    text = token.strip()
    return bool(re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("/") or text.startswith("\\\\"))


def _inside(child: Path, parent: Path) -> bool:
    left = os.path.normcase(str(child))
    right = os.path.normcase(str(parent))
    if left == right:
        return True
    return left.startswith(right.rstrip("\\/") + os.sep)


def _is_system_path(path: Path) -> bool:
    text = os.path.normcase(str(path))
    if text in {"/", "\\"} or re.fullmatch(r"[a-z]:\\?", text):
        return True
    return any(text == prefix or text.startswith(prefix + os.sep) for prefix in _SYSTEM_PREFIXES)
