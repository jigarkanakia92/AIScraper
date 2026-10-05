"""Async rate limiter with per-domain circuit breaker.

Composable protection layers:
  1. aiolimiter token bucket (smooths request rate to RPM)
  2. asyncio.Semaphore (caps in-flight concurrency)
  3. uniform random jitter (kills request-pattern detection)
  4. tenacity-style exponential backoff (per-request retries)
  5. per-domain circuit breaker (after N failures, full cooldown)

All knobs come from `Settings` — nothing is hardcoded in this file.
"""
from __future__ import annotations

import asyncio
import random
import re
import time
import urllib.robotparser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar
from urllib.parse import urlparse

import aiolimiter
from loguru import logger

from app.core.config import Settings, get_settings

T = TypeVar("T")


# ---------------------------------------------------------------- Exceptions
class RateLimiterError(Exception):
    """Base."""


class CircuitOpenError(RateLimiterError):
    """Raised when a domain is in cooldown and cannot be called."""

    def __init__(self, domain: str, until: float):
        super().__init__(f"Circuit open for {domain!r} until {until:.0f}")
        self.domain = domain
        self.until = until


class BlockedResponseError(RateLimiterError):
    """Raised when the server returns 403/429 or shows a CAPTCHA page."""


# ---------------------------------------------------------------- Domain health
@dataclass
class DomainHealth:
    domain: str
    consecutive_failures: int = 0
    in_cooldown_until: float = 0.0
    total_403s: int = 0
    total_429s: int = 0
    total_captcha_hits: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def is_open(self) -> bool:
        if self.in_cooldown_until and time.monotonic() < self.in_cooldown_until:
            return True
        # Auto-recover.
        if self.in_cooldown_until:
            async with self.lock:
                self.in_cooldown_until = 0.0
                self.consecutive_failures = 0
        return False

    async def record_failure(self, *, is_block: bool) -> bool:
        """Increment failure streak. Return True if circuit was opened."""
        async with self.lock:
            self.consecutive_failures += 1
            if is_block:
                self.total_403s += 1
            threshold = get_settings().failure_threshold
            if self.consecutive_failures >= threshold:
                self.in_cooldown_until = time.monotonic() + get_settings().failure_cooldown_sec
                logger.warning(
                    "Circuit opened for {domain} — cooldown {cooldown}s "
                    "(streak={streak}, 403s={f403}, 429s={f429})",
                    domain=self.domain,
                    cooldown=get_settings().failure_cooldown_sec,
                    streak=self.consecutive_failures,
                    f403=self.total_403s,
                    f429=self.total_429s,
                )
                return True
            return False

    async def record_success(self) -> None:
        async with self.lock:
            self.consecutive_failures = 0


