"""Stage A — listing crawl.

Walks Yahoo Finance topic pages with URL-based pagination (``?start=<offset>``),
extracts article cards, deduplicates them against the database (by URL **and**
by normalized title + published_at), and applies an incremental *cutoff*:

- If the DB is empty  → scrape the last ``initial_scrape_days`` days.
- If the DB has rows  → scrape up to the newest article already present
  (tracked by both title and published time), then stop.

Pagination stops early when a run of consecutive known/old cards is seen,
which keeps the crawler from walking forever on large topics.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from dateutil import parser as date_parser
from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Article
from app.scraper.fetcher import Fetcher, FetchResult


# --------------------------------------------------------------------------- #
# Data model                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class ListingItem:
    url: str
    preview_title: str | None = None
    preview_thumbnail_url: str | None = None
    preview_timestamp: str | None = None
    preview_published_at: datetime | None = None  # parsed, tz-aware (UTC)
    source_publisher: str | None = None


@dataclass
class ListingState:
    """State derived from the DB that drives incremental crawling."""

    is_empty: bool = True
    cutoff: datetime | None = None        # stop when cards are older than this
    newest_published_at: datetime | None = None
    # Dedupe sets built from existing DB rows + items already queued this run.
    seen_urls: set[str] = field(default_factory=set)
    seen_keys: set[tuple[str, datetime]] = field(default_factory=set)  # (norm_title, pub_at truncated to minute)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
_PUNCT_RE = re.compile(
    r"[\s\u200b\u200c\u200d\ufeff\-_\u2013\u2014\u00b7\u2022|,.:;!?\"'\u2018\u2019\u201c\u201d()\[\]{}<>]+"
)


def normalize_title(title: str | None) -> str:
    """Normalize a title for fuzzy-duplicate detection.

    - Unicode NFKC
    - lowercase
    - strip punctuation/whitespace/zero-width chars
    """
    if not title:
        return ""
    t = unicodedata.normalize("NFKC", title).strip().lower()
    t = _PUNCT_RE.sub("", t)
    return t


def parse_preview_datetime(raw: str | None) -> datetime | None:
    """Parse a preview timestamp into a UTC-aware datetime.

    Accepts ISO-8601 (``<time datetime=...>``) or Yahoo's relative strings
    like "2 hours ago" (we can't resolve those precisely, so return None
    and fall back to the article-page extractor).
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    # Relative times ("5 minutes ago", "yesterday") — can't pin down exactly.
    if re.search(r"(ago|yesterday|hour|min|second)", raw, re.IGNORECASE):
        return None
    try:
        dt = date_parser.parse(raw)
    except (ValueError, TypeError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt


def truncate_minute(dt: datetime | None) -> datetime | None:
    """Truncate to minute resolution so tiny differences don't defeat dedupe."""
    if dt is None:
        return None
    return dt.replace(second=0, microsecond=0)


def add_pagination_query(url: str, *, start: int, count: int) -> str:
    """Set (or overwrite) ``start`` / ``count`` query params on *url*."""
    parts = urlparse(url)
    qs = parse_qs(parts.query, keep_blank_values=True)
    qs["start"] = [str(start)]
    qs["count"] = [str(count)]
    new_query = urlencode(qs, doseq=True)
    return urlunparse(parts._replace(query=new_query))


# --------------------------------------------------------------------------- #
# Crawler                                                                     #
# --------------------------------------------------------------------------- #
class ListingCrawler:
    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher
        self._settings = get_settings()
        self._url_re = re.compile(self._settings.article_url_pattern)

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    async def crawl(self, db: Session | None = None) -> list[ListingItem]:
        """Paginate the target topic page and return fresh article cards.

        When *db* is provided, uses it to drive incremental cutoffs and
        to skip duplicates. If ``db`` is None (tests), a single-page crawl
        is performed (matches historical behavior).
        """
        target = self._settings.target_url
        state = self._load_state(db) if db is not None else ListingState()

        all_items: list[ListingItem] = []
        consecutive_old = 0
        pages_fetched = 0
        page_size = self._settings.pagination_page_size
        max_pages = self._settings.pagination_max_pages
        stop_after = self._settings.pagination_stop_after_consecutive_old

        for page_num in range(max_pages):
            offset = page_num * page_size
            page_url = add_pagination_query(target, start=offset, count=page_size)
            logger.info(
                "Stage A: fetching page {p} (offset={o}) — {u}",
                p=page_num + 1, o=offset, u=page_url,
            )
            try:
                # Use a small amount of scroll per page — Yahoo serves the
                # offset page's cards in the initial HTML; extra scrolls
                # risk pulling in cards from the "next" offset, which our
                # dedupe will catch, but keeping it small minimizes wasted
                # requests and duplicate processing.
                per_page_scrolls = max(2, self._settings.pagination_page_size // 4)
                result = await self._fetcher.fetch(
                    page_url,
                    scroll=True,
                    wait_for="css:li.js-stream-content",
                    max_scrolls=per_page_scrolls,
                )
            except Exception as e:
                logger.warning(
                    "Stage A: page {p} fetch failed ({e!r}); stopping pagination",
                    p=page_num + 1, e=e,
                )
                break
            pages_fetched += 1

            raw_items = await self._parse_listing(result)
            logger.info(
                "Stage A: page {p} returned {n} raw cards",
                p=page_num + 1, n=len(raw_items),
            )

            if not raw_items:
                logger.info("Stage A: empty page — stopping pagination")
                break

            page_items: list[ListingItem] = []
            for it in raw_items:
                # In-memory + DB dedupe
                if self._is_known(it, state):
                    consecutive_old += 1
                    logger.debug(
                        "Stage A: skipping known item {u!r} (consecutive_old={c})",
                        u=it.url, c=consecutive_old,
                    )
                else:
                    consecutive_old = 0
                    page_items.append(it)
                    self._remember(it, state)

                # Incremental cutoff — have we reached news we already have?
                if self._is_past_cutoff(it, state):
                    consecutive_old += 1
                    logger.debug(
                        "Stage A: hit cutoff at {u!r} ({t})",
                        u=it.url, t=it.preview_published_at,
                    )

            all_items.extend(page_items)

            if consecutive_old >= stop_after:
                logger.info(
                    "Stage A: reached existing news / cutoff after {c} consecutive "
                    "old cards — stopping pagination (pages={p}, total={n})",
                    c=consecutive_old, p=pages_fetched, n=len(all_items),
                )
                break

        # Final dedupe pass (URLs seen across pages).
        deduped = self._dedupe(all_items)
        logger.info(
            "Stage A: {n} unique cards after paginating {p} page(s) (cutoff={c}, db_empty={e})",
            n=len(deduped), p=pages_fetched,
            c=state.cutoff.isoformat() if state.cutoff else None,
            e=state.is_empty,
        )
        return deduped

    # ------------------------------------------------------------------ #
    # Listing-page parsing                                               #
    # ------------------------------------------------------------------ #
    async def _parse_listing(self, result: FetchResult) -> list[ListingItem]:
        items: list[ListingItem] = []
        from app.scraper.extractors.crawl4ai_schema_extractor import (
            Crawl4AISchemaExtractor,
        )

        t3 = Crawl4AISchemaExtractor()
        data = await t3.extract_listing(result)
        if not data:
            logger.warning("Stage A: Tier 3 returned empty; using CSS fallback")
            data = await self._css_listing_fallback_async(result)
        for row in data:
            url = self._canonicalize_url(
                row.get("article_url") or row.get("url"),
                base=getattr(result, "url", None),
            )
            if not url:
                continue
            if not self._url_re.match(url):
                continue

            ts_raw = (
                row.get("preview_timestamp")
                or row.get("published_at")
                or row.get("preview_timestamp_text")
            )
            items.append(ListingItem(
                url=url,
                preview_title=(row.get("preview_title") or row.get("title") or "").strip() or None,
                preview_thumbnail_url=row.get("preview_thumbnail_url") or row.get("top_image_url"),
                preview_timestamp=ts_raw,
                preview_published_at=parse_preview_datetime(ts_raw),
                source_publisher=row.get("source_publisher"),
            ))
        return items

    def _canonicalize_url(self, url: Any, *, base: str | None) -> str | None:
        if not url:
            return None
        url = str(url).strip()
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            if base:
                b = urlparse(base)
                url = f"{b.scheme}://{b.netloc}{url}"
            else:
                url = "https://finance.yahoo.com" + url
        # Drop Yahoo tracking query params that cause false duplicates.
        parts = urlparse(url)
        if parts.netloc.endswith("yahoo.com"):
            qs = parse_qs(parts.query, keep_blank_values=True)
            for k in ("guccounter", "guce_referrer", "fr", "utm_source",
                      "utm_medium", "utm_campaign", "ncid"):
                qs.pop(k, None)
            url = urlunparse(parts._replace(
                query=urlencode(qs, doseq=True),
                fragment="",
            ))
        return url

    async def _css_listing_fallback_async(self, result: FetchResult) -> list[dict]:
        import asyncio as _aio
        return await _aio.to_thread(self._css_listing_fallback, result)

    def _css_listing_fallback(self, result: FetchResult) -> list[dict]:
        try:
            from bs4 import BeautifulSoup  # type: ignore
        except ImportError:
            return []
        try:
            import yaml
        except ImportError:
            return []
        from pathlib import Path
        settings = get_settings()
        with (Path(settings.extraction_config_dir) / settings.selectors_file).open() as f:
            cfg = yaml.safe_load(f) or {}
        listing_cfg = cfg.get("listing_page", {})
        if not listing_cfg or not result.html:
            return []
        soup = BeautifulSoup(result.html, "lxml")
        card_sel = listing_cfg.get("card_container", {}).get("css", "")
        if not card_sel:
            return []
        rows: list[dict] = []
        for card in soup.select(card_sel):
            row: dict = {}
            for fname, sel in listing_cfg.items():
                if not isinstance(sel, dict) or fname == "card_container":
                    continue
                css = sel.get("css", "")
                attr = sel.get("attribute")
                if not css:
                    continue
                el = card.select_one(css)
                if not el:
                    continue
                if attr == "href":
                    row[fname] = el.get("href")
                elif attr == "src":
                    row[fname] = el.get("src")
                elif attr == "datetime":
                    row[fname] = el.get("datetime") or el.get("content") or el.get_text(" ", strip=True)
                elif attr == "text":
                    row[fname] = el.get_text(" ", strip=True)
                elif isinstance(attr, str):
                    v = el.get(attr)
                    row[fname] = v if isinstance(v, str) else el.get_text(" ", strip=True)
            # Prefer <time datetime> if present, for accurate sorting.
            t_el = card.select_one("time[datetime]")
            if t_el and not row.get("preview_timestamp"):
                row["preview_timestamp"] = t_el.get("datetime")
            if row.get("article_url"):
                rows.append(row)
        return rows

    # ------------------------------------------------------------------ #
    # Incremental state + dedupe                                         #
    # ------------------------------------------------------------------ #
    def _load_state(self, db: Session) -> ListingState:
        state = ListingState()
        total = db.scalar(select(func.count(Article.id))) or 0
        if total == 0:
            state.is_empty = True
            state.cutoff = datetime.now(UTC) - timedelta(
                days=self._settings.initial_scrape_days
            )
            logger.info(
                "Stage A: DB is empty — initial window = last {d} days (cutoff={c})",
                d=self._settings.initial_scrape_days, c=state.cutoff.isoformat(),
            )
            return state

        state.is_empty = False
        # Newest article in DB — stop crawling once we walk back past it.
        newest_row = db.execute(
            select(
                func.max(Article.published_at),
                func.max(Article.first_seen_at),
            )
        ).one()
        newest_pub, newest_seen = newest_row
        state.newest_published_at = newest_pub
        # Use the later of published_at / first_seen_at as the cutoff so we
        # don't miss anything published between two runs.
        cutoff_candidates = [dt for dt in (newest_pub, newest_seen) if dt is not None]
        state.cutoff = max(cutoff_candidates) if cutoff_candidates else datetime.now(UTC)
        # Add a small safety margin (2 minutes) so cards published right at
        # the cutoff boundary are still re-checked (URL dedupe will drop them).
        state.cutoff -= timedelta(minutes=2)
        logger.info(
            "Stage A: DB has {n} articles — incremental cutoff={c} (newest_pub={p})",
            n=total,
            c=state.cutoff.isoformat(),
            p=newest_pub.isoformat() if newest_pub else None,
        )

        # Prime the dedupe sets with existing DB rows. We restrict to
        # reasonably-recent rows to keep memory bounded; older rows would
        # be caught by the URL unique constraint anyway.
        horizon = datetime.now(UTC) - timedelta(days=max(self._settings.initial_scrape_days * 2, 14))
        existing = db.execute(
            select(Article.source_url, Article.title, Article.published_at)
            .where(
                or_(
                    Article.published_at.is_(None),
                    Article.published_at >= horizon,
                )
            )
        ).all()
        for url, title, pub_at in existing:
            if url:
                state.seen_urls.add(url)
            key = self._title_time_key(title, pub_at)
            if key is not None:
                state.seen_keys.add(key)

        return state

    @staticmethod
    def _title_time_key(title: str | None, pub_at: datetime | None) -> tuple[str, datetime] | None:
        nt = normalize_title(title)
        pt = truncate_minute(pub_at)
        if not nt or pt is None:
            return None
        return (nt, pt)

    def _is_known(self, item: ListingItem, state: ListingState) -> bool:
        if item.url in state.seen_urls:
            return True
        # Only treat (title, time) as "known" if we actually have a parsed time
        # from the listing; otherwise defer to URL upsert in Stage B.
        key = self._title_time_key(item.preview_title, item.preview_published_at)
        return bool(key is not None and key in state.seen_keys)

    def _remember(self, item: ListingItem, state: ListingState) -> None:
        state.seen_urls.add(item.url)
        key = self._title_time_key(item.preview_title, item.preview_published_at)
        if key is not None:
            state.seen_keys.add(key)

    def _is_past_cutoff(self, item: ListingItem, state: ListingState) -> bool:
        """Is this item older than our incremental cutoff?"""
        if state.cutoff is None:
            return False
        if item.preview_published_at is None:
            # Can't tell from listing — don't stop; Stage B will dedupe.
            return False
        return item.preview_published_at < state.cutoff

    # ------------------------------------------------------------------ #
    # In-memory dedupe                                                   #
    # ------------------------------------------------------------------ #
    def _dedupe(self, items: list[ListingItem]) -> list[ListingItem]:
        seen_urls: set[str] = set()
        seen_keys: set[tuple[str, datetime]] = set()
        out: list[ListingItem] = []
        for it in items:
            if it.url in seen_urls:
                continue
            key = self._title_time_key(it.preview_title, it.preview_published_at)
            if key is not None and key in seen_keys:
                continue
            seen_urls.add(it.url)
            if key is not None:
                seen_keys.add(key)
            out.append(it)
        return out

    # ------------------------------------------------------------------ #
    # Stage-B helper: DB-side freshness + title/time dedupe              #
    # ------------------------------------------------------------------ #
    def filter_fresh(self, items: list[ListingItem], db: Session) -> list[ListingItem]:
        """Drop URLs/title+time pairs that already exist and are fresh enough."""
        if not items:
            return []
        # Pass the full db-driven state to reuse our dedupe primitives.
        state = self._load_state(db)
        # Also seed with URLs seen this run (shouldn't happen, but safe).
        for it in items:
            self._remember(it, state)

        threshold = datetime.now(UTC) - timedelta(
            hours=self._settings.recheck_window_hours
        )
        urls = [it.url for it in items]
        existing = db.execute(
            select(
                Article.source_url,
                Article.title,
                Article.published_at,
                Article.scraped_at,
            ).where(Article.source_url.in_(urls))
        ).all()
        existing_map = {row[0]: row for row in existing}

        # Title+time lookup for non-URL matches
        title_rows = db.execute(
            select(Article.source_url, Article.title, Article.published_at, Article.scraped_at)
            .where(Article.title.isnot(None), Article.published_at.isnot(None))
        ).all()
        tt_map: dict[tuple[str, datetime], tuple] = {}
        for url, title, pub_at, scraped_at in title_rows:
            key = self._title_time_key(title, pub_at)
            if key is not None:
                tt_map[key] = (url, scraped_at)

        kept: list[ListingItem] = []
        for it in items:
            if self._settings.force_refresh:
                kept.append(it)
                continue

            row = existing_map.get(it.url)
            seen_at = row[3] if row else None
            if seen_at is not None and seen_at >= threshold:
                # Already scraped recently — skip.
                continue

            # Title+time duplicate that we didn't catch via URL?
            if row is None and it.preview_title and it.preview_published_at:
                key = self._title_time_key(it.preview_title, it.preview_published_at)
                tt = tt_map.get(key) if key else None
                if tt is not None:
                    scraped_at = tt[1]
                    if scraped_at is None or scraped_at < threshold:
                        # Old record, re-queue under its own URL would be
                        # wrong, so just skip (duplicate).
                        pass
                    continue  # duplicate

            kept.append(it)
        return kept
