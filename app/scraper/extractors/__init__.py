"""Extractor tier registry.

Adding a new tier = (1) implement BaseExtractor, (2) register it below in
TIER_REGISTRY with a priority. The pipeline iterates TIER_REGISTRY in
priority order.
"""
from __future__ import annotations

from app.scraper.extractors.base import BaseExtractor
from app.scraper.extractors.crawl4ai_schema_extractor import Crawl4AISchemaExtractor
from app.scraper.extractors.css_fallback_extractor import CssFallbackExtractor
from app.scraper.extractors.jsonld_extractor import JsonLdExtractor
from app.scraper.extractors.newspaper_extractor import NewspaperExtractor
from app.scraper.extractors.opengraph_extractor import OpenGraphExtractor
from app.scraper.extractors.rss_extractor import RssExtractor
from app.scraper.extractors.trafilatura_extractor import TrafilaturaExtractor

# Priority order matches Section 3 of the spec.
TIER_REGISTRY: list[BaseExtractor] = [
    RssExtractor(),
    JsonLdExtractor(),
    Crawl4AISchemaExtractor(),
    OpenGraphExtractor(),
    TrafilaturaExtractor(),
    NewspaperExtractor(),
    CssFallbackExtractor(),
]


def get_tier(name: str) -> BaseExtractor | None:
    for t in TIER_REGISTRY:
        if t.name == name:
            return t
    return None
