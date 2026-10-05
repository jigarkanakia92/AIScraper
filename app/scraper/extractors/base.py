"""Base extractor interface + ArticleData carrier.

Every tier implements `extract(url, input_payload) -> ArticleData | None`.
The pipeline calls tiers in priority order, merging the highest-priority
non-empty value per field.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.scraper.article_data import ArticleData


class BaseExtractor(ABC):
    """Common interface for all extraction tiers."""

    #: Identifier used in `extraction_tier_used[field]`.
    name: str = "base"

    @abstractmethod
    async def extract(
        self, url: str, payload: Any
    ) -> ArticleData | None:
        """Return a partially-populated ArticleData or None.

        `payload` is whatever the pipeline passes in (FetchResult, raw HTML,
        RSS feed dict, etc.). Implementations should never raise — return
        None on any unrecoverable issue and log a warning.
        """

    # ---- helpers subclasses can reuse --------------------------------
    @staticmethod
    def _empty_article(url: str) -> ArticleData:
        return ArticleData(source_url=url)

    @staticmethod
    def _safe_text(value: Any) -> str | None:
        if value is None:
            return None
        s = str(value).strip()
        return s or None
