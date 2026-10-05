"""Pydantic v2 model representing a normalized article.

This is the *carrier* passed between extractor tiers and the persistence
layer. Every field is optional; the merge logic in pipeline.py fills
fields tier-by-tier, preferring higher-priority data.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ArticleData(BaseModel):
    """Normalized article. Mirrors `articles` table but not 1:1 — the
    ORM is the persistence model, this is the in-flight model."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    # Use str, not HttpUrl — in-flight merges raw strings from many tiers,
    # and we don't want validation to fail a merge mid-pipeline. The DB
    # column is Text; the persistence layer normalizes.
    source_url: str

    title: str | None = None
    summary: str | None = None
    full_content: str | None = None
    full_content_markdown: str | None = None
    # Accept dicts (from JSON-LD) and AuthorInfo (from other tiers).
    authors: list[dict] = Field(default_factory=list)
    # Carriers between extractors — keep as strings so merge never fails
    # on a partial value. The DB layer (article_crawler._parse_dt) parses
    # to a proper datetime before persistence.
    published_at: str | None = None
    updated_at: str | None = None

    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    related_tickers: list[str] = Field(default_factory=list)
    source_publisher: str | None = None

    top_image_url: str | None = None
    language: str | None = None

    extraction_tier_used: dict[str, str] = Field(default_factory=dict)
    raw_html: str | None = None
    scraped_at: datetime | None = None
    status: str = "success"

    # The Crawl4AI CrawlResult, kept so downstream consumers can
    # pull markdown/cleaned_html without re-fetching.
    crawl_result: Any | None = None

    def is_field_filled(self, field_name: str) -> bool:
        val = getattr(self, field_name, None)
        if val is None:
            return False
        if isinstance(val, (list, str, dict)):
            return len(val) > 0
        return True

    def merge(self, other: ArticleData, tier_name: str) -> ArticleData:
        """Merge `other`'s non-empty fields into self.

        Higher-priority (earlier) values win. The pipeline tracks
        *which* fields each tier newly populated and records provenance
        there — this method only handles the data, not the bookkeeping.
        The `tier_name` argument is accepted for backwards-compatibility
        and is currently a no-op.
        """
        from app.scraper.article_data import ArticleData as _Cls

        del tier_name  # provenance is tracked by the pipeline
        for fname in _Cls.model_fields:
            if fname in {"source_url", "extraction_tier_used", "raw_html", "crawl_result", "status", "scraped_at"}:
                continue
            other_val = getattr(other, fname, None)
            self_val = getattr(self, fname, None)
            if other_val not in (None, "", [], {}) and self_val in (None, "", [], {}):
                setattr(self, fname, other_val)
        return self
