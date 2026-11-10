"""Give the bot one small web search tool so it can look up what it cannot know.

The framework ships a web search toolset, but every one of them costs one to three
thousand tokens of tool schema before a single character of the reply is written,
and their results come back as a long JSON blob. On a group chat where most turns
never need the outside world, that is a bad trade. This plugin registers one tool
with a two-line description and returns three trimmed results, so the schema sits
around two hundred tokens and an unused tool costs almost nothing.
"""

import html
import json
import re
import urllib.parse
from pathlib import Path

import aiohttp

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

# Anonymous AnySearch works without a key and answers in about a second. It is the
# primary source because the snippets are already clean prose, no HTML to strip.
ANYSEARCH = "https://api.anysearch.com/v1/search"
BING = "https://www.bing.com/search"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
# Load tunables from config.json next to this file; fall back to built-in defaults.
_CFG_PATH = Path(__file__).parent / "config.json"
try:
    _CFG = json.loads(_CFG_PATH.read_text(encoding="utf-8-sig"))
except (OSError, ValueError):
    _CFG = {}
if not isinstance(_CFG, dict):
    _CFG = {}

# Three results is enough for the model to answer a group-chat question, and the
# cap is what keeps a search from swallowing the reply's whole token budget.
MAX_RESULTS = _CFG.get("max_results", 3)
SNIPPET_CHARS = _CFG.get("snippet_chars", 110)
# Hard ceiling on what one search may hand back, in characters.
MAX_PAYLOAD = _CFG.get("max_payload", 700)


def _tidy(text: str) -> str:
    """Collapse whitespace and strip tags so a snippet fits on one line.

    Args:
        text: Raw snippet text from a search engine.

    Returns:
        Single-line text with no HTML tags, clipped to SNIPPET_CHARS.
    """
    text = re.sub(r"<[^>]+>", "", text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SNIPPET_CHARS]


async def _anysearch(session: aiohttp.ClientSession, query: str) -> list[dict]:
    """Search through the anonymous AnySearch endpoint.

    Args:
        session: Shared HTTP session.
        query: Search terms.

    Returns:
        Results as dictionaries with title, url and snippet.

    Raises:
        Exception: If the service answers with a business error or no results.
    """
    async with session.post(
        ANYSEARCH,
        json={"query": query, "max_results": MAX_RESULTS, "zone": "cn"},
        headers={"Content-Type": "application/json"},
        timeout=aiohttp.ClientTimeout(total=20),
    ) as response:
        data = await response.json(content_type=None)
    if data.get("code") not in (None, 0):
        raise RuntimeError(f"anysearch: {data.get('message') or data.get('code')}")
    body = data.get("data") or data
    found = []
    for item in (body.get("results") or [])[:MAX_RESULTS]:
        if not item.get("url"):
            continue
        found.append(
            {
                "title": _tidy(item.get("title", "")),
                "url": item["url"],
                "snippet": _tidy(item.get("snippet") or item.get("content") or ""),
            }
        )
    if not found:
        raise RuntimeError("anysearch: no results")
    return found


async def _bing(session: aiohttp.ClientSession, query: str) -> list[dict]:
    """Scrape Bing as the fallback when AnySearch is rate limited.

    Args:
        session: Shared HTTP session.
        query: Search terms.

    Returns:
        Results as dictionaries with title, url and snippet.

    Raises:
        Exception: If Bing returns no parsable results.
    """
    url = f"{BING}?q={urllib.parse.quote(query)}&setlang=zh-CN"
    async with session.get(
        url,
        headers={"User-Agent": UA},
        timeout=aiohttp.ClientTimeout(total=20),
    ) as response:
        html = await response.text(errors="ignore")
    found = []
    for block in re.findall(r'<li class="b_algo".*?</li>', html, re.S):
        head = re.search(r'<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not head:
            continue
        snippet = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        found.append(
            {
                "title": _tidy(head.group(2)),
                "url": head.group(1),
                "snippet": _tidy(snippet.group(1) if snippet else ""),
            }
        )
        if len(found) >= MAX_RESULTS:
            break
    if not found:
        raise RuntimeError("bing: no results")
    return found


class WebSearch(Star):
    """One compact search tool for the model."""

    def __init__(self, context: Context):
        super().__init__(context)

    @filter.llm_tool(name="search_web")
    async def search_web(self, event: AstrMessageEvent, query: str) -> str:
        """上网查你不知道的事：最近发生了什么、新消息、具体事实。

        Args:
            query(string): 搜索词，写具体，别写一整句话
        """
        query = (query or "").strip()
        if not query:
            return "搜索词是空的。"
        async with aiohttp.ClientSession(trust_env=True) as session:
            try:
                results = await _anysearch(session, query)
            except Exception as first:
                logger.info("[websearch] anysearch failed (%s), trying bing", first)
                try:
                    results = await _bing(session, query)
                except Exception as second:
                    logger.warning("[websearch] bing failed too: %s", second)
                    return "没搜到，网络那边不通。"
        lines = []
        for item in results:
            host = urllib.parse.urlparse(item["url"]).netloc
            piece = f"{item['title']}（{host}）{item['snippet']}"
            lines.append(piece)
            if sum(len(line) for line in lines) > MAX_PAYLOAD:
                break
        payload = "\n".join(lines)[:MAX_PAYLOAD]
        logger.debug("[websearch] %s -> %d results", query, len(lines))
        return payload
