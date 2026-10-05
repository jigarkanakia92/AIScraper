"""Tiered extraction pipeline.

`run_for_url` is the main entry point. It iterates TIER_REGISTRY in
priority order, calls each tier, and merges per-field results. The
resulting ArticleData is then handed to the persistence layer.

This module is pure async orchestration — it owns no HTTP and no DB.
"""
from __future__ import annotations

from datetime import UTC, datetime

from loguru import logger

from app.scraper.article_data import ArticleData
from app.scraper.extractors import TIER_REGISTRY
from app.scraper.fetcher import FetchResult

REQUIRED_FIELDS = ("title", "full_content")


def _filled_field_set(article: ArticleData) -> set[str]:
    """Snapshot the set of fields that currently have a non-empty value.

    Used by the pipeline to detect *which* fields a tier newly populated,
    so we can record provenance for them.
    """
    out: set[str] = set()
    for fname in ArticleData.model_fields:
        if fname in {"source_url", "extraction_tier_used", "raw_html", "crawl_result", "status", "scraped_at"}:
            continue
        v = getattr(article, fname, None)
        if v not in (None, "", [], {}):
            out.add(fname)
    return out


class ExtractionPipeline:
    """Runs all configured extractors and merges their results."""

    def __init__(self, *, enable_llm: bool = False) -> None:
        self._tiers = list(TIER_REGISTRY)
        if enable_llm:
            from app.scraper.extractors.llm_extractor import LlmExtractor
            self._tiers.append(LlmExtractor())

    async def run_for_url(
        self, fetch_result: FetchResult, *, url: str | None = None
    ) -> ArticleData:
        url = url or fetch_result.url
        article = ArticleData(source_url=url)
        article.raw_html = fetch_result.html
        article.full_content_markdown = (fetch_result.raw_markdown or None)

        for tier in self._tiers:
            try:
                partial = await tier.extract(url, fetch_result)
            except Exception as e:
                logger.warning(
                    "Tier {t} threw on {u}: {e!r}", t=tier.name, u=url, e=e
                )
                continue
            if partial is None:
                logger.debug("Tier {t}: no data for {u}", t=tier.name, u=url)
                continue
            # Record which fields this tier *first* populated. Capture
            # the field snapshot before merging so we know what was new.
            before = _filled_field_set(article)
            article.merge(partial, tier_name=tier.name)
            after = _filled_field_set(article)
            newly_filled = after - before
            for fname in newly_filled:
                article.extraction_tier_used[fname] = tier.name
            logger.debug(
                "Tier {t}: filled {n} new fields for {u}",
                t=tier.name,
                n=len(newly_filled),
                u=url,
            )

        # Tag the crawl source.
        article.extraction_tier_used.setdefault(
            "_crawl_via", getattr(fetch_result, "via", "unknown")
        )
        article.scraped_at = datetime.now(UTC)
        if not article.source_url:
            article.source_url = url

        # Determine status.
        missing = [f for f in REQUIRED_FIELDS if not article.is_field_filled(f)]
        if not missing:
            article.status = "success"
        elif article.title or article.full_content:
            article.status = "partial_extraction"
        else:
            article.status = "failed"

        return article
