"""Tier 7 — BeautifulSoup4 + lxml with selectors from `selectors.yaml`.

Pure-Python, no JS rendering. This is the last structural fallback
before LLM. Selectors are read from `selectors.yaml` so that when
Yahoo changes their markup, you edit YAML — not Python.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from app.core.config import get_settings
from app.scraper.article_data import ArticleData
from app.scraper.extractors.base import BaseExtractor

_TICKER_RE = re.compile(r"^\(?([A-Z]{1,5})(?:\.|\b)")


def _extract_ticker(text: str) -> str | None:
    if not text:
        return None
    m = _TICKER_RE.search(text.strip())
    if m:
        return m.group(1)
    # Fallback: take the first all-caps run 1-5 chars long
    m2 = re.search(r"\b[A-Z]{1,5}\b", text)
    return m2.group(0) if m2 else None


class CssFallbackExtractor(BaseExtractor):
    name = "css_fallback"

    def __init__(self) -> None:
        self._selectors = self._load_selectors()

    @staticmethod
    def _load_selectors() -> dict[str, Any]:
        settings = get_settings()
        path = Path(settings.extraction_config_dir) / settings.selectors_file
        if not path.exists():
            logger.error("selectors.yaml not found at {p}", p=path)
            return {}
        try:
            with path.open() as f:
                return yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            logger.error("selectors.yaml invalid YAML: {e}", e=e)
            return {}

    async def extract(self, url: str, payload: Any) -> ArticleData | None:
        html = self._html_from(payload)
        if not html:
            return None
        try:
            from bs4 import BeautifulSoup, Tag  # type: ignore
        except ImportError:
            logger.warning("bs4 not installed; CSS fallback disabled")
            return None
        soup = BeautifulSoup(html, "lxml")
        cfg = self._selectors.get("article_page", {})
        if not cfg:
            return None

        article = self._empty_article(url)
        empty_count = 0
        for field_name, sel in cfg.items():
            if field_name in {"meta_tags", "bot_detection_markers"}:
                continue
            if not isinstance(sel, dict):
                continue
            val = self._apply_selector(soup, sel, BeautifulSoup, Tag)
            if val is None or val in ("", []):
                empty_count += 1
                continue
            self._set_field(article, field_name, val)

        # If EVERY optional field is empty, treat as "site changed" warning.
        if empty_count >= max(1, len(cfg) - 1):
            logger.warning(
                "Possible site structure change: css_fallback returned "
                "{empty}/{total} empty fields for {u}",
                empty=empty_count,
                total=len(cfg),
                u=url,
            )
            return None

        return article

    @staticmethod
    def _apply_selector(soup, sel, BeautifulSoup, Tag) -> Any:
        try:
            stype = sel.get("selector", "css")
            attr = sel.get("attribute")
            multiple = bool(sel.get("multiple"))
            transform = sel.get("transform")

            if stype == "xpath":
                nodes = soup.xpath(sel["xpath"])  # type: ignore[attr-defined]
            else:
                nodes = soup.select(sel.get("css", ""))

            if not nodes:
                return None
            if not isinstance(nodes, list):
                nodes = [nodes]
            # lxml's xpath may return non-Tag nodes
            nodes = [n for n in nodes if isinstance(n, Tag)]

            def pick(n: Tag) -> Any:
                if attr == "text":
                    return n.get_text(" ", strip=True)
                if attr:
                    v = n.get(attr)
                    return v if isinstance(v, str) else None
                return n.get_text(" ", strip=True)

            values = [pick(n) for n in nodes if pick(n)]
            values = [v for v in values if v]

            if not values:
                return None
            if multiple:
                return values
            v = values[0]
            if transform == "extract_ticker":
                ticker = _extract_ticker(v)
                return [ticker] if ticker else None
            return v
        except Exception as e:
            logger.debug("selector failed: {e}", e=e)
            return None

    @staticmethod
    def _set_field(article: ArticleData, field_name: str, value: Any) -> None:
        if field_name == "title":
            article.title = str(value)
        elif field_name == "summary":
            article.summary = str(value)
        elif field_name == "full_content":
            article.full_content = str(value) if isinstance(value, str) else "\n\n".join(value)
        elif field_name == "authors":
            if isinstance(value, list):
                article.authors = [{"name": str(v)} for v in value]
            else:
                article.authors = [{"name": str(value)}]
        elif field_name in {"published_at", "updated_at"}:
            if field_name == "published_at":
                article.published_at = str(value)
            else:
                article.updated_at = str(value)
        elif field_name == "top_image_url":
            article.top_image_url = str(value)
        elif field_name == "category":
            article.category = str(value)
        elif field_name == "related_tickers":
            if isinstance(value, list):
                article.related_tickers = [
                    str(v).strip().upper() for v in value if v
                ]
        elif field_name == "source_publisher":
            article.source_publisher = str(value)
        # unknown fields are ignored — not part of ArticleData.

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
