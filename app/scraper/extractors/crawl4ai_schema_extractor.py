"""Tier 3 — Crawl4AI JsonCssExtractionStrategy.

Schema is loaded from `crawl4ai_schema.json`. The strategy asks Crawl4AI
to do an in-browser, JS-aware CSS extract; this is more robust than
static BeautifulSoup because it sees the post-render DOM.

We use it for both listing pages and article pages (two schemas in
the same JSON file).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from app.core.config import get_settings
from app.scraper.article_data import ArticleData
from app.scraper.extractors.base import BaseExtractor


def _load_schema(schema_key: str) -> dict[str, Any] | None:
    settings = get_settings()
    path = Path(settings.extraction_config_dir) / settings.crawl4ai_schema_file
    try:
        with path.open() as f:
            data = json.load(f)
        return data.get(schema_key)
    except FileNotFoundError:
        logger.warning("crawl4ai_schema.json not found at {p}", p=path)
        return None
    except json.JSONDecodeError as e:
        logger.error("crawl4ai_schema.json is invalid JSON: {e}", e=e)
        return None


class Crawl4AISchemaExtractor(BaseExtractor):
    name = "crawl4ai_schema"

    async def extract_article(self, fetch_result: Any) -> ArticleData | None:
        schema = _load_schema("article_schema")
        if not schema:
            return None
        return await self._run_schema(fetch_result, schema, kind="article")

    async def extract_listing(self, fetch_result: Any) -> list[dict[str, Any]] | None:
        schema = _load_schema("listing_schema")
        if not schema:
            return None
        return await self._run_schema(fetch_result, schema, kind="listing")

    # BaseExtractor interface — article schema is the default
    async def extract(self, url: str, payload: Any) -> ArticleData | None:
        return await self.extract_article(payload)

    # ---- internals ----------------------------------------------------
    async def _run_schema(
        self,
        fetch_result: Any,
        schema: dict[str, Any],
        *,
        kind: str,
    ) -> Any:
        try:
            from crawl4ai.extraction_strategy import (  # type: ignore
                JsonCssExtractionStrategy,
            )
        except ImportError:
            logger.warning("crawl4ai not installed; JsonCss strategy disabled")
            return None
        try:
            strategy = JsonCssExtractionStrategy(schema=schema)
            # The strategy operates on raw HTML; for listing vs article we
            # pass the appropriate html field from the FetchResult.
            html = self._html_for(fetch_result, kind=kind)
            if not html:
                return None
            # Run synchronously — JsonCss is pure-Python CSS extraction.
            import asyncio as _aio

            url = getattr(fetch_result, "url", "") or ""
            extracted = await _aio.to_thread(strategy.run, url, html)
        except Exception as e:
            logger.warning("crawl4ai_schema {k} extraction failed: {e}", k=kind, e=e)
            return None

        if not extracted:
            logger.warning(
                "Possible site structure change: crawl4ai_schema {k} returned empty for {u}",
                k=kind, u=getattr(fetch_result, "url", "?"),
            )
            return [] if kind == "listing" else None

        if kind == "listing":
            return extracted

        # Article case
        first = extracted[0] if extracted else {}
        return self._first_to_article(getattr(fetch_result, "url", ""), first)

    @staticmethod
    def _html_for(fetch_result: Any, *, kind: str) -> str | None:
        if fetch_result is None:
            return None
        # Prefer cleaned HTML for article, raw HTML for listing (more cards).
        if kind == "article" and getattr(fetch_result, "cleaned_html", None):
            return fetch_result.cleaned_html
        return getattr(fetch_result, "html", None)

    def _first_to_article(self, url: str, node: dict[str, Any]) -> ArticleData:
        a = self._empty_article(url)
        a.title = self._safe_text(node.get("title") or node.get("preview_title"))
        a.summary = self._safe_text(node.get("summary") or node.get("description"))
        a.top_image_url = self._safe_text(node.get("top_image_url") or node.get("preview_thumbnail_url"))
        a.source_publisher = self._safe_text(node.get("source_publisher"))
        published_raw = self._safe_text(node.get("published_at"))
        if published_raw:
            a.published_at = published_raw
        body = node.get("full_content")
        if isinstance(body, list):
            a.full_content = "\n\n".join(str(p) for p in body if p)
        elif isinstance(body, str):
            a.full_content = body
        tickers = node.get("tickers")
        if isinstance(tickers, list):
            a.related_tickers = [str(t).strip().upper() for t in tickers if t]
        return a
