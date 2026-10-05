"""Tier 5 — trafilatura (content-density heuristics)."""
from __future__ import annotations

from typing import Any

from loguru import logger

from app.scraper.article_data import ArticleData
from app.scraper.extractors.base import BaseExtractor


class TrafilaturaExtractor(BaseExtractor):
    name = "trafilatura"

    async def extract(self, url: str, payload: Any) -> ArticleData | None:
        html = self._html_from(payload)
        if not html:
            return None
        try:
            import trafilatura  # type: ignore
        except ImportError:
            logger.warning("trafilatura not installed; Tier 5 disabled")
            return None
        try:
            import asyncio as _aio

            text = await _aio.to_thread(
                trafilatura.extract, html, include_comments=False, include_tables=False
            )
            markdown = await _aio.to_thread(
                trafilatura.extract, html, output_format="markdown",
                include_comments=False, include_tables=False
            )
        except Exception as e:
            logger.warning("trafilatura extraction failed: {e}", e=e)
            return None
        if not text:
            return None
        article = self._empty_article(url)
        article.full_content = text.strip()
        article.full_content_markdown = (markdown or "").strip() or None
        return article

    @staticmethod
    def _html_from(payload: Any) -> str | None:
        if payload is None:
            return None
        if hasattr(payload, "html"):
            return payload.html
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            return payload.get("html")
        return None
