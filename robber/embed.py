"""Tiny local embeddings — no model download, CPU-only hashing."""

from __future__ import annotations

import hashlib
import math
import re

DIM = 96
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")


def embed(text: str) -> list[float]:
    vec = [0.0] * DIM
    tokens = TOKEN.findall(text.lower())[:500]
    for index, token in enumerate(tokens):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        slot = int.from_bytes(digest[:2], "little") % DIM
        vec[slot] += 1.0 + (index % 5) * 0.05
    for i in range(0, min(len(text) - 2, 800)):
        gram = text[i : i + 3].lower().encode("utf-8")
        digest = hashlib.blake2b(gram, digest_size=4).digest()
        slot = int.from_bytes(digest[:2], "little") % DIM
        vec[slot] += 0.15
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))
