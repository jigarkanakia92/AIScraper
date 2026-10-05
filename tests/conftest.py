"""Test configuration & shared fixtures."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make `app.*` importable when pytest is run from project root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Use a test-friendly env BEFORE importing the app.
os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("REQUESTS_PER_MINUTE", "1000")  # don't throttle tests
os.environ.setdefault("MIN_DELAY_SEC", "0")
os.environ.setdefault("MAX_DELAY_SEC", "0")
os.environ.setdefault("MAX_CONCURRENT_REQUESTS", "10")
os.environ.setdefault("RESPECT_ROBOTS_TXT", "false")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("ENABLE_LLM_EXTRACTOR", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()  # force re-read with the env we just set


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def listing_html() -> str:
    return (FIXTURES / "listing" / "sample_listing.html").read_text()


@pytest.fixture(scope="session")
def article_html() -> str:
    return (FIXTURES / "article" / "sample_article.html").read_text()
