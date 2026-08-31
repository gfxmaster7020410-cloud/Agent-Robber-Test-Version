"""Groq chat ping. Prints status and preview length only."""

from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        print("groq_missing")
        return
    response = httpx.get(
        "https://api.groq.com/openai/v1/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=12.0,
    )
    print("groq_models", response.status_code)
    if response.status_code != 200:
        return
    response = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "Reply with exactly one word: pong"}],
            "max_tokens": 16,
        },
        timeout=20.0,
    )
    preview = ""
    if response.status_code == 200:
        try:
            preview = (
                response.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
        except Exception:
            preview = ""
    print("groq_http", response.status_code, "chars", len(preview or ""), "preview", (preview or "")[:40].replace("\n", " "))


if __name__ == "__main__":
    main()
