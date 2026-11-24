"""Bounded public-HTTP fetch. Size-capped, no auth bypass, no crawl."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx

from robber.tools.workspace import clip

FETCH_TIMEOUT = 8.0
MAX_BYTES = 512_000
USER_AGENT = "AgentRobber/1.0 (+local)"
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = " ".join(data.split())
        if text:
            self._chunks.append(text)

    def text(self) -> str:
        return "\n".join(self._chunks)


def url_from(query: str) -> str | None:
    match = URL_RE.search(query or "")
    if match is None:
        return None
    return match.group(0).rstrip(").,]")


def fetch_page(query: str) -> str:
    url = url_from(query)
    if not url:
        return "No http(s) URL found"
    scheme = url.split(":", 1)[0].lower()
    if scheme not in {"http", "https"}:
        return "Only http(s) URLs are allowed"
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(url)
            response.raise_for_status()
            payload = response.content[:MAX_BYTES]
            content_type = (response.headers.get("content-type") or "").lower()
    except Exception as exc:
        return f"scrape failed: {type(exc).__name__}"
    text = payload.decode(response.encoding or "utf-8", errors="replace")
    if "html" in content_type or "<html" in text[:400].lower():
        parser = _TextExtractor()
        try:
            parser.feed(text)
            body = parser.text()
        except Exception:
            body = TAG_RE.sub(" ", text)
    else:
        body = text
    return clip(f"scrape {url}\n{body.strip()}", 4_000)
