"""Stage B — article detail crawl.

For each queued URL:
  1. fetch (with full stealth + rate limiting)
  2. run the tiered extraction pipeline
  3. persist to PostgreSQL via upsert
"""
from __future__ import annotations

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Article, ArticleStatus
from app.scraper.article_data import ArticleData
from app.scraper.fetcher import Fetcher
from app.scraper.listing_crawler import ListingItem
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
        for item in items:
            try:
                result = await self._fetcher.fetch(item.url)
            except CircuitOpenError:
                logger.warning("Skipping {u}: circuit open", u=item.url)
                fail += 1
                # Re-raise at the run level so the run pauses.
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

            if self._upsert(db, article, result):
                new += 1
            else:
                dup += 1
        return new, dup, fail

    def _upsert(self, db: Session, article: ArticleData, result) -> bool:
        """Insert or update an article. Returns True if it was new."""
        raw = self._fetcher.compress_html(result.html) if result.html else None
        now = datetime.now(UTC)

        values = {
            "source_url": str(article.source_url),
            "title": article.title,
            "summary": article.summary,
            "full_content": article.full_content,
            "full_content_markdown": article.full_content_markdown,
            "authors": [a.model_dump() if hasattr(a, "model_dump") else a for a in article.authors] or None,
            "published_at": self._parse_dt(article.published_at),
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

        stmt = pg_insert(Article).values(**values)
        # Always overwrite mutable fields. Keep first_seen_at untouched.
        update_cols = {k: v for k, v in values.items() if k not in {"source_url", "first_seen_at"}}
        stmt = stmt.on_conflict_do_update(
            index_elements=[Article.source_url],
            set_=update_cols,
        )
        result_proxy = db.execute(stmt)
        db.commit()
        # Heuristic: PG doesn't return rowcount for ON CONFLICT reliably
        # across versions, so we look it up post-insert to count this run.
        # `rowcount` is on CursorResult, not the dialect-specific Result.
        return bool(getattr(result_proxy, "rowcount", 0))

    @staticmethod
    def _parse_dt(value) -> datetime | None:
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