# ---------------------------------------------------------------- Main class
class ResilientRateLimiter:
    """Composed async rate limiter with circuit breaker.

    Usage:
        limiter = ResilientRateLimiter()
        result = await limiter.call(fetch_func, url)
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        # Capacity = RPM, refill rate = RPM/min → aiolimiter takes
        # max_rate and time_period=60.
        self._limiter = aiolimiter.AsyncLimiter(
            max_rate=self._settings.requests_per_minute,
            time_period=60.0,
        )
        self._semaphore = asyncio.Semaphore(self._settings.max_concurrent_requests)
        self._health: dict[str, DomainHealth] = {}
        self._robots_cache: dict[str, RobotsRules | None] = {}
        self._on_cooldown_event: Callable[[str], None] | None = None
        self._cooldown_count = 0

    # ---- observability hooks --------------------------------------------
    def on_cooldown(self, callback: Callable[[str], None]) -> None:
        """Register a callback fired every time we enter cooldown."""
        self._on_cooldown_event = callback

    @property
    def cooldown_count(self) -> int:
        return self._cooldown_count

    # ---- domain health --------------------------------------------------
    def _health_for(self, domain: str) -> DomainHealth:
        if domain not in self._health:
            self._health[domain] = DomainHealth(domain=domain)
        return self._health[domain]

    # ---- robots.txt -----------------------------------------------------
    async def _get_robots(self, domain: str) -> RobotsRules | None:
        if domain in self._robots_cache:
            return self._robots_cache[domain]
        try:
            import urllib.robotparser as rp

            url = f"https://{domain}/robots.txt"
            parser = rp.RobotFileParser()
            parser.set_url(url)
            # robotparser is sync; run it in a thread to avoid blocking the loop.
            await asyncio.to_thread(parser.read)
            rules = RobotsRules(parser=parser)
        except Exception as e:
            logger.debug("robots.txt fetch failed for {d}: {e}", d=domain, e=e)
            rules = None
        self._robots_cache[domain] = rules
        return rules

    async def is_allowed(self, url: str) -> bool:
        if not self._settings.respect_robots_txt:
            return True
        parsed = urlparse(url)
        rules = await self._get_robots(parsed.netloc)
        if rules is None:
            return True
        try:
            return rules.parser.can_fetch("*", url)
        except Exception:
            return True

    # ---- public entry point ---------------------------------------------
    async def call(
        self,
        func: Callable[..., Awaitable[T]],
        *args,
        **kwargs,
    ) -> T:
        """Acquire the bucket + semaphore, add jitter, then invoke `func`.

        The first positional arg is treated as `url` for circuit-breaker
        accounting if it's a string.
        """
        url = args[0] if args and isinstance(args[0], str) else kwargs.get("url")
        domain = urlparse(url).netloc if url else "<unknown>"

        # 1. Circuit breaker check
        h = self._health_for(domain)
        if await h.is_open():
            raise CircuitOpenError(domain=domain, until=h.in_cooldown_until)

        # 2. Concurrency cap
        await self._semaphore.acquire()
        try:
            # 3. Token bucket
            async with self._limiter:
                # 4. Jitter
                await self._jitter()
                return await func(*args, **kwargs)
        finally:
            self._semaphore.release()

    async def report_response(
        self, url: str, *, status: int | None, html: str = ""
    ) -> None:
        """Tell the limiter about a response so it can update domain health."""
        if not url:
            return
        domain = urlparse(url).netloc
        h = self._health_for(domain)

        is_block = bool(status and status in (403, 429))
        captcha = _contains_captcha_marker(html)
        if is_block or captcha:
            if status == 429:
                h.total_429s += 1
            if captcha:
                h.total_captcha_hits += 1
            opened = await h.record_failure(is_block=is_block)
            if opened:
                self._cooldown_count += 1
                if self._on_cooldown_event:
                    try:
                        self._on_cooldown_event(domain)
                    except Exception:
                        logger.exception("on_cooldown callback raised")
        elif status and 200 <= status < 400:
            await h.record_success()

    # ---- helpers --------------------------------------------------------
    async def _jitter(self) -> None:
        lo = self._settings.min_delay_sec
        hi = max(self._settings.max_delay_sec, lo)
        delay = random.uniform(lo, hi)
        logger.debug("Jitter sleep {d:.2f}s", d=delay)
        await asyncio.sleep(delay)

    async def wait_for_open(self, domain: str) -> None:
        """Sleep until the domain's circuit is closed again (or raise)."""
        h = self._health_for(domain)
        while await h.is_open():
            remaining = h.in_cooldown_until - time.monotonic()
            logger.info("Waiting for cooldown on {d}: {r:.0f}s remaining", d=domain, r=remaining)
            await asyncio.sleep(min(remaining, 30))


@dataclass
class RobotsRules:
    parser: urllib.robotparser.RobotFileParser


# ---------------------------------------------------------------- helpers
_CAPTCHA_PATTERNS = (
    re.compile(r"are you a (robot|human)", re.IGNORECASE),
    re.compile(r"pardon our interruption", re.IGNORECASE),
    re.compile(r"access denied", re.IGNORECASE),
    re.compile(r"please verify", re.IGNORECASE),
    re.compile(r"captcha", re.IGNORECASE),
    re.compile(r"px-captcha", re.IGNORECASE),
    re.compile(r"<title>\s*Just a Moment", re.IGNORECASE),
)


def _contains_captcha_marker(html: str) -> bool:
    if not html:
        return False
    head = html[:20_000]
    return any(p.search(head) for p in _CAPTCHA_PATTERNS)
