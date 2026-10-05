"""Tier 1 — RSS / Atom feed ingestion via feedparser.

Yahoo Finance removed public topic RSS years ago, so this tier is
essentially a no-op for `stock-market-news` — but the module is wired in
so the moment a feed URL becomes available, the pipeline picks it up
automatically.

Discovery: we try a small set of candidate feed URLs for the topic
page. If any returns a parseable feed, we extract article URLs and
feed-level metadata only — content still comes from Tier 2-7 per article.
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from app.scraper.article_data import ArticleData
from app.scraper.extractors.base import BaseExtractor

CANDIDATE_FEEDS = (
    "/topic/stock-market-news.rss",
    "/rss/topstories",
    "/rss/finance",
    "/rss/stock-market-news",
)


class RssExtractor(BaseExtractor):
    name = "rss"

    async def extract(self, url: str, payload: Any) -> ArticleData | None:
        # We don't have an HTTP client here (extractors are pure functions
        # of their payload). The pipeline's `rss_discovery` step is what
        # actually fetches feeds and passes parsed entries in as `payload`.
        if not isinstance(payload, dict) or payload.get("kind") != "rss_entry":
            return None
        try:
            import feedparser  # type: ignore
        except ImportError:
            logger.warning("feedparser not installed; RSS extractor disabled")
            return None

        entry = payload.get("entry", {})
        article = self._empty_article(url)
        article.title = self._safe_text(getattr(entry, "title", None))
        if hasattr(entry, "summary"):
            article.summary = self._safe_text(entry.summary)
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            import datetime as _dt

            pp = entry.published_parsed
            dt = _dt.datetime(pp[0], pp[1], pp[2], pp[3], pp[4], pp[5], tzinfo=_dt.UTC)
            article.published_at = dt.isoformat()
        if hasattr(entry, "author"):
            article.authors = [{"name": entry.author}]
        if hasattr(entry, "tags"):
            article.tags = [t.term for t in entry.tags if getattr(t, "term", None)]
        if hasattr(entry, "links") and entry.links:
            for link in entry.links:
                if link.get("rel") == "alternate" and link.get("href"):
                    article.source_url = link["href"]
                    break
        return article

    async def discover_feed_url(self, base_url: str) -> str | None:
        """Try candidate feed URLs; return the first one that parses."""
        try:
            import feedparser  # type: ignore
        except ImportError:
            return None
        from urllib.parse import urlparse

        import httpx

        origin = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
        for path in CANDIDATE_FEEDS:
            url = origin + path
            try:
                async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                    r = await client.get(url)
                if r.status_code == 200 and r.text.strip().startswith(("<?xml", "<rss", "<feed")):
                    parsed = feedparser.parse(r.text)
                    if parsed.entries:
                        logger.info("RSS feed discovered: {u}", u=url)
                        return url
            except Exception:
                continue
        return None
