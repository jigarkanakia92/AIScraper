"""Application configuration loaded from environment variables.

All rate-limit, stealth, and connection parameters live here. Nothing in the
scraper logic should read os.environ directly — go through Settings so we have
a single, testable source of truth.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Strongly-typed application settings.

    All defaults are CONSERVATIVE — favor not getting blocked over speed.
    """

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ App
    app_env: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = False  # if True, emit structured JSON logs
    app_timezone: str = "UTC"

    # -------------------------------------------------------------- Database
    db_host: str = "postgres-db"
    db_port: int = 5432
    db_name: str = "aiscraper"
    db_user: str = "aiscraper"
    db_password: str = "aiscraper"  # overridden in production via .env
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def alembic_url(self) -> str:
        # Alembic prefers a sync URL
        return self.database_url

    # ----------------------------------------------------------- Target site
    target_url: str = "https://finance.yahoo.com/topic/stock-market-news/"
    article_url_pattern: str = r"^https?://finance\.yahoo\.com/news/.*\.html$"
    user_agents: list[str] = Field(
        default_factory=lambda: [
            # Realistic, current desktop Chrome / Firefox / Edge UAs.
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
            "Gecko/20100101 Firefox/125.0",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
        ]
    )

    # ---------------------------------------------------- Rate limit / stealth
    requests_per_minute: int = 8            # combined across both stages
    min_delay_sec: float = 1.5              # jitter floor
    max_delay_sec: float = 4.0              # jitter ceiling
    max_concurrent_requests: int = 2        # in-flight cap
    max_retry_attempts: int = 4
    backoff_base_sec: float = 2.0
    backoff_max_sec: float = 60.0
    failure_threshold: int = 3              # 403/429 streak before cooldown
    failure_cooldown_sec: int = 300         # 5 min cool-down after a streak
    respect_robots_txt: bool = True
    user_agent: str = ""                    # if empty, rotate from pool

    # -------------------------------------------------------- Crawl4AI options
    crawl4ai_max_scroll_count: int = 25
    crawl4ai_scroll_pause_sec: float = 1.2
    crawl4ai_page_timeout_sec: int = 45
    crawl4ai_cache_enabled: bool = True
    crawl4ai_simulate_user: bool = True
    crawl4ai_magic: bool = True             # anti-bot magic mode
    crawl4ai_wait_for: str = (
        "css:li.js-stream-content"          # wait for first card before scrolling
    )
    crawl4ai_viewport_min_w: int = 1280
    crawl4ai_viewport_min_h: int = 800
    crawl4ai_viewport_max_w: int = 1920
    crawl4ai_viewport_max_h: int = 1080
    crawl4ai_headless: bool = True

    # ---------------------------------------------------------- LLM (Tier 8)
    enable_llm_extractor: bool = False
    llm_provider: Literal["openai", "anthropic", "ollama"] = "openai"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_ollama_base_url: str = "http://localhost:11434"

    # ---------------------------------------------------------- Scheduler
    scheduler_enabled: bool = True
    scheduler_interval_minutes: int = 30
    scheduler_run_on_start: bool = True
    recheck_window_hours: int = 12          # re-scrape an article if older than this
    force_refresh: bool = False

    # ------------------------------------------------------------- Extraction
    extraction_config_dir: str = str(PROJECT_ROOT / "app" / "scraper" / "config")
    selectors_file: str = "selectors.yaml"
    crawl4ai_schema_file: str = "crawl4ai_schema.json"

    @field_validator("min_delay_sec", "max_delay_sec")
    @classmethod
    def _validate_delays(cls, v: float) -> float:
        if v < 0:
            raise ValueError("delay values must be non-negative")
        return v

    @field_validator("max_delay_sec")
    @classmethod
    def _max_greater_than_min(cls, v: float, info) -> float:
        min_v = info.data.get("min_delay_sec")
        if min_v is not None and v < min_v:
            raise ValueError("max_delay_sec must be >= min_delay_sec")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
