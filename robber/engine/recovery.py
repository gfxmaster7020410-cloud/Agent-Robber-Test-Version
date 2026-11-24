"""Long-horizon SSOT + source snapshots for crash recovery on 8GB boxes."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from robber.config import (
    SNAPSHOT_DIR_NAME,
    SNAPSHOT_EXPORT_DIR,
    SNAPSHOT_MAX_BYTES,
    SNAPSHOT_MAX_FILE_BYTES,
    SNAPSHOT_MAX_FILES,
)
from robber.jail import workspace_root
from robber.patch import BLOCKED_NAMES
from robber.ssot_db import default_ssot_path, get_ssot_db, reset_ssot_cache
from robber.state_manager import StateManager

logger = logging.getLogger(__name__)

SKIP_DIR_NAMES = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist"}
CLEAN_SHUTDOWN_KEY = "clean_shutdown"
DIRTY_PHASES = frozenset({"running", "planning"})
DIRTY_LOOPS = frozenset({"run", "dispatch", "resume", "running", "seed"})


class SnapshotRecoveryEngine:
    """Atomic point-in-time copies of ssot.db and selected workspace files."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        manager: StateManager | None = None,
        hub: Any = None,
    ) -> None:
        self.root = Path(root).resolve() if root else workspace_root()
        self.manager = manager
        self.hub = hub
        self.snap_root = self.root / ".robber" / SNAPSHOT_DIR_NAME
        self.export_root = self.root / SNAPSHOT_EXPORT_DIR
        self.snap_root.mkdir(parents=True, exist_ok=True)

    def create_snapshot(self, reason: str = "checkpoint", files: list[str] | None = None) -> str:
        return str(self.snapshot(reason=reason, files=files).get("id") or "")

    def restore_snapshot(self, snapshot_id: str) -> bool:
        try:
            report = self.restore(snapshot_id)
        except FileNotFoundError:
            return False
        return bool(report.get("ssot"))

    def snapshot(
        self,
        *,
        label: str = "",
        files: list[str] | None = None,
        reason: str = "checkpoint",
    ) -> dict[str, Any]:
        started = time.perf_counter()
        snap_id = uuid.uuid4().hex[:16]
        dest = self.snap_root / snap_id
        dest.mkdir(parents=True, exist_ok=True)
        db_copy = dest / "ssot.snapshot.db"
        db = self.manager.db if self.manager is not None else get_ssot_db(self.root)
        self.mark_dirty(db)
        targets = list(files or [])[:SNAPSHOT_MAX_FILES]
        copied: list[dict[str, str]] = []
        used = 0
        for rel in targets:
            if used >= SNAPSHOT_MAX_BYTES:
                break
            info = self._copy_file(rel, dest / "files")
            if info is None:
                continue
            used += int(info.get("bytes") or 0)
            copied.append(info)
        try:
            db.wal_checkpoint("TRUNCATE")
        except sqlite3.OperationalError:
            try:
                db.wal_checkpoint("PASSIVE")
            except sqlite3.OperationalError:
                logger.debug("wal checkpoint skipped")
        db.backup_to(db_copy)
        export_name = _export_name(snap_id)
        export_path = self.export_root / export_name
        try:
            self.export_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(db_copy, export_path)
        except OSError:
            export_path = db_copy
        bytes_total = _dir_bytes(dest)
        if export_path.is_file() and export_path.resolve() != db_copy.resolve():
            bytes_total += int(export_path.stat().st_size)
        stamp = time.time()
        manifest = {
            "id": snap_id,
            "label": (label or reason)[:80],
            "reason": reason,
            "created_at": stamp,
            "ssot": "ssot.snapshot.db",
            "export": str(export_path.relative_to(self.root)).replace("\\", "/")
            if _is_relative(export_path, self.root)
            else str(export_path),
            "files": copied,
            "file_count": len(copied),
            "bytes": bytes_total,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
        }
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        try:
            db.put_system_snapshot(
                snap_id,
                reason=reason,
                path=str(export_path),
                bytes_total=bytes_total,
                file_count=len(copied),
                label=str(manifest["label"]),
                created_at=stamp,
            )
        except Exception:
            logger.debug("system_snapshots insert skipped")
        return manifest

    def list_snapshots(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        if self.snap_root.is_dir():
            for folder in self.snap_root.iterdir():
                manifest = folder / "manifest.json"
                if not manifest.is_file():
                    continue
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    data.setdefault("id", folder.name)
                    sid = str(data.get("id") or folder.name)
                    seen.add(sid)
                    rows.append(data)
        try:
            db = self.manager.db if self.manager is not None else get_ssot_db(self.root)
            for item in db.list_system_snapshots(limit=64):
                sid = str(item.get("id") or "")
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                rows.append(
                    {
                        "id": sid,
                        "label": item.get("label") or item.get("reason") or "",
                        "reason": item.get("reason") or "",
                        "created_at": item.get("created_at") or 0,
                        "file_count": item.get("file_count") or 0,
                        "bytes": item.get("bytes") or 0,
                    }
                )
        except Exception:
            logger.debug("system_snapshots list skipped")
        rows.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        return rows

    def restore(self, snapshot_id: str) -> dict[str, Any]:
        started = time.perf_counter()
        dest = self._find(snapshot_id)
        if dest is None:
            raise FileNotFoundError(f"snapshot '{snapshot_id}' not found")
        manifest_path = dest / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        db_copy = dest / str(manifest.get("ssot") or "ssot.snapshot.db")
        if not db_copy.is_file():
            alt = self.root / str(manifest.get("export") or "")
            if alt.is_file():
                db_copy = alt
        if not db_copy.is_file():
            raise FileNotFoundError("snapshot is missing ssot.snapshot.db")
        restored = self._restore_files(dest, manifest)
        self._restore_ssot(db_copy)
        if self.manager is not None:
            self.manager.db = get_ssot_db(self.root)
            self.manager.project = self.manager.ensure_project()
        self.mark_clean(self.manager.db if self.manager is not None else get_ssot_db(self.root))
        report = {
            "id": str(manifest.get("id") or dest.name),
            "label": manifest.get("label") or "",
            "files_restored": restored,
            "ssot": True,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
        }
        self._notify(
            f"[RESTORE] snapshot {report['id'][:12]} "
            f"files={restored} ssot=ok {report['elapsed_ms']:.1f}ms"
        )
        return report

    def crash_report(self) -> dict[str, Any] | None:
        db = self.manager.db if self.manager is not None else get_ssot_db(self.root)
        flag = ""
        try:
            flag = str(db.get_meta(CLEAN_SHUTDOWN_KEY) or "")
        except Exception:
            flag = ""
        dirty_agents = _dirty_agents(db)
        if flag in {"1", "prompted"}:
            return None
        crashed = flag == "0" or bool(dirty_agents)
        if not crashed:
            return None
        latest = (self.list_snapshots() or [None])[0]
        return {
            "crashed": True,
            "clean_shutdown": flag,
            "dirty_agents": dirty_agents,
            "snapshot": latest,
        }

    def crash_prompt(self) -> str:
        info = self.crash_report()
        if info is None:
            return ""
        latest = info.get("snapshot") if isinstance(info.get("snapshot"), dict) else None
        if latest:
            sid = str(latest.get("id") or "")
            reason = str(latest.get("reason") or latest.get("label") or "checkpoint")
            when = _fmt_ts(latest.get("created_at"))
            return (
                f"[CRASH-RECOVERY] Ungraceful exit detected. Last stable snapshot "
                f"{sid} ({reason} @ {when}). Type /restore {sid[:8]} to roll back, "
                f"or /snapshots to list checkpoints."
            )
        return (
            "[CRASH-RECOVERY] Ungraceful exit detected. No snapshots yet. "
            "Type /snapshots after the next high-risk checkpoint."
        )

    def mark_dirty(self, db: Any | None = None) -> None:
        store = db or (self.manager.db if self.manager is not None else get_ssot_db(self.root))
        try:
            store.put_meta(CLEAN_SHUTDOWN_KEY, "0")
        except Exception:
            logger.debug("dirty flag skipped")

    def mark_clean(self, db: Any | None = None) -> None:
        store = db or (self.manager.db if self.manager is not None else get_ssot_db(self.root))
        try:
            store.put_meta(CLEAN_SHUTDOWN_KEY, "1")
        except Exception:
            logger.debug("clean flag skipped")

    def emit_crash_prompt(self) -> str:
        text = self.crash_prompt()
        if text:
            self._notify(text)
            try:
                db = self.manager.db if self.manager is not None else get_ssot_db(self.root)
                if str(db.get_meta(CLEAN_SHUTDOWN_KEY) or "") == "0":
                    db.put_meta(CLEAN_SHUTDOWN_KEY, "prompted")
            except Exception:
                pass
        return text

    def _find(self, snapshot_id: str) -> Path | None:
        key = (snapshot_id or "").strip()
        if not key:
            return None
        exact = self.snap_root / key
        if (exact / "manifest.json").is_file():
            return exact
        if self.snap_root.is_dir():
            for folder in self.snap_root.iterdir():
                if folder.name.startswith(key) and (folder / "manifest.json").is_file():
                    return folder
        export = self._find_export(key)
        if export is not None:
            return export
        return None

    def _find_export(self, key: str) -> Path | None:
        if not self.export_root.is_dir():
            return None
        for path in self.export_root.glob("snap_*.db"):
            if key in path.stem:
                folder = self.snap_root / key
                if (folder / "manifest.json").is_file():
                    return folder
        return None

    def _copy_file(self, rel: str, dest_files: Path) -> dict[str, str] | None:
        src = self._safe_join(rel)
        if not src.is_file():
            return None
        if src.name in BLOCKED_NAMES or src.name.lower().startswith(".env"):
            return None
        size = src.stat().st_size
        if size > SNAPSHOT_MAX_FILE_BYTES:
            return None
        posix = str(src.relative_to(self.root)).replace("\\", "/")
        out = dest_files / posix
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        return {"path": posix, "sha256": digest, "bytes": str(size)}

    def _safe_join(self, rel: str) -> Path:
        text = (rel or "").replace("\\", "/").lstrip("/")
        if not text or ".." in text.split("/"):
            raise ValueError("snapshot path escapes workspace")
        target = (self.root / text).resolve()
        target.relative_to(self.root)
        for part in target.parts:
            if part in SKIP_DIR_NAMES:
                raise ValueError("snapshot path is in a skipped directory")
        return target

    def _restore_files(self, dest: Path, manifest: dict[str, Any]) -> int:
        rows = [item for item in (manifest.get("files") or []) if isinstance(item, dict)]
        diff = _bundle_restore_diff(self.root, dest / "files", rows)
        if diff:
            if self._apply_rust(diff):
                return len(rows)
        restored = 0
        for row in rows:
            rel = str(row.get("path") or "")
            src = dest / "files" / rel.replace("\\", "/")
            if not src.is_file():
                continue
            target = self._safe_join(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            restored += 1
        return restored

    def _apply_rust(self, diff: str) -> bool:
        try:
            from robber.indexer_bridge import apply_diff_atomic, load_indexer

            if load_indexer() is None:
                return False
            report = apply_diff_atomic(self.root, diff, ssot_db=None)
            return bool(isinstance(report, dict) and report.get("ok"))
        except FileNotFoundError:
            return False
        except Exception:
            logger.debug("atomic restore patch skipped")
            return False

    def _restore_ssot(self, snapshot_db: Path) -> None:
        live = default_ssot_path(self.root)
        reset_ssot_cache()
        live.parent.mkdir(parents=True, exist_ok=True)
        tmp = live.with_name(live.name + ".restore")
        shutil.copy2(snapshot_db, tmp)
        for extra in (live, Path(str(live) + "-wal"), Path(str(live) + "-shm")):
            if extra.is_file():
                extra.unlink()
        tmp.replace(live)
        get_ssot_db(self.root)

    def _notify(self, text: str) -> None:
        logger.info(text)
        hub = self.hub
        broadcast = getattr(hub, "broadcast", None) if hub is not None else None
        payload = {
            "type": "message",
            "event": "STATE_ACK",
            "role": "system",
            "content": text,
            "text": text,
            "stream_kind": "ack",
            "append": False,
        }
        if broadcast is not None:
            try:
                result = broadcast(payload)
                if inspect.iscoroutine(result):
                    result.close()
            except TypeError:
                try:
                    result = broadcast()
                    if inspect.iscoroutine(result):
                        result.close()
                except Exception:
                    pass
            except Exception:
                logger.debug("restore hub emit skipped")
        try:
            from robber.gateway_ipc import GatewayClient

            client = GatewayClient(timeout=0.4)
            client.connect()
            try:
                client.notify_ssot("task_state")
                client.send(
                    "STATE_ACK",
                    {"content": text, "event": "restore"},
                )
            finally:
                client.close()
        except Exception:
            logger.debug("restore ipc emit skipped")


def format_snapshot_list(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No snapshots yet. High-risk mutations write .robber/snapshots automatically."
    lines = ["snapshots:"]
    for row in rows[:20]:
        sid = str(row.get("id") or "")
        reason = str(row.get("reason") or row.get("label") or "")
        count = int(row.get("file_count") or 0)
        when = _fmt_ts(row.get("created_at"))
        lines.append(f"  {sid}  {when}  {reason}  files={count}")
    return "\n".join(lines)


def _dirty_agents(db: Any) -> list[str]:
    out: list[str] = []
    try:
        rows = db.list_agents(limit=32)
    except Exception:
        return out
    for row in rows:
        phase = str(row.get("phase") or "").strip().lower()
        loop = str(row.get("loop_status") or "").strip().lower()
        if phase in DIRTY_PHASES or loop in DIRTY_LOOPS:
            out.append(str(row.get("worker_id") or row.get("id") or ""))
    return out


def _export_name(snap_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"snap_{stamp}_{snap_id[:8]}.db"


def _fmt_ts(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return "-"


def _dir_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    if path.is_file():
        return int(path.stat().st_size)
    for item in path.rglob("*"):
        if item.is_file():
            total += int(item.stat().st_size)
    return total


def _is_relative(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _bundle_restore_diff(root: Path, files_root: Path, rows: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in rows:
        rel = str(row.get("path") or "").replace("\\", "/")
        if not rel:
            continue
        src = files_root / rel
        if not src.is_file():
            continue
        try:
            after = src.read_text(encoding="utf-8")
        except OSError:
            continue
        target = root / rel
        before = ""
        if target.is_file():
            try:
                before = target.read_text(encoding="utf-8")
            except OSError:
                before = ""
        if before == after:
            continue
        chunk = _unified_replace(rel, before, after)
        if chunk:
            parts.append(chunk)
    return "\n".join(parts).strip()


def _unified_replace(rel: str, before: str, after: str) -> str:
    posix = rel.replace("\\", "/")
    old_lines = before.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    new_lines = after.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if old_lines == [""]:
        old_lines = []
    if new_lines == [""]:
        new_lines = []
    if before.endswith("\n") and old_lines and old_lines[-1] == "":
        old_lines.pop()
    if after.endswith("\n") and new_lines and new_lines[-1] == "":
        new_lines.pop()
    if not old_lines and new_lines:
        body = "\n".join(f"+{line}" for line in new_lines)
        return (
            f"--- /dev/null\n+++ b/{posix}\n@@ -0,0 +1,{len(new_lines)} @@\n{body}\n"
        )
    if old_lines and not new_lines:
        body = "\n".join(f"-{line}" for line in old_lines)
        return (
            f"--- a/{posix}\n+++ /dev/null\n@@ -1,{len(old_lines)} +0,0 @@\n{body}\n"
        )
    old_n = max(1, len(old_lines))
    new_n = max(1, len(new_lines))
    minus = "\n".join(f"-{line}" for line in old_lines)
    plus = "\n".join(f"+{line}" for line in new_lines)
    return (
        f"--- a/{posix}\n+++ b/{posix}\n@@ -1,{old_n} +1,{new_n} @@\n{minus}\n{plus}\n"
    )
