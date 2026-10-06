"""Crawl4AI-based fetch layer with stealth and rate-limit integration.

All Yahoo fetches go through `Fetcher.fetch()` so the rate limiter,
jitter, UA rotation, and circuit breaker are enforced uniformly.
"""
from __future__ import annotations

import asyncio
import gzip
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.core.config import Settings, get_settings
from app.scraper.rate_limiter import (
    BlockedResponseError,
    CircuitOpenError,
    ResilientRateLimiter,
)


@dataclass
class FetchResult:
    url: str
    status: int
    html: str
    raw_markdown: str
    cleaned_html: str
    crawl_result: Any  # full CrawlResult for advanced downstream use
    user_agent: str
    via: str  # "crawl4ai" | "fallback"


class Fetcher:
    """Wraps Crawl4AI with rate limiting, UA rotation, and stealth options."""

    def __init__(
        self,
        limiter: ResilientRateLimiter | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._limiter = limiter or ResilientRateLimiter(self._settings)
        # The proxy provider hook — plug in a residential rotator later.
        self._proxy_provider: Callable[[], str | None] | None = None
        self._crawl4ai_available = self._probe_crawl4ai()

    # ---- public API ----------------------------------------------------
    def set_proxy_provider(self, provider: Callable[[], str | None]) -> None:
        self._proxy_provider = provider

    @property
    def limiter(self) -> ResilientRateLimiter:
        return self._limiter

    def rotate_user_agent(self) -> str:
        if self._settings.user_agent:
            return self._settings.user_agent
        return random.choice(self._settings.user_agents)

    def random_viewport(self) -> tuple[int, int]:
        w = random.randint(
            self._settings.crawl4ai_viewport_min_w, self._settings.crawl4ai_viewport_max_w
        )
        h = random.randint(
            self._settings.crawl4ai_viewport_min_h, self._settings.crawl4ai_viewport_max_h
        )
        return w, h

    async def fetch(
        self,
        url: str,
        *,
        scroll: bool = False,
        wait_for: str | None = None,
        max_scrolls: int | None = None,
        timeout: int | None = None,
    ) -> FetchResult:
        """Fetch a URL with stealth + rate limit + retries.

        Raises:
            CircuitOpenError: domain in cooldown
            BlockedResponseError: persistent 403/429/CAPTCHA after retries
        """
        if not await self._limiter.is_allowed(url):
            logger.warning("robots.txt disallows {u} — skipping (respect_robots_txt=True)", u=url)
            raise PermissionError(f"robots.txt disallows {url}")

        # Retry with exponential backoff. NOTE: circuit-open errors are NOT retried
        # — they are surfaced so the run-level loop can pause.
        max_attempts = self._settings.max_retry_attempts
        base = self._settings.backoff_base_sec
        cap = self._settings.backoff_max_sec
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                return await self._do_fetch(url, scroll=scroll, wait_for=wait_for,
                                             max_scrolls=max_scrolls, timeout=timeout)
            except CircuitOpenError:
                raise  # don't retry; let the run-level loop decide
            except Exception as e:
                last_exc = e
                backoff = min(cap, base * (2 ** (attempt - 1)))
                logger.warning(
                    "Fetch attempt {a}/{m} for {u} failed: {e!r} — backing off {b:.1f}s",
                    a=attempt, m=max_attempts, u=url, e=e, b=backoff,
                )
                await asyncio.sleep(backoff)

        raise BlockedResponseError(
            f"Persistent failure fetching {url}: {last_exc!r}"
        )

    # ---- internals -----------------------------------------------------
    def _probe_crawl4ai(self) -> bool:
        try:
            import crawl4ai  # noqa: F401

            return True
        except Exception:
            logger.warning("Crawl4AI not installed — fetcher will use HTTP fallback")
            return False

    async def _do_fetch(
        self,
        url: str,
        *,
        scroll: bool,
        wait_for: str | None,
        max_scrolls: int | None,
        timeout: int | None,
    ) -> FetchResult:
        ua = self.rotate_user_agent()
        viewport_w, viewport_h = self.random_viewport()

        if self._crawl4ai_available:
            return await self._fetch_crawl4ai(
                url,
                user_agent=ua,
                viewport=(viewport_w, viewport_h),
                scroll=scroll,
                wait_for=wait_for,
                max_scrolls=max_scrolls,
                timeout=timeout,
            )
        return await self._fetch_http_fallback(url, user_agent=ua)

    async def _fetch_crawl4ai(
        self,
        url: str,
        *,
        user_agent: str,
        viewport: tuple[int, int],
        scroll: bool,
        wait_for: str | None,
        max_scrolls: int | None,
        timeout: int | None,
    ) -> FetchResult:
        # Import lazily so missing optional dep doesn't break tests.
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
        from crawl4ai.async_configs import CacheMode  # type: ignore

        proxy = self._proxy_provider() if self._proxy_provider else None
        browser_cfg = BrowserConfig(
            headless=self._settings.crawl4ai_headless,
            user_agent=user_agent,
            viewport={"width": viewport[0], "height": viewport[1]},
            proxy=proxy,
        )

        js_code = None
        if scroll:
            js_code = self._build_scroll_js(
                max_scrolls or self._settings.crawl4ai_max_scroll_count
            )

        cache_mode = (
            CacheMode.ENABLED if self._settings.crawl4ai_cache_enabled else CacheMode.DISABLED
        )
        run_cfg = CrawlerRunConfig(
            cache_mode=cache_mode,
            js_code=js_code,
            wait_for=wait_for or self._settings.crawl4ai_wait_for,
            page_timeout=(timeout or self._settings.crawl4ai_page_timeout_sec) * 1000,
            simulate_user=self._settings.crawl4ai_simulate_user,
            magic=self._settings.crawl4ai_magic,
        )

        async def _run() -> FetchResult:
            async with AsyncWebCrawler(config=browser_cfg) as crawler:
                result = await crawler.arun(url=url, config=run_cfg)
            html = result.html or ""
            status = getattr(result, "status_code", 200) or 200
            await self._limiter.report_response(url, status=status, html=html)
            if not result.success:
                raise BlockedResponseError(
                    f"Crawl4AI reported failure for {url}: {result.error_message}"
                )
            return FetchResult(
                url=url,
                status=status,
                html=html,
                raw_markdown=getattr(result, "markdown", "") or "",
                cleaned_html=getattr(result, "cleaned_html", "") or "",
                crawl_result=result,
                user_agent=user_agent,
                via="crawl4ai",
            )

        return await self._limiter.call(_run, url)

    async def _fetch_http_fallback(self, url: str, *, user_agent: str) -> FetchResult:
        """Pure-HTTP fallback when Crawl4AI is unavailable (e.g. unit tests)."""
        import httpx

        async def _run() -> FetchResult:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=self._settings.crawl4ai_page_timeout_sec,
                headers={"User-Agent": user_agent},
            ) as client:
                resp = await client.get(url)
                html = resp.text
                await self._limiter.report_response(
                    url, status=resp.status_code, html=html
                )
                if resp.status_code in (403, 429):
                    raise BlockedResponseError(f"{resp.status_code} for {url}")
            return FetchResult(
                url=url,
                status=resp.status_code,
                html=html,
                raw_markdown="",
                cleaned_html="",
                crawl_result=None,
                user_agent=user_agent,
                via="fallback",
            )

        return await self._limiter.call(_run, url)

    def _build_scroll_js(self, max_scrolls: int) -> str:
        return f"""
        (async () => {{
            const max = {max_scrolls};
            const pause = {int(self._settings.crawl4ai_scroll_pause_sec * 1000)};
            let lastCount = 0;
            let sameCount = 0;
            for (let i = 0; i < max; i++) {{
                window.scrollTo(0, document.body.scrollHeight);
                await new Promise(r => setTimeout(r, pause));
                const count = document.querySelectorAll('li.js-stream-content').length;
                if (count === lastCount) {{
                    sameCount += 1;
                    if (sameCount >= 3) break;
                }} else {{
                    sameCount = 0;
                    lastCount = count;
                }}
            }}
        }})();
        """

    @staticmethod
    def compress_html(html: str) -> bytes:
        return gzip.compress(html.encode("utf-8"))

    @staticmethod
    def decompress_html(blob: bytes) -> str:
        return gzip.decompress(blob).decode("utf-8")
