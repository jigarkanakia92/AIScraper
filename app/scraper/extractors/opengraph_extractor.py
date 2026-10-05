"""Tier 4 — Open Graph and Twitter Card meta tags.

The selectors come from selectors.yaml (article_page.meta_tags) so the
mapping is config-driven. Tier 3 (JSON-LD) is checked first because it
typically wins — this tier is the fallback for fields OG has but JSON-LD
doesn't, like `og:site_name` -> `source_publisher`.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from loguru import logger

from app.scraper.article_data import ArticleData
from app.scraper.extractors.base import BaseExtractor


class OpenGraphExtractor(BaseExtractor):
    name = "opengraph"

    async def extract(self, url: str, payload: Any) -> ArticleData | None:
        html = self._html_from(payload)
        if not html:
            return None
        try:
            from bs4 import BeautifulSoup  # type: ignore
        except ImportError:
            logger.warning("bs4 not installed; OG extractor disabled")
            return None

        article = self._empty_article(url)
        soup = BeautifulSoup(html, "lxml")
        for meta in soup.find_all("meta"):
            prop = meta.get("property") or ""
            name = meta.get("name") or ""
            raw_content = meta.get("content")
            content = raw_content.strip() if isinstance(raw_content, str) else ""
            if not content:
                continue

            if prop == "og:title" and not article.title:
                article.title = content
            elif prop == "og:description" and not article.summary:
                article.summary = content
            elif prop == "og:image" and not article.top_image_url:
                article.top_image_url = urljoin(url, content)
            elif prop == "og:site_name" and not article.source_publisher:
                article.source_publisher = content
            elif prop == "og:type":
                pass  # informational only
            elif name == "twitter:title" and not article.title:
                article.title = content
            elif name == "twitter:description" and not article.summary:
                article.summary = content
            elif name == "twitter:image" and not article.top_image_url:
                article.top_image_url = urljoin(url, content)
            elif name == "twitter:site" and not article.source_publisher:
                article.source_publisher = content

        # Only return the article if at least one field was populated.
        if not any([
            article.title, article.summary, article.top_image_url, article.source_publisher
        ]):
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
