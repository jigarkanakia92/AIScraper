"""initial schema: articles + scrape_runs

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-05 00:00:00

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "articles",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("full_content", sa.Text(), nullable=True),
        sa.Column("full_content_markdown", sa.Text(), nullable=True),
        sa.Column("authors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "scraped_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("related_tickers", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("source_publisher", sa.String(length=256), nullable=True),
        sa.Column("top_image_url", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column(
            "extraction_tier_used", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "status",
            sa.Enum(
                "success",
                "partial_extraction",
                "failed",
                name="article_status",
                create_type=True,
            ),
            nullable=False,
            server_default="success",
        ),
        sa.Column("raw_html_compressed", sa.LargeBinary(), nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.UniqueConstraint("source_url", name="uq_articles_source_url"),
    )
    op.create_index("ix_articles_published_at", "articles", ["published_at"])
    op.create_index("ix_articles_category", "articles", ["category"])
    op.create_index("ix_articles_scraped_at", "articles", ["scraped_at"])
    op.create_index("ix_articles_status", "articles", ["status"])
    op.create_index(
        "ix_articles_related_tickers",
        "articles",
        ["related_tickers"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_articles_search_vector",
        "articles",
        ["search_vector"],
        postgresql_using="gin",
    )

    op.create_table(
        "scrape_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_uuid", sa.String(length=64), nullable=False),
        sa.Column(
            "stage",
            sa.Enum(
                "listing", "article", name="scrape_stage", create_type=True
            ),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pages_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_articles", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_articles", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_articles", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rate_limit_hits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cooldown_events", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("extra_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.UniqueConstraint("run_uuid", name="uq_scrape_runs_run_uuid"),
    )
    op.create_index("ix_scrape_runs_started_at", "scrape_runs", ["started_at"])
    op.create_index("ix_scrape_runs_stage", "scrape_runs", ["stage"])


def downgrade() -> None:
    op.drop_index("ix_scrape_runs_stage", table_name="scrape_runs")
    op.drop_index("ix_scrape_runs_started_at", table_name="scrape_runs")
    op.drop_table("scrape_runs")
    op.execute("DROP TYPE IF EXISTS scrape_stage")

    op.drop_index("ix_articles_search_vector", table_name="articles")
    op.drop_index("ix_articles_related_tickers", table_name="articles")
    op.drop_index("ix_articles_status", table_name="articles")
    op.drop_index("ix_articles_scraped_at", table_name="articles")
    op.drop_index("ix_articles_category", table_name="articles")
    op.drop_index("ix_articles_published_at", table_name="articles")
    op.drop_table("articles")
    op.execute("DROP TYPE IF EXISTS article_status")
