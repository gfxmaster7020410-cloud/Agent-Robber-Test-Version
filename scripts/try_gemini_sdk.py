"""One-shot SDK ping. Prints only ok/length, never secrets."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from robber.engine.gemini_stream import GeminiStreamEngine  # noqa: E402


async def main() -> None:
    engine = GeminiStreamEngine()
    chunks: list[str] = []
    async for piece in engine.stream_completion("Reply with exactly one word: pong"):
        chunks.append(piece)
    text = "".join(chunks).strip()
    print("gemini_sdk_ok", bool(text), "chars", len(text), "preview", text[:40].replace("\n", " "))


if __name__ == "__main__":
    asyncio.run(main())
