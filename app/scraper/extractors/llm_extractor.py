"""Tier 8 — LLM extractor (OPTIONAL, feature-flagged).

Disabled by default (`ENABLE_LLM_EXTRACTOR=false`). When enabled, it
calls an LLM provider to fill any remaining empty fields on the
ArticleData. This is the most expensive, slowest, and most fragile
tier — it exists as a true last resort.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger

from app.core.config import get_settings
from app.scraper.article_data import ArticleData
from app.scraper.extractors.base import BaseExtractor


class LlmExtractor(BaseExtractor):
    name = "llm"

    def __init__(self) -> None:
        s = get_settings()
        self._enabled = s.enable_llm_extractor
        if self._enabled and not s.llm_api_key and s.llm_provider != "ollama":
            logger.warning("LLM extractor enabled but no API key set — disabling")
            self._enabled = False

    async def extract(self, url: str, payload: Any) -> ArticleData | None:
        if not self._enabled:
            return None
        html = self._html_from(payload)
        if not html:
            return None
        settings = get_settings()
        # Cap HTML to avoid blowing token budget
        truncated = html[: settings.llm_max_html_chars] if hasattr(settings, "llm_max_html_chars") else html[:50_000]
        prompt = self._build_prompt(truncated)
        try:
            if settings.llm_provider == "openai":
                data = await self._call_openai(prompt, settings)
            elif settings.llm_provider == "anthropic":
                data = await self._call_anthropic(prompt, settings)
            elif settings.llm_provider == "ollama":
                data = await self._call_ollama(prompt, settings)
            else:
                return None
        except Exception as e:
            logger.error("LLM extractor failed: {e}", e=e)
            return None
        if not data:
            return None
        article = self._empty_article(url)
        article.title = self._safe_text(data.get("title"))
        article.summary = self._safe_text(data.get("summary"))
        article.full_content = self._safe_text(data.get("full_content"))
        if isinstance(data.get("related_tickers"), list):
            article.related_tickers = [str(t).upper() for t in data["related_tickers"] if t]
        return article

    def _build_prompt(self, html: str) -> str:
        return (
            "Extract structured information about this news article from the HTML. "
            "Return a JSON object with keys: title, summary, full_content, "
            "related_tickers (array of ticker symbols, e.g. AAPL). "
            "If a field is not present, return null. Do not invent facts.\n\n"
            f"HTML:\n```\n{html}\n```"
        )

    async def _call_openai(self, prompt: str, settings) -> dict[str, Any] | None:
        try:
            import httpx  # type: ignore
        except ImportError:
            return None
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={
                    "model": settings.llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                },
            )
            if r.status_code != 200:
                logger.error("OpenAI LLM {s}: {b}", s=r.status_code, b=r.text[:300])
                return None
            content = r.json()["choices"][0]["message"]["content"]
            return json.loads(content)

    async def _call_anthropic(self, prompt: str, settings) -> dict[str, Any] | None:
        try:
            import httpx  # type: ignore
        except ImportError:
            return None
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.llm_api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": settings.llm_model,
                    "max_tokens": 2048,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if r.status_code != 200:
                return None
            text = r.json()["content"][0]["text"]
            return json.loads(text)

    async def _call_ollama(self, prompt: str, settings) -> dict[str, Any] | None:
        try:
            import httpx  # type: ignore
        except ImportError:
            return None
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{settings.llm_ollama_base_url}/api/chat",
                json={
                    "model": settings.llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "format": "json",
                },
            )
            if r.status_code != 200:
                return None
            content = r.json()["message"]["content"]
            return json.loads(content)

    @staticmethod
    def _html_from(payload: Any) -> str | None:
        if payload is None:
            return None
        if hasattr(payload, "html"):
            return payload.html
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            return payload.get("html")
        return None
