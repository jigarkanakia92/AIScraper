"""Tier 6 — newspaper4k (news-metadata heuristics)."""
from __future__ import annotations

from typing import Any

from loguru import logger

from app.scraper.article_data import ArticleData
from app.scraper.extractors.base import BaseExtractor


class NewspaperExtractor(BaseExtractor):
    name = "newspaper"

    async def extract(self, url: str, payload: Any) -> ArticleData | None:
        html = self._html_from(payload)
        if not html:
            return None
        try:
            from newspaper import Article  # type: ignore
        except ImportError:
            logger.warning("newspaper4k not installed; Tier 6 disabled")
            return None
        try:
            import asyncio as _aio

            def _do() -> ArticleData:
                # newspaper4k >= 0.9 uses the attribute API: assign html, then parse.
                a = Article(url=url or "")
                a.html = html
                a.parse()
                art = self._empty_article(url)
                art.title = self._safe_text(a.title)
                # newspaper4k metadata fields
                meta = a.meta_data or {}
                art.summary = self._safe_text(
                    meta.get("description") or meta.get("og", {}).get("description")
                )
                art.top_image_url = self._safe_text(
                    meta.get("og", {}).get("image") or meta.get("top_image")
                )
                if a.publish_date:
                    art.published_at = a.publish_date.isoformat()
                if a.authors:
                    art.authors = [{"name": n} for n in a.authors]
                if a.keywords:
                    art.tags = list(a.keywords)
                art.language = self._safe_text(meta.get("lang"))
                if a.text:
                    art.full_content = a.text
                if isinstance(meta.get("article"), dict) and meta["article"].get("section"):
                    art.category = self._safe_text(meta["article"]["section"])
                return art

            article = await _aio.to_thread(_do)
        except Exception as e:
            logger.warning("newspaper4k extraction failed: {e}", e=e)
            return None
        if not article.title and not article.full_content:
            return None
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
