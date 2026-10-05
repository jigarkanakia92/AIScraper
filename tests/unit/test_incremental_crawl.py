"""Tests for the new incremental/paginated listing crawl logic.

These tests exercise pure helpers + in-memory behavior. DB-backed tests
need PostgreSQL (ARRAY column type) and are covered by integration tests
elsewhere / when run against a real database.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.scraper.listing_crawler import (
    ListingCrawler,
    ListingItem,
    ListingState,
    add_pagination_query,
    normalize_title,
    parse_preview_datetime,
    truncate_minute,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_normalize_title_strips_punctuation_and_case():
    assert normalize_title("  Hello,  World!  ") == "helloworld"
    assert normalize_title("Apple – beats earnings?") == normalize_title("Apple beats earnings")
    assert normalize_title("") == ""
    assert normalize_title(None) == ""


def test_parse_preview_datetime_absolute_vs_relative():
    dt = parse_preview_datetime("2026-10-04T12:34:56Z")
    assert dt is not None and dt.year == 2026 and dt.tzinfo is not None
    # Relative strings cannot be resolved to an exact timestamp — we return
    # None so the pipeline relies on the article-page extract instead.
    assert parse_preview_datetime("2 hours ago") is None
    assert parse_preview_datetime("yesterday") is None
    assert parse_preview_datetime(None) is None


def test_truncate_minute():
    dt = datetime(2026, 1, 2, 3, 4, 45, 123456, tzinfo=UTC)
    assert truncate_minute(dt) == datetime(2026, 1, 2, 3, 4, 0, 0, tzinfo=UTC)
    assert truncate_minute(None) is None


def test_add_pagination_query():
    u0 = add_pagination_query(
        "https://finance.yahoo.com/topic/stock-market-news/", start=0, count=10
    )
    assert "start=0" in u0 and "count=10" in u0
    u10 = add_pagination_query(u0, start=10, count=10)
    # Must overwrite, not append — otherwise URL accumulates duplicate keys.
    assert "start=10" in u10
    assert "start=0" not in u10


# ---------------------------------------------------------------------------
# URL canonicalization (tracker stripping + relative-URL absolutization)
# ---------------------------------------------------------------------------
@pytest.fixture()
def crawler() -> ListingCrawler:
    return ListingCrawler(fetcher=MagicMock())


def test_canonicalize_strips_yahoo_trackers(crawler):
    u = crawler._canonicalize_url(
        "https://finance.yahoo.com/news/x.html?guccounter=1&utm_source=tw&guce_referrer=a",
        base=None,
    )
    assert u is not None
    assert "guccounter" not in u
    assert "utm_source" not in u
    assert u.startswith("https://finance.yahoo.com/news/x.html")


def test_canonicalize_relative_url(crawler):
    u = crawler._canonicalize_url(
        "/news/y.html",
        base="https://finance.yahoo.com/topic/stock-market-news/",
    )
    assert u == "https://finance.yahoo.com/news/y.html"


def test_url_pattern_rejects_non_articles(crawler):
    # The regex filter (applied after canonicalize in _parse_listing)
    # rejects quote pages, topic pages, etc.
    assert crawler._url_re.match("https://finance.yahoo.com/news/real.html")
    assert not crawler._url_re.match("https://finance.yahoo.com/quote/AAPL")
    assert not crawler._url_re.match("https://finance.yahoo.com/topic/stock-market-news/")


# ---------------------------------------------------------------------------
# In-memory dedupe
# ---------------------------------------------------------------------------
def test_in_memory_dedupe_drops_duplicate_urls(crawler):
    now = datetime.now(UTC)
    items = [
        ListingItem(url="https://a.example.com/1.html", preview_title="A", preview_published_at=now),
        ListingItem(url="https://a.example.com/1.html", preview_title="A", preview_published_at=now),
        ListingItem(url="https://a.example.com/2.html", preview_title="B", preview_published_at=now),
    ]
    out = crawler._dedupe(items)
    assert len(out) == 2


def test_in_memory_dedupe_drops_duplicate_title_time(crawler):
    now = truncate_minute(datetime.now(UTC))
    items = [
        ListingItem(url="https://a.example.com/a.html", preview_title="Same Title",
                    preview_published_at=now),
        ListingItem(url="https://a.example.com/b.html", preview_title="same title!",
                    preview_published_at=now + timedelta(seconds=25)),
        ListingItem(url="https://a.example.com/c.html", preview_title="Different",
                    preview_published_at=now),
    ]
    out = crawler._dedupe(items)
    assert len(out) == 2
    urls = {i.url for i in out}
    assert "https://a.example.com/a.html" in urls
    assert "https://a.example.com/c.html" in urls


# ---------------------------------------------------------------------------
# Cutoff logic
# ---------------------------------------------------------------------------
def test_empty_db_cutoff_is_three_days():
    """When the DB is empty, crawl window = last initial_scrape_days days."""
    state = ListingState(is_empty=True, cutoff=datetime.now(UTC) - timedelta(days=3))
    assert state.is_empty
    assert 2 <= (datetime.now(UTC) - state.cutoff).days <= 3


def test_is_past_cutoff_when_published_before_cutoff(crawler):
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=4)
    state = ListingState(is_empty=False, cutoff=cutoff)
    old = ListingItem(
        url="https://finance.yahoo.com/news/old.html",
        preview_title="Old",
        preview_published_at=now - timedelta(days=2),
    )
    new = ListingItem(
        url="https://finance.yahoo.com/news/new.html",
        preview_title="New",
        preview_published_at=now - timedelta(minutes=5),
    )
    unknown_time = ListingItem(url="https://finance.yahoo.com/news/x.html",
                               preview_title="X", preview_published_at=None)
    assert crawler._is_past_cutoff(old, state) is True
    assert crawler._is_past_cutoff(new, state) is False
    # Without a parsed time, never claim past-cutoff (Stage B will dedupe).
    assert crawler._is_past_cutoff(unknown_time, state) is False


def test_is_known_by_url(crawler):
    state = ListingState()
    state.seen_urls.add("https://finance.yahoo.com/news/seen.html")
    it = ListingItem(url="https://finance.yahoo.com/news/seen.html",
                     preview_title="Seen", preview_published_at=None)
    assert crawler._is_known(it, state) is True
    it2 = ListingItem(url="https://finance.yahoo.com/news/new.html",
                      preview_title="New", preview_published_at=None)
    assert crawler._is_known(it2, state) is False


def test_is_known_by_title_time(crawler):
    now = truncate_minute(datetime.now(UTC))
    state = ListingState()
    # Normalize and add a known key
    key = crawler._title_time_key("Apple beats earnings!", now)
    assert key is not None
    state.seen_keys.add(key)

    it = ListingItem(
        url="https://finance.yahoo.com/news/different-url.html",
        preview_title="  Apple — beats earnings?",  # punctuation/case variant
        preview_published_at=now + timedelta(seconds=17),  # same minute
    )
    assert crawler._is_known(it, state) is True
