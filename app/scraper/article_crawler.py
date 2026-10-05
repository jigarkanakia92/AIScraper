"""Stage B — article detail crawl.

For each queued URL:
  1. fetch (with full stealth + rate limiting)
  2. run the tiered extraction pipeline
  3. persist to PostgreSQL via upsert

Duplicate detection happens at two layers:

1. By ``source_url`` (DB UNIQUE constraint + ON CONFLICT DO UPDATE).
2. By normalized (title, published_at), so the same article syndicated
   under a different URL isn't inserted twice. In that case we log and
   skip rather than inserting a near-duplicate row.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Article, ArticleStatus
from app.scraper.article_data import ArticleData
from app.scraper.fetcher import Fetcher
from app.scraper.listing_crawler import ListingItem, normalize_title, truncate_minute
from app.scraper.pipeline import ExtractionPipeline
from app.scraper.rate_limiter import BlockedResponseError, CircuitOpenError


class ArticleCrawler:
    def __init__(self, fetcher: Fetcher, pipeline: ExtractionPipeline) -> None:
        self._fetcher = fetcher
        self._pipeline = pipeline

    async def process_queue(
        self, items: list[ListingItem], db: Session
    ) -> tuple[int, int, int]:
        """Returns (new_count, duplicate_count, failed_count)."""
        new = dup = fail = 0

        # Pre-cache recent title+pub_at pairs so we can detect duplicates
        # that arrive via different URLs within the same run.
        seen_tt: set[tuple[str, datetime]] = self._load_recent_title_time_keys(db)

        for item in items:
            try:
                result = await self._fetcher.fetch(item.url)
            except CircuitOpenError:
                logger.warning("Skipping {u}: circuit open", u=item.url)
                fail += 1
                raise
            except BlockedResponseError as e:
                logger.error("Blocked on {u}: {e!r}", u=item.url, e=e)
                fail += 1
                continue
            except Exception as e:
                logger.exception("Fetch failed for {u}: {e!r}", u=item.url, e=e)
                fail += 1
                continue

            try:
                article = await self._pipeline.run_for_url(result)
            except Exception as e:
                logger.exception("Pipeline failed for {u}: {e!r}", u=item.url, e=e)
                fail += 1
                continue

            published = self._parse_dt(article.published_at)
            tt_key = self._title_time_key(article.title, published)

            # In-run title+time dedupe.
            if tt_key is not None and tt_key in seen_tt:
                logger.info(
                    "Duplicate by (title, published_at): {t!r} @ {p} — skipping {u}",
                    t=article.title, p=published, u=item.url,
                )
                dup += 1
                continue

            # DB-side title+time dedupe (covers articles added by another
            # concurrent worker / previous page in this run).
            if tt_key is not None:
                existing_tt = self._find_by_title_time(db, article.title, published)
                if existing_tt is not None:
                    logger.info(
                        "Duplicate by (title, published_at) in DB: id={i} — skipping {u}",
                        i=existing_tt.id, u=item.url,
                    )
                    dup += 1
                    seen_tt.add(tt_key)
                    continue

            inserted = self._upsert(db, article, result)
            if inserted:
                new += 1
                if tt_key is not None:
                    seen_tt.add(tt_key)
            else:
                dup += 1
        return new, dup, fail

    # ------------------------------------------------------------------ #
    # Persistence                                                        #
    # ------------------------------------------------------------------ #
    def _upsert(self, db: Session, article: ArticleData, result: Any) -> bool:
        """Insert or update an article. Returns True if a *new* row was inserted."""
        raw = self._fetcher.compress_html(result.html) if result.html else None
        now = datetime.now(UTC)
        published = self._parse_dt(article.published_at)

        values: dict[str, Any] = {
            "source_url": str(article.source_url),
            "title": article.title,
            "summary": article.summary,
            "full_content": article.full_content,
            "full_content_markdown": article.full_content_markdown,
            "authors": (
                [a.model_dump() if hasattr(a, "model_dump") else a for a in article.authors]
                or None
            ),
            "published_at": published,
            "updated_at": self._parse_dt(article.updated_at),
            "scraped_at": now,
            "category": article.category,
            "tags": article.tags or None,
            "related_tickers": article.related_tickers or None,
            "source_publisher": article.source_publisher,
            "top_image_url": article.top_image_url,
            "language": article.language,
            "extraction_tier_used": article.extraction_tier_used or None,
            "status": ArticleStatus(article.status),
            "raw_html_compressed": raw,
        }

        # Does a row already exist for this source_url? We need to know for
        # accurate new/dup accounting; ON CONFLICT alone doesn't tell us.
        existing = db.execute(
            select(Article.id).where(Article.source_url == values["source_url"])
        ).scalar_one_or_none()

        stmt = pg_insert(Article).values(**values)
        update_cols = {k: v for k, v in values.items() if k not in {"source_url", "first_seen_at"}}
        stmt = stmt.on_conflict_do_update(
            index_elements=[Article.source_url],
            set_=update_cols,
        )
        db.execute(stmt)
        db.commit()
        return existing is None

    # ------------------------------------------------------------------ #
    # Title+time dedupe helpers                                          #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _title_time_key(title: str | None, published_at: datetime | None) -> tuple[str, datetime] | None:
        nt = normalize_title(title)
        pt = truncate_minute(published_at)
        if not nt or pt is None:
            return None
        return (nt, pt)

    @staticmethod
    def _load_recent_title_time_keys(db: Session, days: int = 30) -> set[tuple[str, datetime]]:
        """Pre-populate an in-memory set of recent (norm_title, pub_at) keys."""
        from datetime import timedelta
        horizon = datetime.now(UTC) - timedelta(days=days)
        rows = db.execute(
            select(Article.title, Article.published_at).where(
                Article.title.isnot(None),
                Article.published_at.isnot(None),
                Article.published_at >= horizon,
            )
        ).all()
        out: set[tuple[str, datetime]] = set()
        for title, pub_at in rows:
            key = ArticleCrawler._title_time_key(title, pub_at)
            if key is not None:
                out.add(key)
        return out

    @staticmethod
    def _find_by_title_time(db: Session, title: str | None, published_at: datetime | None) -> Article | None:
        key = ArticleCrawler._title_time_key(title, published_at)
        if key is None:
            return None
        nt, pt = key
        # Match with a 1-minute tolerance on either side to absorb clock skew.
        from datetime import timedelta
        lo = pt - timedelta(minutes=1)
        hi = pt + timedelta(minutes=1)
        # We pull candidates in the range and filter by normalized title in
        # Python to avoid depending on the DB collation.
        candidates = db.execute(
            select(Article).where(
                Article.published_at.between(lo, hi),
                Article.title.isnot(None),
            )
        ).scalars().all()
        for c in candidates:
            if normalize_title(c.title) == nt:
                return c
        return None

    # ------------------------------------------------------------------ #
    # Utils                                                              #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        try:
            from dateutil import parser as _p  # type: ignore

            dt = _p.parse(str(value))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except Exception:
            return None
