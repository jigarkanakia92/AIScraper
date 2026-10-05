"""Selector-refresh helper.

When Yahoo Finance changes their DOM, you don't need to touch any
Python code. Run:

    docker compose exec scraper-app python -m app.scraper.tools.refresh_selectors

It will:
  1. Fetch the topic page (uses production fetcher + stealth + rate limit)
  2. Save the HTML to /app/app/scraper/config/_samples/
  3. Print a diff report of which selectors now match vs don't
  4. Tell you which fields in `selectors.yaml` need updating

This module deliberately lives outside the hot path so the production
loop stays simple.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.scraper.fetcher import Fetcher  # noqa: E402


async def main() -> None:
    configure_logging()
    settings = get_settings()
    fetcher = Fetcher()
    sample_dir = Path(settings.extraction_config_dir) / "_samples"
    sample_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Fetching {u} with stealth enabled…", u=settings.target_url)
    result = await fetcher.fetch(
        settings.target_url,
        scroll=True,
        wait_for="css:li.js-stream-content",
        max_scrolls=5,
    )
    out = sample_dir / "listing_sample.html"
    out.write_text(result.html or "")
    logger.info("Saved {n} chars to {p}", n=len(result.html or ""), p=out)

    # Quick report of common selectors.
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(result.html or "", "lxml")
        probes = {
            "stream_cards": "li.js-stream-content",
            "any_news_links": "a[href*='/news/']",
            "h3_titles": "h3",
            "og_image": "meta[property='og:image']",
            "ld_json": "script[type='application/ld+json']",
        }
        print("\n=== Selector health report ===")
        for name, sel in probes.items():
            count = len(soup.select(sel))
            print(f"  {name:20s} ({sel:50s})  matches={count}")
    except Exception as e:
        logger.error("Could not probe selectors: {e}", e=e)

    print(
        "\nTo refresh selectors, open",
        out,
        "and update app/scraper/config/selectors.yaml + crawl4ai_schema.json.",
    )


if __name__ == "__main__":
    asyncio.run(main())
