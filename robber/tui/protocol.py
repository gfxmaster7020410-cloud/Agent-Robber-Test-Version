"""Pure helpers for the native workbench TUI — no Textual import."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any

WRITE_RE = re.compile(
    r"\b(write_file|edit_file|str_replace|create_file|Wrote to|Edit~to|wrote)\b",
    re.I,
)
SHELL_RE = re.compile(r"\b(execute_bash|run_command|npm |node |python |pytest|shell)\b", re.I)

KIND_LABEL = {
    "initiating": "Initiating",
    "ack": "Routing",
    "terminal": "Terminal",
    "error": "return error!",
    "writing": "Writing",
    "reply": "Reply",
}


def format_elapsed(ms: int | float) -> str:
    n = max(0, int(ms))
    minutes = n // 60_000
    rest = n % 60_000
    return f"{minutes}m {rest}ms" if minutes > 0 else f"{rest}ms"


def session_id() -> str:
    return "random" + "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(6))


def pretty_mode(mode: str) -> str:
    raw = (mode or "plan").strip() or "plan"
    return raw[:1].upper() + raw[1:]


def classify_stream(
    *,
    role: str,
    content: str,
    stream_kind: str | None = None,
) -> tuple[str, str]:
    kind = (stream_kind or "").strip().lower()
    if kind in {"ack", "state_ack", "routing"}:
        return "ack", KIND_LABEL["ack"]
    if kind in KIND_LABEL:
        return kind, KIND_LABEL[kind]
    lowered = (role or "system").strip().lower()
    if lowered == "error":
        return "error", KIND_LABEL["error"]
    if lowered in {"assistant", "success"}:
        if WRITE_RE.search(content or "") or SHELL_RE.search(content or ""):
            if SHELL_RE.search(content or "") and not WRITE_RE.search(content or ""):
                return "terminal", KIND_LABEL["terminal"]
            return "writing", KIND_LABEL["writing"]
        return "reply", KIND_LABEL["reply"]
    if lowered == "file" or WRITE_RE.search(content or ""):
        if SHELL_RE.search(content or ""):
            return "terminal", KIND_LABEL["terminal"]
        return "writing", KIND_LABEL["writing"]
    if lowered == "tool" or SHELL_RE.search(content or ""):
        return "terminal", KIND_LABEL["terminal"]
    return "initiating", KIND_LABEL["initiating"]


def mutation_rows(value: Any) -> list[dict[str, str]]:
    row = value if isinstance(value, dict) else {}
    raw = value if isinstance(value, list) else row.get("files")
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        marker = str(item.get("marker") or "~")
        if marker not in {"+", "-", "~"}:
            marker = "~"
        detail = str(item.get("detail") or "")
        line = str(item.get("line") or f"[{marker}] {path} {detail}").strip()
        out.append({"path": path, "marker": marker, "detail": detail, "line": line})
    return out


@dataclass
class WorkbenchState:
    status: str = "connecting"
    mode: str = "act"
    model: str = "Hybrid LLM"
    control: str = "autonomous"
    hybrid: bool = False
    busy: bool = False
    busy_label: str = ""
    workspace: str = "workspace"
    ssot_files: int = 0
    ssot_chunks: int = 0
    ssot_backend: str = "hash-json"
    context_packed: int = 0
    context_window: int = 0
    mcp_online: int = 0
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    tps: float = 0.0
    ttft_ms: int = 0
    elapsed_ms: int = 0
    mutations: list[dict[str, str]] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)
    loop_phase: str = "idle"
    turn_tokens: int = 0
    max_tokens: int = 0
    stale_locks: int = 0
    graph_nodes: int = 0
    graph_head: str = ""
    router_engine: str = ""
    horizon_status: str = ""
    horizon_cursor: int = 0
    horizon_total: int = 0

    def ingest_state(self, frame: dict[str, Any]) -> None:
        self.mode = str(frame.get("mode") or self.mode)
        self.model = str(frame.get("model") or self.model)
        self.control = str(frame.get("control_mode") or self.control)
        self.hybrid = frame.get("hybrid") is True
        self.busy = frame.get("busy") is True
        self.workspace = str(frame.get("workspace") or self.workspace)
        ssot = frame.get("ssot") if isinstance(frame.get("ssot"), dict) else {}
        self.ssot_files = int(ssot.get("files") or 0)
        self.ssot_chunks = int(ssot.get("chunks") or 0)
        self.ssot_backend = str(ssot.get("backend") or self.ssot_backend)
        self.context_packed = int(frame.get("context_packed") or ssot.get("packed") or 0)
        self.context_window = int(frame.get("context_window") or ssot.get("window") or 0)
        mcp = frame.get("mcp") if isinstance(frame.get("mcp"), dict) else {}
        servers = mcp.get("servers") if isinstance(mcp.get("servers"), list) else []
        self.mcp_servers = [row for row in servers if isinstance(row, dict)]
        self.mcp_online = int(mcp.get("enabled") or sum(1 for row in self.mcp_servers if row.get("enabled")))
        try:
            self.tps = float(frame.get("tps") or 0)
        except (TypeError, ValueError):
            self.tps = 0.0
        self.ttft_ms = int(frame.get("ttft_ms") or 0)
        self.elapsed_ms = int(frame.get("elapsed_ms") or 0)
        if not self.busy:
            self.busy_label = ""
        mut = frame.get("mutations")
        if mut is not None:
            self.mutations = mutation_rows(mut)
        loop = frame.get("loop") if isinstance(frame.get("loop"), dict) else {}
        self.ingest_loop(loop)

    def ingest_loop(self, tree: dict[str, Any]) -> None:
        if not isinstance(tree, dict) or not tree:
            return
        self.loop_phase = str(tree.get("phase") or self.loop_phase)
        try:
            self.turn_tokens = int(tree.get("turn_tokens") or self.turn_tokens)
        except (TypeError, ValueError):
            pass
        try:
            self.max_tokens = int(tree.get("max_tokens_per_turn") or self.max_tokens)
        except (TypeError, ValueError):
            pass
        session = tree.get("session") if isinstance(tree.get("session"), dict) else {}
        self.router_engine = str(session.get("engine") or self.router_engine)
        try:
            self.stale_locks = int(tree.get("stale_locks") or self.stale_locks)
        except (TypeError, ValueError):
            pass
        meta = tree.get("graph") if isinstance(tree.get("graph"), dict) else {}
        if meta:
            try:
                self.graph_nodes = int(meta.get("nodes") or self.graph_nodes)
            except (TypeError, ValueError):
                pass
            head = meta.get("head")
            if isinstance(head, dict):
                self.graph_head = str(head.get("version") or "")
            elif head:
                self.graph_head = str(head)
        horizon = tree.get("horizon") if isinstance(tree.get("horizon"), dict) else {}
        if horizon:
            self.horizon_status = str(horizon.get("status") or self.horizon_status)
            try:
                self.horizon_cursor = int(horizon.get("cursor") or self.horizon_cursor)
            except (TypeError, ValueError):
                pass
            try:
                self.horizon_total = int(horizon.get("total") or self.horizon_total)
            except (TypeError, ValueError):
                pass

    def remember_prompt(self, text: str) -> None:
        line = (text or "").strip()
        if line and (not self.history or self.history[-1] != line):
            self.history.append(line)
            self.history = self.history[-80:]
