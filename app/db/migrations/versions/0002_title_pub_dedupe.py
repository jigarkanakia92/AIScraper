"""add title+published_at dedupe index

Revision ID: 0002_title_pub_dedupe
Revises: 0001_initial
Create Date: 2026-10-05 00:00:01

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_title_pub_dedupe"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Btree index to support fast "already exists" lookups by (title, published_at).
    # We intentionally do NOT add a UNIQUE constraint here because titles can be
    # NULL or collide across publishers for legitimate different articles; dedupe
    # is enforced at the application layer with normalized matching.
    op.create_index(
        "ix_articles_title_published_at",
        "articles",
        ["title", "published_at"],
        unique=False,
        postgresql_where=sa.text("title IS NOT NULL AND published_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_articles_title_published_at", table_name="articles")
