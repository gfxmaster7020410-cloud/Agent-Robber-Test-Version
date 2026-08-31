"""Single Source of Truth — local vector index of the jailed workspace."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from robber.chunker import chunk_file
from robber.embed import cosine, embed
from robber.jail import is_skipped_dir, workspace_root, dir_is_locked

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
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
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
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def reindex(self) -> dict[str, object]:
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
            if size > MAX_BYTES:
                skipped["size"] = skipped.get("size", 0) + 1
                if len(samples) < SKIP_SAMPLE_LIMIT:
                    rel = str(path.relative_to(self.root)).replace("\\", "/")
                    samples.append(f"{rel} (size)")
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                skipped["stat"] = skipped.get("stat", 0) + 1
                continue
            rel = str(path.relative_to(self.root)).replace("\\", "/")
            files += 1
            for piece in chunk_file(rel, source):
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
