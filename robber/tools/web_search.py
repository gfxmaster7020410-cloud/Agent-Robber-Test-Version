"""Lightweight web lookup (no Google scrape, no API keys required)."""

from __future__ import annotations

import re
from urllib.parse import quote_plus

import httpx

SEARCH_TIMEOUT = 8.0
USER_AGENT = "AgentRobber/1.0 (+local)"


def search_query_from(prompt: str) -> str:
    text = (prompt or "").strip()
    text = re.sub(
        r"^\s*(please\s+)?(search( the web)?|google|look up( online)?|web search)\s*(for\s+)?",
        "",
        text,
        flags=re.I,
    )
    return text.strip(" ?") or prompt.strip()


def search_web(prompt: str) -> str:
    query = search_query_from(prompt)
    if not query:
        return "empty search query"
    lines = [f"web search: {query}"]
    wiki = _wikipedia(query)
    if wiki:
        lines.append(wiki)
    ddg = _duckduckgo(query)
    if ddg:
        lines.append(ddg)
    if len(lines) == 1:
        lines.append("no instant answers (network empty or blocked)")
    from robber.tools.workspace import clip

    return clip("\n".join(lines), 3_200)


def _wikipedia(query: str) -> str:
    url = (
        "https://en.wikipedia.org/w/api.php?action=opensearch&limit=3&namespace=0"
        f"&format=json&search={quote_plus(query[:180])}"
    )
    try:
        with httpx.Client(timeout=SEARCH_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return f"wikipedia: {type(exc).__name__}"
    if not isinstance(data, list) or len(data) < 4:
        return ""
    titles = data[1] if isinstance(data[1], list) else []
    descs = data[2] if isinstance(data[2], list) else []
    links = data[3] if isinstance(data[3], list) else []
    rows = []
    for title, desc, link in zip(titles, descs, links, strict=False):
        if title:
            rows.append(f"- {title}: {desc or link}")
    return "wikipedia:\n" + "\n".join(rows) if rows else ""


def _duckduckgo(query: str) -> str:
    url = "https://api.duckduckgo.com/"
    try:
        with httpx.Client(timeout=SEARCH_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(
                url,
                params={"q": query[:180], "format": "json", "no_html": "1", "skip_disambig": "1"},
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return f"duckduckgo: {type(exc).__name__}"
    if not isinstance(data, dict):
        return ""
    abstract = str(data.get("AbstractText") or "").strip()
    heading = str(data.get("Heading") or "").strip()
    related = data.get("RelatedTopics") if isinstance(data.get("RelatedTopics"), list) else []
    bits = []
    if heading and abstract:
        bits.append(f"{heading}: {abstract[:600]}")
    elif abstract:
        bits.append(abstract[:600])
    for item in related[:4]:
        if isinstance(item, dict) and item.get("Text"):
            bits.append(f"- {item['Text'][:220]}")
    return "duckduckgo:\n" + "\n".join(bits) if bits else ""
