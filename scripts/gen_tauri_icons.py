"""Write minimal PNG/ICO assets for the Tauri bundle (stdlib only)."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "gui" / "src-tauri" / "icons"
ACCENT = (0, 224, 255, 255)
BG = (10, 16, 28, 255)


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def write_png(path: Path, size: int) -> bytes:
    rows: list[bytes] = []
    cx = cy = size / 2
    radius = min(size, size) * 0.34
    inner = radius * 0.42
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            dx = x + 0.5 - cx
            dy = y + 0.5 - cy
            dist = (dx * dx + dy * dy) ** 0.5
            if inner <= dist <= radius:
                row.extend(ACCENT)
            else:
                row.extend(BG)
        rows.append(bytes(row))
    raw = b"".join(rows)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    body = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(body)
    return body


def write_ico(path: Path, png_bytes: bytes, size: int) -> None:
    count = 1
    header = struct.pack("<HHH", 0, 1, count)
    entry = struct.pack(
        "<BBBBHHII",
        size if size < 256 else 0,
        size if size < 256 else 0,
        0,
        0,
        1,
        32,
        len(png_bytes),
        6 + 16,
    )
    path.write_bytes(header + entry + png_bytes)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    png32 = write_png(OUT / "32x32.png", 32)
    write_png(OUT / "128x128.png", 128)
    write_png(OUT / "128x128@2x.png", 256)
    write_png(OUT / "icon.png", 512)
    write_ico(OUT / "icon.ico", png32, 32)
    print(f"wrote icons in {OUT}")


if __name__ == "__main__":
    main()
