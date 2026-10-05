"""End-to-end pipeline test against stored HTML fixtures (no live Yahoo)."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.scraper.pipeline import ExtractionPipeline


@dataclass
class FakeFetch:
    url: str
    html: str
    cleaned_html: str = ""
    raw_markdown: str = "# Fed\n\nThe Federal Reserve cut rates…"
    status: int = 200
    via: str = "test"


@pytest.mark.asyncio
async def test_full_pipeline_against_fixtures(article_html, listing_html):
    pipeline = ExtractionPipeline(enable_llm=False)

    # ---- Stage A simulation ------------------------------------------
    # We don't run the full ListingCrawler (it hits the network); instead we
    # verify the CSS-tier listing fallback works on the listing fixture.
    from app.scraper.listing_crawler import ListingCrawler

    crawler = ListingCrawler(fetcher=None)  # type: ignore[arg-type]
    rows = crawler._css_listing_fallback(FakeFetch(
        url="https://finance.yahoo.com/topic/stock-market-news/",
        html=listing_html,
    ))
    assert len(rows) >= 3
    assert any("/news/" in r["article_url"] for r in rows)

    # ---- Stage B simulation ------------------------------------------
    fetch = FakeFetch(
        url="https://finance.yahoo.com/news/fed-cuts-rates-20261005123000-456.html",
        html=article_html,
    )
    article = await pipeline.run_for_url(fetch)
    assert article.title is not None
    assert "Federal Reserve" in (article.full_content or "")
    # At least one tier should have supplied a field
    assert article.extraction_tier_used
    # Status should be success or partial (some fields may not survive the
    # simplified fixture) — but never 'failed' for this rich fixture.
    assert article.status in ("success", "partial_extraction")


@pytest.mark.asyncio
async def test_pipeline_graceful_on_empty_html():
    pipeline = ExtractionPipeline(enable_llm=False)
    fetch = FakeFetch(url="https://example.com/empty", html="<html><body></body></html>")
    article = await pipeline.run_for_url(fetch)
    # No fields filled -> failed
    assert article.status == "failed"
    # Tier provenance map should still be populated
    assert isinstance(article.extraction_tier_used, dict)
