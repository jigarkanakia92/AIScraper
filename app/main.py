"""Entry point: run migrations, then start the scheduler."""
from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.scraper.scheduler import ScraperScheduler  # noqa: E402


def _run_migrations() -> None:
    cfg_path = PROJECT_ROOT / "alembic.ini"
    cfg = Config(str(cfg_path))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "app" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().alembic_url)
    logger.info("Running Alembic migrations …")
    command.upgrade(cfg, "head")
    logger.info("Migrations complete")


async def _main() -> None:
    configure_logging()
    _run_migrations()
    scheduler = ScraperScheduler()
    scheduler.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, stop_event.set)

    logger.info("AIScraper is up. Press Ctrl+C to stop.")
    await stop_event.wait()
    logger.info("Shutting down…")
    scheduler.shutdown()


if __name__ == "__main__":
    import contextlib

    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(_main())
