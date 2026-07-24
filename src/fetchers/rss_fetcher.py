"""RSS/Atom feed'lerinden haber çeken fetcher.

Öncelik gereksinimimiz gereği (RSS varsa RSS'e öncelik ver), Bloomberg HT,
CNBC-e ve Foreks gibi RSS sunan kaynaklar bu modül üzerinden çekilir.
"""

from __future__ import annotations

import logging
from calendar import timegm
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx
from bs4 import BeautifulSoup

from src.fetchers.base import is_allowed, rate_limit
from src.models import NewsItem

logger = logging.getLogger(__name__)


def _clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    return BeautifulSoup(raw_html, "html.parser").get_text(separator=" ", strip=True)


def _parse_published(entry: Any) -> datetime | None:
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return None
    try:
        return datetime.fromtimestamp(timegm(struct), tz=timezone.utc)
    except (OverflowError, ValueError):
        return None


def fetch_rss(source_cfg: dict[str, Any], app_cfg: dict[str, Any]) -> list[NewsItem]:
    """Tek bir RSS kaynağını çeker ve NewsItem listesine dönüştürür.

    Bir hata oluşursa exception fırlatmaz; boş liste döner ve hatayı loglar.
    Böylece bir kaynağın çökmesi diğerlerini etkilemez (gereksinim #7).
    """
    name = source_cfg.get("name", source_cfg.get("id", "bilinmeyen-kaynak"))
    url = source_cfg["url"]
    user_agent = app_cfg.get("user_agent", "FinansHaberToplayiciBot/1.0")
    timeout = app_cfg.get("request_timeout", 15)
    min_interval = app_cfg.get("rate_limit_seconds", 2.0)
    max_items = app_cfg.get("max_articles_per_source", 15)

    if not is_allowed(url, user_agent, timeout=timeout):
        logger.warning("%s: robots.txt bu adrese erişime izin vermiyor, atlanıyor (%s)", name, url)
        return []

    rate_limit(url, min_interval)

    try:
        response = httpx.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.error("%s: RSS isteği başarısız oldu: %s", name, exc)
        return []

    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        logger.error("%s: RSS içeriği ayrıştırılamadı: %s", name, parsed.get("bozo_exception"))
        return []

    items: list[NewsItem] = []
    for entry in parsed.entries[:max_items]:
        title = _clean_html(entry.get("title", "")).strip()
        link = entry.get("link", "").strip()
        if not title or not link:
            continue
        raw_text = _clean_html(entry.get("summary", "") or entry.get("description", ""))
        items.append(
            NewsItem(
                title=title,
                link=link,
                source=name,
                published_at=_parse_published(entry),
                raw_text=raw_text,
            )
        )

    logger.info("%s: %d haber çekildi (RSS)", name, len(items))
    return items
