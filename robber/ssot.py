"""Single Source of Truth — local vector index of the jailed workspace."""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from robber.chunker import Chunk, chunk_file
from robber.config import LOCK_STALE_SECONDS, MMAP_MAX_BYTES, MMAP_WINDOW_BYTES
from robber.embed import cosine, embed
from robber.jail import is_skipped_dir, workspace_root, dir_is_locked

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

INDEX_EXTS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".md",
    ".json",
    ".txt",
    ".css",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".html",
}
INDEX_BARE_NAMES = {
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    ".prettierignore",
    ".env.example",
    "Dockerfile",
    "Makefile",
    "LICENSE",
    "LICENSE.txt",
}
SECRET_NAMES = {".env", ".env.local", "keys.vault", "credentials.json"}
LOCK_NAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "composer.lock",
}
BINARY_EXTS = {
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".mp4",
    ".zip",
    ".gz",
    ".pyc",
    ".exe",
    ".dll",
    ".bin",
}
DUMP_PREFIXES = ("session-ses_",)
MAX_FILES = 160
MAX_BYTES = 80_000
TOP_K = 4
SKIP_SAMPLE_LIMIT = 8

# Kept so older imports (`from robber.ssot import SKIP_FILE_NAMES`) still work.
SKIP_FILE_NAMES = LOCK_NAMES | SECRET_NAMES


class QuantumSafeChecksum:
    """SHA-3 (sha3_256) integrity digest for ledger and index mutations."""

    @staticmethod
    def digest(data: bytes | str) -> str:
        blob = data.encode("utf-8") if isinstance(data, str) else data
        return hashlib.sha3_256(blob or b"").hexdigest()

    @staticmethod
    def verify(data: bytes | str, expected: str) -> bool:
        got = QuantumSafeChecksum.digest(data)
        want = (expected or "").strip().lower()
        return bool(want) and got == want


