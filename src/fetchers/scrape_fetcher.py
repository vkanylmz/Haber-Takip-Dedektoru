"""RSS sunmayan kaynaklar için genel amaçlı, robots.txt'e saygılı scraper.

Bu modül kasıtlı olarak "temkinli" tasarlanmıştır:
  - Her istekten önce robots.txt kontrol edilir; izin yoksa kaynak sessizce
    (loglayarak) atlanır. Örneğin Reuters robots.txt'i genel botlar için
    siteyi tamamen kapattığından (`Disallow: /`), bu fetcher Reuters için
    hiçbir zaman veri döndürmeyecektir - bu istenen/doğru davranıştır.
  - Alan adı başına rate-limit uygulanır.
  - CSS seçiciler config.yaml'dan okunur, böylece bir sitenin HTML yapısı
    değiştiğinde kod değiştirmeden ayar güncellenebilir.
  - render_js: true ise ve `playwright` paketi kuruluysa tarayıcı tabanlı
    render denenir; paket kurulu değilse hata fırlatmadan atlanır.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from src.fetchers.base import is_allowed, rate_limit
from src.models import NewsItem

logger = logging.getLogger(__name__)


def _fetch_html(url: str, user_agent: str, timeout: float) -> str | None:
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.text
    except Exception as exc:  # noqa: BLE001
        logger.error("HTML alınamadı (%s): %s", url, exc)
        return None


def _fetch_html_with_playwright(url: str, user_agent: str, timeout: float) -> str | None:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        logger.warning(
            "render_js=true ayarlanmış ancak 'playwright' paketi kurulu değil. "
            "Kurulum için README'ye bakın. Bu kaynak atlanıyor."
        )
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=user_agent)
            page.goto(url, timeout=int(timeout * 1000))
            html = page.content()
            browser.close()
            return html
    except Exception as exc:  # noqa: BLE001
        logger.error("Playwright ile sayfa render edilemedi (%s): %s", url, exc)
        return None


def fetch_scrape(source_cfg: dict[str, Any], app_cfg: dict[str, Any]) -> list[NewsItem]:
    name = source_cfg.get("name", source_cfg.get("id", "bilinmeyen-kaynak"))
    base_url = source_cfg["base_url"]
    user_agent = app_cfg.get("user_agent", "FinansHaberToplayiciBot/1.0")
    timeout = app_cfg.get("request_timeout", 15)
    min_interval = app_cfg.get("rate_limit_seconds", 2.0)
    max_items = app_cfg.get("max_articles_per_source", 15)

    if not is_allowed(base_url, user_agent, timeout=timeout):
        logger.warning(
            "%s: robots.txt bu kaynağın taranmasına izin vermiyor, atlanıyor (%s). "
            "Bu kasıtlı bir davranıştır (saygılı scraping gereksinimi).",
            name,
            base_url,
        )
        return []

    rate_limit(base_url, min_interval)

    if source_cfg.get("render_js"):
        html = _fetch_html_with_playwright(base_url, user_agent, timeout)
    else:
        html = _fetch_html(base_url, user_agent, timeout)

    if html is None:
        return []

    soup = BeautifulSoup(html, "html.parser")
    item_selector = source_cfg.get("item_selector", "article")
    title_selector = source_cfg.get("title_selector", "a")
    link_selector = source_cfg.get("link_selector", "a")

    items: list[NewsItem] = []
    elements = soup.select(item_selector)
    if not elements:
        logger.info(
            "%s: item_selector ('%s') ile hiçbir öğe bulunamadı. Site yapısı "
            "değişmiş veya içerik JavaScript ile render ediliyor olabilir "
            "(render_js: true deneyin).",
            name,
            item_selector,
        )
        return []

    for el in elements[:max_items]:
        title_el = el.select_one(title_selector)
        link_el = el.select_one(link_selector)
        if title_el is None or link_el is None:
            continue
        title = title_el.get_text(strip=True)
        href = link_el.get("href", "")
        if not title or not href:
            continue
        link = urljoin(base_url, href)
        items.append(
            NewsItem(
                title=title,
                link=link,
                source=name,
                # Listeleme sayfalarında genelde kesin yayın saati bulunmaz;
                # bilinmiyorsa None bırakılır (main.py tarihsiz öğeleri
                # çekildikleri an itibarıyla "şimdi" kabul edip sıralar).
                published_at=None,
                raw_text="",
            )
        )

    logger.info("%s: %d haber çekildi (scrape)", name, len(items))
    return items
