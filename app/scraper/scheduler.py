"""APScheduler-based periodic scraper.

Runs Stage A + Stage B on a configurable interval, records metrics
in the `scrape_runs` table, and pauses on circuit-breaker cooldown.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import ScrapeRun, ScrapeStage

if TYPE_CHECKING:
    import apscheduler.schedulers.asyncio
from app.db.session import session_scope
from app.scraper.article_crawler import ArticleCrawler
from app.scraper.fetcher import Fetcher
from app.scraper.listing_crawler import ListingCrawler
from app.scraper.pipeline import ExtractionPipeline
from app.scraper.rate_limiter import CircuitOpenError


class ScraperScheduler:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._fetcher = Fetcher()
        self._pipeline = ExtractionPipeline(enable_llm=self._settings.enable_llm_extractor)
        self._listing = ListingCrawler(self._fetcher)
        self._article = ArticleCrawler(self._fetcher, self._pipeline)
        self._scheduler: apscheduler.schedulers.asyncio.AsyncIOScheduler | None = None  # type: ignore[name-defined]

    def start(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore

        if not self._settings.scheduler_enabled:
            logger.info("Scheduler disabled by config")
            return
        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            self._run_once_safely,
            "interval",
            minutes=self._settings.scheduler_interval_minutes,
            next_run_time=(
                datetime.now(UTC)
                if self._settings.scheduler_run_on_start
                else None
            ),
            id="scrape_job",
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.start()
        logger.info(
            "Scheduler started: every {m} minutes", m=self._settings.scheduler_interval_minutes
        )

    def shutdown(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler shut down")

    async def _run_once_safely(self) -> None:
        try:
            await self.run_once()
        except Exception:
            logger.exception("run_once failed")

    async def run_once(self) -> None:
        run_uuid = str(uuid.uuid4())
        logger.info("=== Run {r} start ===", r=run_uuid)
        pages = new = dup = fail = 0
        cooldown_events = 0
        errors: list[dict] = []

        with session_scope() as db:
            run_row = self._start_run(db, run_uuid, ScrapeStage.LISTING)
            try:
                items = await self._listing.crawl()
                pages += 1
                fresh = self._listing.filter_fresh(items, db)
                logger.info(
                    "Stage A → B: {n} articles queued ({d} dropped as already-fresh)",
                    n=len(fresh), d=len(items) - len(fresh),
                )
                self._end_run(db, run_row, pages=pages, new_=0, dup_=0, fail_=0,
                              cooldown=self._fetcher.limiter.cooldown_count,
                              extra={"listing_count": len(items), "fresh_count": len(fresh)},
                              errors=errors)

                # Stage B
                run_row = self._start_run(db, run_uuid, ScrapeStage.ARTICLE)
                new, dup, fail = await self._article.process_queue(fresh, db)
                self._end_run(db, run_row, pages=len(fresh), new_=new, dup_=dup, fail_=fail,
                              cooldown=self._fetcher.limiter.cooldown_count,
                              errors=errors)
            except CircuitOpenError as e:
                cooldown_events += 1
                logger.error("Run aborted: {e}", e=e)
                errors.append({"type": "circuit_open", "message": str(e)})
                self._end_run(db, run_row, pages=pages, new_=new, dup_=dup, fail_=fail,
                              cooldown=cooldown_events, errors=errors)
                return
            except Exception as e:
                logger.exception("Run failed: {e!r}", e=e)
                errors.append({"type": "exception", "message": repr(e)})
                self._end_run(db, run_row, pages=pages, new_=new, dup_=dup, fail_=fail,
                              cooldown=cooldown_events, errors=errors)
                return

        logger.info(
            "=== Run {r} end: pages={p} new={n} dup={d} fail={f} cooldowns={c} ===",
            r=run_uuid, p=pages, n=new, d=dup, f=fail, c=cooldown_events,
        )

    # ---- helpers --------------------------------------------------------
    def _start_run(self, db: Session, run_uuid: str, stage: ScrapeStage) -> ScrapeRun:
        row = ScrapeRun(run_uuid=f"{run_uuid}:{stage.value}", stage=stage)
        db.add(row)
        db.flush()
        return row

    def _end_run(
        self, db: Session, row: ScrapeRun, *, pages: int, new_: int, dup_: int,
        fail_: int, cooldown: int, errors: list[dict],
        extra: dict | None = None,
    ) -> None:
        row.ended_at = datetime.now(UTC)
        row.pages_fetched = pages
        row.new_articles = new_
        row.duplicate_articles = dup_
        row.failed_articles = fail_
        row.cooldown_events = cooldown
        row.rate_limit_hits = self._fetcher.limiter.cooldown_count
        if errors:
            row.errors = errors
        if extra:
            row.extra_metrics = extra
        db.flush()