class FileMicroLock:
    """Non-blocking exclusive lease. fcntl on POSIX, msvcrt on Windows."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: object | None = None

    def acquire(self, *, blocking: bool = False) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        handle = os.open(str(self.path), flags)
        try:
            os.lseek(handle, 0, os.SEEK_SET)
            if os.fstat(handle).st_size <= 0:
                os.write(handle, b"\0")
                os.lseek(handle, 0, os.SEEK_SET)
            if fcntl is not None:
                op = fcntl.LOCK_EX
                if not blocking:
                    op |= fcntl.LOCK_NB
                try:
                    fcntl.flock(handle, op)
                except OSError:
                    os.close(handle)
                    return False
            elif msvcrt is not None:
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                try:
                    msvcrt.locking(handle, mode, 1)
                except OSError:
                    os.close(handle)
                    return False
            else:
                os.close(handle)
                return False
        except Exception:
            try:
                os.close(handle)
            except OSError:
                pass
            return False
        lease = json.dumps({"pid": os.getpid(), "ts": time.time()})
        try:
            os.lseek(handle, 0, os.SEEK_SET)
            payload = lease.encode("utf-8")
            os.write(handle, payload)
        except OSError:
            pass
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)
            elif msvcrt is not None:
                try:
                    os.lseek(handle, 0, os.SEEK_SET)
                    msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            try:
                os.close(handle)
            except OSError:
                pass

    def stale(self, *, now: float | None = None, timeout: float = LOCK_STALE_SECONDS) -> bool:
        if not self.path.is_file():
            return False
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace").strip()
            stamp = float(json.loads(raw).get("ts") or 0.0) if raw.startswith("{") else self.path.stat().st_mtime
        except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError, UnicodeError):
            try:
                stamp = self.path.stat().st_mtime
            except OSError:
                return False
        clock = time.time() if now is None else now
        return (clock - stamp) >= timeout


class DeadlockRecoveryWatchdog:
    """Sweep abandoned micro-locks after a 30s timeout. Optional background loop."""

    def __init__(self, lock_dir: Path, *, stale_after: float = LOCK_STALE_SECONDS) -> None:
        self.lock_dir = Path(lock_dir)
        self.stale_after = stale_after
        self.sweeps = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, *, interval: float = 5.0) -> None:
        self.stop()
        self._stop = threading.Event()
        wait = max(0.05, float(interval))

        def loop() -> None:
            while not self._stop.wait(wait):
                self.sweep()

        self._thread = threading.Thread(target=loop, name="ssot-lock-watchdog", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.05, float(timeout)))

    def sweep(self, *, now: float | None = None) -> int:
        if not self.lock_dir.is_dir():
            return 0
        recovered = 0
        clock = time.time() if now is None else now
        for path in self.lock_dir.glob("*.lock"):
            lock = FileMicroLock(path)
            if not lock.stale(now=clock, timeout=self.stale_after):
                continue
            try:
                path.unlink()
                recovered += 1
            except OSError:
                continue
        self.sweeps += 1
        return recovered

    def recover(self) -> int:
        return self.sweep()

    def count_stale(self, *, now: float | None = None) -> int:
        if not self.lock_dir.is_dir():
            return 0
        clock = time.time() if now is None else now
        total = 0
        for path in self.lock_dir.glob("*.lock"):
            if FileMicroLock(path).stale(now=clock, timeout=self.stale_after):
                total += 1
        return total


@contextmanager
def atomic_section_lock(name: str, root: Path | None = None, *, timeout: float = LOCK_STALE_SECONDS):
    """Exclusive critical section for SSOT mutation (reindex/save)."""
    base = (root or workspace_root()).resolve() / ".robber" / "locks"
    watchdog = DeadlockRecoveryWatchdog(base, stale_after=LOCK_STALE_SECONDS)
    watchdog.sweep()
    lock = FileMicroLock(base / f"{name}.lock")
    deadline = time.monotonic() + max(0.05, float(timeout))
    held = False
    while time.monotonic() <= deadline:
        if lock.acquire(blocking=False):
            held = True
            break
        watchdog.sweep()
        time.sleep(0.02)
    if not held:
        raise TimeoutError(f"atomic lock '{name}' timed out")
    try:
        yield lock
    finally:
        lock.release()


class MemoryMappedChunkStreamer:
    """Zero-copy mmap windows over large documents (200+ page text)."""

    def __init__(self, path: Path, *, window: int = MMAP_WINDOW_BYTES) -> None:
        self.path = Path(path)
        self.window = max(4096, int(window))

    def iter_windows(self) -> Iterator[bytes]:
        if not self.path.is_file():
            return
        size = self.path.stat().st_size
        if size <= 0:
            return
        with self.path.open("rb") as handle:
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                length = mapped.size()
                for start in range(0, length, self.window):
                    yield bytes(mapped[start : start + self.window])

    def iter_lines(self) -> Iterator[tuple[int, str]]:
        buf = ""
        line_no = 0
        overflow = max(self.window * 4, 256_000)
        for window in self.iter_windows():
            buf += window.decode("utf-8", errors="replace")
            while True:
                newline = buf.find("\n")
                if newline < 0:
                    if len(buf) > overflow:
                        line_no += 1
                        yield line_no, buf[:overflow]
                        buf = buf[overflow:]
                    break
                line_no += 1
                yield line_no, buf[:newline]
                buf = buf[newline + 1 :]
        if buf:
            line_no += 1
            yield line_no, buf.rstrip("\n")

    def extract_lines(self, start: int, end: int) -> str:
        lo = max(1, int(start))
        hi = max(lo, int(end))
        out: list[str] = []
        for line_no, line in self.iter_lines():
            if line_no < lo:
                continue
            if line_no > hi:
                break
            out.append(line)
        return "\n".join(out)

    def iter_line_chunks(self, rel: str, *, lines_per_chunk: int = 50, max_chunks: int = 400) -> Iterator[Chunk]:
        batch: list[str] = []
        start_line = 1
        emitted = 0
        last = 0
        for line_no, line in self.iter_lines():
            if not batch:
                start_line = line_no
            batch.append(line)
            last = line_no
            if len(batch) >= lines_per_chunk:
                body = "\n".join(batch)
                if body.strip():
                    yield Chunk(rel, "mmap", start_line, last, body)
                    emitted += 1
                    if emitted >= max_chunks:
                        return
                batch = []
        if batch and emitted < max_chunks:
            body = "\n".join(batch)
            if body.strip():
                yield Chunk(rel, "mmap", start_line, last, body)


def skip_reason(path: Path) -> str | None:
    """Return why a file is not indexed, or None if it should be."""
    name = path.name
    if name in SECRET_NAMES:
        return "secret"
    if name in LOCK_NAMES:
        return "lock"
    if name.startswith(DUMP_PREFIXES):
        return "dump"
    suffix = path.suffix.lower()
    if suffix in BINARY_EXTS:
        return "binary"
    if name in INDEX_BARE_NAMES or suffix in INDEX_EXTS:
        return None
    return "ext"


@dataclass
class Hit:
    path: str
    kind: str
    start: int
    end: int
    score: float
    preview: str


@dataclass
class StoredChunk:
    path: str
    kind: str
    start: int
    end: int
    text: str
    vector: list[float]


class SsotIndex:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or workspace_root()).resolve()
        self.dir = self.root / ".robber"
        self.path = self.dir / "ssot.json"
        self.chunks: list[StoredChunk] = []
        self.files = 0
        self.skipped: dict[str, int] = {}
        self.skipped_samples: list[str] = []
        self._lock = threading.Lock()

    @property
    def stats(self) -> dict[str, object]:
        return {
            "files": self.files,
            "chunks": len(self.chunks),
            "backend": "hash-json",
            "skipped": sum(self.skipped.values()),
            "skipped_by": dict(self.skipped),
        }

    def fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        """Indexable files plus mtime/size — skips .robber, secrets, and other jail filters."""
        rows: list[tuple[str, int, int]] = []
        for path, reason in self._iter_candidates():
            if reason is not None:
                continue
            try:
                stat = path.stat()
                rel = path.relative_to(self.root).as_posix()
            except (OSError, ValueError):
                continue
            rows.append((rel, int(stat.st_mtime_ns), int(stat.st_size)))
        rows.sort()
        return tuple(rows)

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, UnicodeError):
            return
        with self._lock:
            self.files = int(raw.get("files", 0))
            self.chunks = [StoredChunk(**row) for row in raw.get("chunks", [])]
            self.skipped = {
                str(key): int(value) for key, value in raw.get("skipped_by", {}).items()
            }
            self.skipped_samples = [str(item) for item in raw.get("skipped_samples", [])]

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "files": self.files,
            "chunks": [asdict(chunk) for chunk in self.chunks],
            "skipped_by": self.skipped,
            "skipped_samples": self.skipped_samples,
            "checksum": QuantumSafeChecksum.digest(
                json.dumps({"files": self.files, "chunks": len(self.chunks)})
            ),
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def reindex(self) -> dict[str, object]:
        with atomic_section_lock("ssot-reindex", self.root):
            return self._reindex_unlocked()

    def _reindex_unlocked(self) -> dict[str, object]:
        chunks: list[StoredChunk] = []
        files = 0
        skipped: dict[str, int] = {}
        samples: list[str] = []
        pruned_dirs = 0
        for path, reason in self._iter_candidates():
            if reason == "dir":
                pruned_dirs += 1
                continue
            if reason is not None:
                skipped[reason] = skipped.get(reason, 0) + 1
                if reason != "dir" and len(samples) < SKIP_SAMPLE_LIMIT:
                    rel = str(path.relative_to(self.root)).replace("\\", "/")
                    samples.append(f"{rel} ({reason})")
                continue
            try:
                size = path.stat().st_size
            except OSError:
                skipped["stat"] = skipped.get("stat", 0) + 1
                continue
            if size > MMAP_MAX_BYTES:
                skipped["size"] = skipped.get("size", 0) + 1
                if len(samples) < SKIP_SAMPLE_LIMIT:
                    rel = str(path.relative_to(self.root)).replace("\\", "/")
                    samples.append(f"{rel} (size)")
                continue
            rel = str(path.relative_to(self.root)).replace("\\", "/")
            try:
                if size > MAX_BYTES:
                    pieces = list(MemoryMappedChunkStreamer(path).iter_line_chunks(rel))
                else:
                    source = path.read_text(encoding="utf-8", errors="replace")
                    pieces = chunk_file(rel, source)
            except OSError:
                skipped["stat"] = skipped.get("stat", 0) + 1
                continue
            files += 1
            for piece in pieces:
                chunks.append(
                    StoredChunk(
                        path=piece.path,
                        kind=piece.kind,
                        start=piece.start,
                        end=piece.end,
                        text=piece.text,
                        vector=embed(f"{piece.path}\n{piece.text}"),
                    )
                )
            if files >= MAX_FILES:
                break
        if pruned_dirs:
            skipped["dir"] = pruned_dirs
        with self._lock:
            self.chunks = chunks
            self.files = files
            self.skipped = skipped
            self.skipped_samples = samples
            self.save()
            return self.stats

    def rank_chunks(self, query: str, k: int | None = None) -> list[tuple[float, StoredChunk]]:
        with self._lock:
            chunks = list(self.chunks)
        if not chunks:
            return []
        qvec = embed(query or "workspace")
        ranked = [(float(cosine(qvec, chunk.vector)), chunk) for chunk in chunks]
        ranked.sort(key=lambda item: item[0], reverse=True)
        if k is None:
            return ranked
        return ranked[: max(0, int(k))]

    def search(self, query: str, k: int = TOP_K) -> list[Hit]:
        hits: list[Hit] = []
        for score, chunk in self.rank_chunks(query, k=k):
            preview = chunk.text.strip().splitlines()[0][:80] if chunk.text.strip() else ""
            hits.append(
                Hit(
                    path=chunk.path,
                    kind=chunk.kind,
                    start=chunk.start,
                    end=chunk.end,
                    score=round(float(score), 3),
                    preview=preview,
                )
            )
        return hits

    def format_hits(self, hits: list[Hit]) -> str:
        if not hits:
            return (
                f"SSOT empty ({self.stats['files']} files indexed). "
                "SSOT empty. The file watcher refreshes the index; /memory reindex still works."
            )
        lines = [
            f"SSOT {self.stats['files']} files · {self.stats['chunks']} chunks · {self.stats['backend']}"
        ]
        notable = {key: count for key, count in self.skipped.items() if key != "dir" and count}
        if notable:
            detail = " · ".join(f"{count} {key}" for key, count in sorted(notable.items()))
            lines.append(f"skipped {detail}")
        for sample in self.skipped_samples[:SKIP_SAMPLE_LIMIT]:
            lines.append(f"  skipped {sample}")
        for hit in hits:
            lines.append(
                f"• {hit.path}:{hit.start}-{hit.end} [{hit.kind} · {hit.score}] {hit.preview}"
            )
        return "\n".join(lines)

    def _iter_candidates(self):
        """Yield (path, skip_reason). skip_reason is None when the file should be indexed."""
        seen = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            hidden = [name for name in dirnames if is_skipped_dir(name)]
            locked = [
                name
                for name in dirnames
                if dir_is_locked(Path(dirpath) / name)
            ]
            dirnames[:] = [
                name
                for name in dirnames
                if not is_skipped_dir(name) and not dir_is_locked(Path(dirpath) / name)
            ]
            for name in hidden:
                yield Path(dirpath) / name, "dir"
            for name in locked:
                yield Path(dirpath) / name, "locked"
            for name in filenames:
                path = Path(dirpath) / name
                reason = skip_reason(path)
                yield path, reason
                if reason is None:
                    seen += 1
                    if seen >= MAX_FILES:
                        return

    def _iter_files(self):
        for path, reason in self._iter_candidates():
            if reason is None:
                yield path


_INDEX: SsotIndex | None = None


def get_index() -> SsotIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = SsotIndex()
        _INDEX.load()
    return _INDEX
