"""Tier 2 — JSON-LD structured data (schema.org/NewsArticle).

Parses every <script type="application/ld+json"> block, looks for a
NewsArticle (or compatible) object, and maps it to ArticleData.

This is the most-reliable field source when present; structured data
is typically less volatile than DOM classes.
"""
from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from app.scraper.article_data import ArticleData
from app.scraper.extractors.base import BaseExtractor

_SCRIPT_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_NEWSARTICLE_TYPES = {
    "NewsArticle",
    "Article",
    "ReportageNewsArticle",
    "AnalysisNewsArticle",
    "OpinionNewsArticle",
    "ReviewNewsArticle",
}


def _walk(obj: Any) -> Any:
    """Some sites put the article under @graph; find it recursively."""
    if isinstance(obj, dict):
        t = obj.get("@type")
        if isinstance(t, str) and t in _NEWSARTICLE_TYPES:
            return obj
        if isinstance(t, list) and any(x in _NEWSARTICLE_TYPES for x in t):
            return obj
        for v in obj.values():
            found = _walk(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _walk(v)
            if found is not None:
                return found
    return None


class JsonLdExtractor(BaseExtractor):
    name = "jsonld"

    async def extract(self, url: str, payload: Any) -> ArticleData | None:
        html = self._html_from(payload)
        if not html:
            return None
        try:
            matches = _SCRIPT_RE.findall(html)
        except Exception as e:
            logger.warning("jsonld regex failed: {e}", e=e)
            return None
        if not matches:
            return None
        for raw in matches:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # Sometimes there's a stray // comment or HTML inside;
                # try stripping and re-parsing.
                try:
                    cleaned = re.sub(r"//.*?\n", "\n", raw)
                    data = json.loads(cleaned)
                except Exception:
                    continue
            node = _walk(data)
            if not node:
                continue
            return self._from_node(url, node)
        logger.warning("jsonld: no NewsArticle node found in {n} script blocks", n=len(matches))
        return None

    # --- helpers --------------------------------------------------------
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

    def _from_node(self, url: str, node: dict[str, Any]) -> ArticleData:
        article = self._empty_article(url)
        article.title = self._safe_text(node.get("headline"))
        article.summary = self._safe_text(node.get("description"))
        published_raw = self._parse_iso(node.get("datePublished"))
        if published_raw:
            article.published_at = published_raw
        updated_raw = self._parse_iso(node.get("dateModified"))
        if updated_raw:
            article.updated_at = updated_raw
        article.language = self._safe_text(node.get("inLanguage"))
        article.category = self._safe_text(node.get("articleSection"))

        # Author(s)
        authors_raw = node.get("author") or node.get("creator")
        if isinstance(authors_raw, dict):
            authors_raw = [authors_raw]
        if isinstance(authors_raw, list):
            for a in authors_raw:
                if isinstance(a, dict):
                    name = a.get("name") or a.get("givenName")
                    url_v = a.get("url")
                    if name:
                        article.authors.append({"name": str(name), "url": url_v})
                elif isinstance(a, str):
                    article.authors.append({"name": a})

        # Image
        img = node.get("image")
        if isinstance(img, list) and img:
            img = img[0]
        if isinstance(img, dict):
            img = img.get("url")
        if isinstance(img, str):
            article.top_image_url = img

        # Publisher
        pub = node.get("publisher")
        if isinstance(pub, dict) and pub.get("name"):
            article.source_publisher = pub["name"]

        # Keywords
        kw = node.get("keywords")
        if isinstance(kw, str):
            article.tags = [k.strip() for k in kw.split(",") if k.strip()]
        elif isinstance(kw, list):
            article.tags = [str(k) for k in kw]

        # Tickers — sometimes embedded in `about` or in `keywords`
        about = node.get("about")
        if isinstance(about, list):
            for a in about:
                if isinstance(a, dict) and a.get("name"):
                    article.related_tickers.append(str(a["name"]).upper())
        return article

    @staticmethod
    def _parse_iso(s: Any) -> str | None:
        if not s or not isinstance(s, str):
            return None
        try:
            from datetime import datetime

            # Normalize trailing Z
            iso = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            return dt.isoformat()
        except Exception:
            return s
