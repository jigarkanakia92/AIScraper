"""Stage A — listing crawl.

Fetches the topic page with infinite-scroll handling, extracts article
URLs, dedupes against the database, and returns a queue of URLs to
process in Stage B.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Article
from app.scraper.fetcher import Fetcher, FetchResult


@dataclass
class ListingItem:
    url: str
    preview_title: str | None = None
    preview_thumbnail_url: str | None = None
    preview_timestamp: str | None = None
    source_publisher: str | None = None


class ListingCrawler:
    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher
        self._settings = get_settings()
        self._url_re = re.compile(self._settings.article_url_pattern)

    async def crawl(self) -> list[ListingItem]:
        target = self._settings.target_url
        logger.info("Stage A: crawling {u}", u=target)
        result = await self._fetcher.fetch(
            target,
            scroll=True,
            wait_for="css:li.js-stream-content",
            max_scrolls=self._settings.crawl4ai_max_scroll_count,
        )
        items = await self._parse_listing(result)
        logger.info("Stage A: extracted {n} raw cards", n=len(items))
        deduped = self._dedupe(items)
        logger.info("Stage A: {n} cards after dedupe", n=len(deduped))
        return deduped

    async def _parse_listing(self, result: FetchResult) -> list[ListingItem]:
        items: list[ListingItem] = []
        from app.scraper.extractors.crawl4ai_schema_extractor import (
            Crawl4AISchemaExtractor,
        )

        t3 = Crawl4AISchemaExtractor()
        data = await t3.extract_listing(result)
        if not data:
            logger.warning("Stage A: Tier 3 returned empty; using CSS fallback")
            data = await self._css_listing_fallback_async(result)
        for row in data:
            url = row.get("article_url") or row.get("url")
            if not url:
                continue
            if not self._url_re.match(url):
                continue
            items.append(ListingItem(
                url=url,
                preview_title=row.get("preview_title") or row.get("title"),
                preview_thumbnail_url=row.get("preview_thumbnail_url") or row.get("top_image_url"),
                preview_timestamp=row.get("preview_timestamp") or row.get("published_at"),
                source_publisher=row.get("source_publisher"),
            ))
        return items

    async def _css_listing_fallback_async(self, result: FetchResult) -> list[dict]:
        """Tier-7 listing-page scan using selectors.yaml — runs sync I/O in a thread."""
        import asyncio as _aio
        return await _aio.to_thread(self._css_listing_fallback, result)

    def _css_listing_fallback(self, result: FetchResult) -> list[dict]:
        """Tier-7 listing-page scan using selectors.yaml."""
        try:
            from bs4 import BeautifulSoup  # type: ignore
        except ImportError:
            return []
        try:
            import yaml
        except ImportError:
            return []
        from pathlib import Path
        settings = get_settings()
        with (Path(settings.extraction_config_dir) / settings.selectors_file).open() as f:
            cfg = yaml.safe_load(f) or {}
        listing_cfg = cfg.get("listing_page", {})
        if not listing_cfg or not result.html:
            return []
        soup = BeautifulSoup(result.html, "lxml")
        card_sel = listing_cfg.get("card_container", {}).get("css", "")
        if not card_sel:
            return []
        rows: list[dict] = []
        for card in soup.select(card_sel):
            row: dict = {}
            for fname, sel in listing_cfg.items():
                if not isinstance(sel, dict) or fname == "card_container":
                    continue
                css = sel.get("css", "")
                attr = sel.get("attribute")
                if not css:
                    continue
                el = card.select_one(css)
                if not el:
                    continue
                if attr == "href":
                    row[fname] = el.get("href")
                elif attr == "src":
                    row[fname] = el.get("src")
                elif attr == "text":
                    row[fname] = el.get_text(" ", strip=True)
                elif isinstance(attr, str):
                    v = el.get(attr)
                    row[fname] = v if isinstance(v, str) else el.get_text(" ", strip=True)
            if row.get("article_url"):
                rows.append(row)
        return rows

    def _dedupe(self, items: list[ListingItem]) -> list[ListingItem]:
        """In-memory dedupe + DB-based dedupe (handled by Stage B / persistence)."""
        seen: set[str] = set()
        out: list[ListingItem] = []
        for it in items:
            if it.url in seen:
                continue
            seen.add(it.url)
            out.append(it)
        return out

    def filter_fresh(self, items: list[ListingItem], db: Session) -> list[ListingItem]:
        """Drop URLs that already exist in the DB and are fresh enough.

        Re-scrapes articles older than `recheck_window_hours`.
        """
        urls = [it.url for it in items]
        if not urls:
            return []
        existing = db.execute(
            select(Article.source_url, Article.scraped_at).where(Article.source_url.in_(urls))
        ).all()
        existing_map = {row[0]: row[1] for row in existing}
        threshold = datetime.now(UTC) - timedelta(
            hours=self._settings.recheck_window_hours
        )
        kept: list[ListingItem] = []
        for it in items:
            if self._settings.force_refresh:
                kept.append(it)
                continue
            seen_at = existing_map.get(it.url)
            if seen_at is None or seen_at < threshold:
                kept.append(it)
        return kept
