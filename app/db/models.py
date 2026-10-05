"""SQLAlchemy 2.x ORM models.

- `articles`: persistent record for every article we've ever seen.
- `scrape_runs`: per-execution metrics for observability.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ArticleStatus(str, enum.Enum):
    SUCCESS = "success"
    PARTIAL = "partial_extraction"
    FAILED = "failed"


class ScrapeStage(str, enum.Enum):
    LISTING = "listing"
    ARTICLE = "article"


class Article(Base):
    """One row per unique article (deduplicated by source_url)."""

    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("source_url", name="uq_articles_source_url"),
        Index("ix_articles_published_at", "published_at"),
        Index("ix_articles_category", "category"),
        Index("ix_articles_scraped_at", "scraped_at"),
        Index("ix_articles_status", "status"),
        Index(
            "ix_articles_title_published_at",
            "title",
            "published_at",
            postgresql_where=text("title IS NOT NULL AND published_at IS NOT NULL"),
        ),
        Index("ix_articles_related_tickers", "related_tickers", postgresql_using="gin"),
        Index("ix_articles_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)

    # Core content
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_content_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Author(s) — JSON list of {"name": str, "url": str?}
    authors: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)

    # Timestamps (timezone-aware)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Classification
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    related_tickers: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    source_publisher: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Media
    top_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Provenance
    extraction_tier_used: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[ArticleStatus] = mapped_column(
        SAEnum(ArticleStatus, name="article_status"),
        nullable=False,
        default=ArticleStatus.SUCCESS,
    )

    # Raw HTML (gzipped bytes) — stored for offline reprocessing
    raw_html_compressed: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # Full-text search vector
    search_vector: Mapped[Any | None] = mapped_column(TSVECTOR, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Article id={self.id} url={self.source_url!r} status={self.status}>"


class ScrapeRun(Base):
    """One row per pipeline execution (or per-stage within an execution)."""

    __tablename__ = "scrape_runs"
    __table_args__ = (
        Index("ix_scrape_runs_started_at", "started_at"),
        Index("ix_scrape_runs_stage", "stage"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_uuid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    stage: Mapped[ScrapeStage] = mapped_column(
        SAEnum(ScrapeStage, name="scrape_stage"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_articles: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_articles: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_articles: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rate_limit_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cooldown_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    errors: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    extra_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ScrapeRun {self.run_uuid} stage={self.stage}>"
