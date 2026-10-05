"""Offline unit tests for the rate limiter and circuit breaker."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.core.config import get_settings
from app.scraper.rate_limiter import (
    CircuitOpenError,
    ResilientRateLimiter,
)


@pytest.fixture(autouse=True)
def _fast_jitter():
    s = get_settings()
    s.min_delay_sec = 0
    s.max_delay_sec = 0
    s.requests_per_minute = 1000
    s.max_concurrent_requests = 50
    s.failure_threshold = 3
    s.failure_cooldown_sec = 1
    yield


async def _noop(url: str = "https://example.com/"):
    return "ok"


@pytest.mark.asyncio
async def test_limiter_throttles_burst():
    """Acquire 50 tokens back-to-back and ensure total time is non-trivial."""
    s = get_settings()
    s.min_delay_sec = 0.05
    s.max_delay_sec = 0.05
    s.requests_per_minute = 1000
    limiter = ResilientRateLimiter(s)
    start = time.monotonic()
    for _ in range(5):
        await limiter.call(_noop)
    elapsed = time.monotonic() - start
    # 5 * 50ms = 250ms minimum
    assert elapsed >= 0.2


@pytest.mark.asyncio
async def test_circuit_opens_after_threshold():
    s = get_settings()
    s.failure_threshold = 3
    s.failure_cooldown_sec = 5
    limiter = ResilientRateLimiter(s)
    url = "https://blocked.example.com/path"
    for _ in range(s.failure_threshold):
        await limiter.report_response(url, status=403, html="<html></html>")
    h = limiter._health_for("blocked.example.com")
    assert await h.is_open() is True
    # Next call should raise
    with pytest.raises(CircuitOpenError):
        await limiter.call(_noop, url)


@pytest.mark.asyncio
async def test_captcha_marker_opens_circuit():
    s = get_settings()
    s.failure_threshold = 2
    limiter = ResilientRateLimiter(s)
    url = "https://captcha.example.com/"
    await limiter.report_response(url, status=200, html="<title>Just a Moment…</title>")
    await limiter.report_response(url, status=200, html="Please verify you are a human")
    with pytest.raises(CircuitOpenError):
        await limiter.call(_noop, url)


@pytest.mark.asyncio
async def test_cooldown_auto_releases():
    s = get_settings()
    s.failure_threshold = 1
    s.failure_cooldown_sec = 0.1
    limiter = ResilientRateLimiter(s)
    url = "https://quick.example.com/"
    await limiter.report_response(url, status=429, html="")
    with pytest.raises(CircuitOpenError):
        await limiter.call(_noop, url)
    await asyncio.sleep(0.2)
    # Should now succeed
    result = await limiter.call(_noop, url)
    assert result == "ok"


@pytest.mark.asyncio
async def test_robots_disallowed_blocks_call():
    s = get_settings()
    s.respect_robots_txt = True
    limiter = ResilientRateLimiter(s)

    # Force robots cache to a parser that disallows /admin for everyone.
    import urllib.robotparser as rp

    parser = rp.RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /admin"])
    parser.modified()  # ensure internal state is consistent

    from app.scraper.rate_limiter import RobotsRules

    limiter._robots_cache["example.com"] = RobotsRules(parser=parser)

    # The limiter exposes is_allowed() which the Fetcher consults.
    assert await limiter.is_allowed("https://example.com/admin") is False
    assert await limiter.is_allowed("https://example.com/index.html") is True

    # Sanity: the parser itself agrees
    assert parser.can_fetch("*", "https://example.com/admin") is False
    assert parser.can_fetch("*", "https://example.com/index.html") is True

    # Now exercise the Fetcher path (where PermissionError is raised).
    from app.scraper.fetcher import Fetcher

    fetcher = Fetcher(limiter=limiter, settings=s)
    with pytest.raises(PermissionError):
        await fetcher.fetch("https://example.com/admin")
    # Allowed path should pass through (we expect a network error in unit env,
    # but NOT PermissionError).
    try:
        await fetcher.fetch("https://example.com/index.html")
    except PermissionError:
        pytest.fail("PermissionError raised for allowed URL")
    except Exception:
        pass  # any other error (network, etc.) is fine in unit test
