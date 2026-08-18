"""NewsAPI.ai (Event Registry) üzerinden Reuters/Bloomberg finans haberlerine
lisanslı, resmi API erişimi.

ÖNEMLİ: Bu modül Reuters veya Bloomberg'e DOĞRUDAN istek ATMAZ ve onları
scrape etmez — her iki sitenin de robots.txt'i buna izin vermiyor (bkz.
config.yaml'daki ilgili notlar). Bunun yerine, bu kaynakları resmi olarak
lisanslayıp bir API üzerinden sunan üçüncü bir servis olan NewsAPI.ai (Event
Registry, eventregistry.org) kullanılır. `sourceUri` parametresiyle yalnızca
reuters.com ve bloomberg.com kaynaklı makaleler istenir. Detaylar ve API key
alma talimatları için README.md'ye bakın.

Ücretsiz katman ayda sınırlı sayıda istek sunduğundan, bu modül kendi
kota-korumalı önbellekleme mekanizmasını içerir:
  - Gerçek API çağrısı en fazla `min_interval_minutes` dakikada bir yapılır
    (varsayılan 120 dk); ana zamanlayıcının çalışma sıklığından bağımsızdır.
  - Aradaki çalıştırmalarda son başarılı sonuç, data/state/ altındaki bir
    JSON dosyasından döner (böylece 3 çalıştırmanın 2'sinde de boş dönmez).
  - Aylık gerçek çağrı sayısı `monthly_call_quota` ile sınırlanır; aşılırsa
    kaynak sessizce (loglayarak) önbelleğe döner, hata fırlatmaz.

`eventregistry` paketi kurulu değilse veya EVENTREGISTRY_API_KEY tanımlı
değilse, bu fetcher exception fırlatmaz — durumu loglar ve boş liste döner
(diğer tüm fetcher'larla aynı hata izolasyonu mantığı, gereksinim).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dateutil import parser as date_parser

from src.models import NewsItem

logger = logging.getLogger(__name__)

_STATE_FILENAME = "licensed_aggregator_state.json"
_DEFAULT_SOURCE_DOMAINS = ["reuters.com", "bloomberg.com"]
_DEFAULT_FALLBACK_NAME = "Reuters/Bloomberg (NewsAPI.ai)"


# --------------------------------------------------------------------------
# Kota-korumalı önbellek (state) yönetimi
# --------------------------------------------------------------------------


def _state_path(output_dir: str | Path) -> Path:
    state_dir = Path(output_dir) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / _STATE_FILENAME


def _load_state(output_dir: str | Path) -> dict[str, Any]:
    path = _state_path(output_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Lisanslı kaynak önbellek dosyası okunamadı (%s), sıfırdan başlanıyor.", exc)
        return {}


def _save_state(output_dir: str | Path, state: dict[str, Any]) -> None:
    path = _state_path(output_dir)
    try:
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        logger.warning("Lisanslı kaynak önbellek dosyası yazılamadı: %s", exc)


def _items_from_cache(cache: list[dict[str, Any]]) -> list[NewsItem]:
    items: list[NewsItem] = []
    for c in cache:
        published_at = None
        if c.get("published_at"):
            try:
                published_at = datetime.fromisoformat(c["published_at"])
            except ValueError:
                published_at = None
        items.append(
            NewsItem(
                title=c.get("title", ""),
                link=c.get("link", ""),
                source=c.get("source", _DEFAULT_FALLBACK_NAME),
                published_at=published_at,
                raw_text=c.get("raw_text", ""),
                image_url=c.get("image_url", ""),
            )
        )
    return items


def _items_to_cache(items: list[NewsItem]) -> list[dict[str, Any]]:
    return [
        {
            "title": i.title,
            "link": i.link,
            "source": i.source,
            "published_at": i.published_at.isoformat() if i.published_at else None,
            "raw_text": i.raw_text,
            "image_url": i.image_url,
        }
        for i in items
    ]


# --------------------------------------------------------------------------
# Event Registry yanıtını NewsItem'a normalize etme
# --------------------------------------------------------------------------


def _parse_article_date(article: dict[str, Any]) -> datetime | None:
    date_str = article.get("dateTimePub") or article.get("dateTime")
    if not date_str and article.get("date"):
        date_str = f"{article['date']}T{article.get('time', '00:00:00')}"
    if not date_str:
        return None
    try:
        dt = date_parser.parse(date_str)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _article_to_news_item(article: dict[str, Any]) -> NewsItem | None:
    title = (article.get("title") or "").strip()
    link = (article.get("url") or "").strip()
    if not title or not link:
        return None

    source_obj = article.get("source") or {}
    source_name = source_obj.get("title") or source_obj.get("uri") or _DEFAULT_FALLBACK_NAME

    raw_text = (article.get("body") or "").strip()
    if len(raw_text) > 2000:
        raw_text = raw_text[:2000] + "..."

    return NewsItem(
        title=title,
        link=link,
        source=source_name,
        published_at=_parse_article_date(article),
        raw_text=raw_text,
        # GERÇEK makale görseli (2026-08-18, kullanıcı isteği) - Event
        # Registry'nin kendi döndürdüğü, yayıncının (Reuters/Bloomberg/CNBC
        # vb.) makaleyle birlikte sağladığı bir URL (bkz. altındaki
        # ReturnInfo > image=True) - UYDURMA/scrape edilmiş bir görsel DEĞİL.
        image_url=(article.get("image") or "").strip(),
    )


def _fetch_from_api(
    api_key: str,
    source_domains: list[str],
    lang: str,
    max_articles: int,
    keywords: list[str] | None,
) -> list[NewsItem]:
    try:
        from eventregistry import (  # type: ignore
            ArticleInfoFlags,
            EventRegistry,
            QueryArticlesIter,
            QueryItems,
            ReturnInfo,
        )
    except ImportError:
        logger.warning(
            "'eventregistry' paketi kurulu değil (requirements.txt'te var; "
            "`pip install -r requirements.txt` çalıştırın). Lisanslı "
            "Reuters/Bloomberg kaynağı atlanıyor."
        )
        return []

    try:
        er = EventRegistry(apiKey=api_key, allowUseOfArchive=False, verboseOutput=False)

        query_kwargs: dict[str, Any] = {
            "sourceUri": QueryItems.OR(source_domains),
            "lang": lang,
        }
        if keywords:
            query_kwargs["keywords"] = QueryItems.OR(keywords)

        query = QueryArticlesIter(**query_kwargs)

        # Sadece ihtiyacımız olan alanları isteyip token/bant genişliği
        # maliyetini düşük tutuyoruz (concepts/categories/sentiment vb. bizim
        # için gerekli değil). `image=True` (2026-08-18, kullanıcı isteği):
        # dashboard hero/öne çıkan kartlarında gerçek haber görseli
        # göstermek için - bkz. _article_to_news_item.
        return_info = ReturnInfo(
            articleInfo=ArticleInfoFlags(
                bodyLen=2000,
                concepts=False,
                categories=False,
                socialScore=False,
                sentiment=False,
                location=False,
                image=True,
                links=False,
                videos=False,
            )
        )

        items: list[NewsItem] = []
        for article in query.execQuery(er, sortBy="date", maxItems=max_articles, returnInfo=return_info):
            if not isinstance(article, dict):
                continue
            if "error" in article:
                logger.error("NewsAPI.ai hata döndürdü: %s", article["error"])
                break
            item = _article_to_news_item(article)
            if item:
                items.append(item)
        return items
    except Exception as exc:  # noqa: BLE001 - kota aşımı, auth hatası, ağ hatası vb. hepsi burada izole edilir
        logger.error("NewsAPI.ai (Event Registry) isteği başarısız oldu: %s", exc)
        return []


# --------------------------------------------------------------------------
# Genel giriş noktası
# --------------------------------------------------------------------------


def fetch_licensed_aggregator(source_cfg: dict[str, Any], app_cfg: dict[str, Any]) -> list[NewsItem]:
    """Reuters/Bloomberg içeriğini resmi/lisanslı bir aracı API (NewsAPI.ai /
    Event Registry) üzerinden, kota-korumalı önbellekleme ile çeker."""
    name = source_cfg.get("name", source_cfg.get("id", "lisansli-kaynak"))
    output_dir = app_cfg.get("output_dir", "data")

    api_key = os.environ.get("EVENTREGISTRY_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            "%s: EVENTREGISTRY_API_KEY tanımlı değil, bu kaynak atlanıyor. "
            ".env dosyasına anahtarınızı ekleyin (bkz. README).",
            name,
        )
        return []

    min_interval_minutes = source_cfg.get("min_interval_minutes", 120)
    monthly_call_quota = source_cfg.get("monthly_call_quota", 200)
    max_articles = app_cfg.get("max_articles_per_source", 15)
    source_domains = source_cfg.get("source_domains") or _DEFAULT_SOURCE_DOMAINS
    keywords = source_cfg.get("keywords") or None
    lang = source_cfg.get("lang", "eng")

    state = _load_state(output_dir)
    now = datetime.now(timezone.utc)

    last_fetched_at: datetime | None = None
    if state.get("last_fetched_at"):
        try:
            last_fetched_at = datetime.fromisoformat(state["last_fetched_at"])
        except ValueError:
            last_fetched_at = None

    current_month = now.strftime("%Y-%m")
    calls_this_month = state.get("calls_this_month", 0) if state.get("quota_month") == current_month else 0

    should_call_api = (
        last_fetched_at is None
        or (now - last_fetched_at).total_seconds() >= min_interval_minutes * 60
    )

    if should_call_api and calls_this_month >= monthly_call_quota:
        logger.warning(
            "%s: aylık istek kotası (%d) doldu, gerçek API çağrısı atlanıyor, "
            "önbellekteki son sonuçlar kullanılıyor.",
            name,
            monthly_call_quota,
        )
        should_call_api = False

    if not should_call_api:
        cached_items = _items_from_cache(state.get("items", []))
        logger.info(
            "%s: kota korumak için önbellekten döndürülüyor (%d haber, son gerçek "
            "çekim: %s, sonraki gerçek çekim en erken %d dk sonra).",
            name,
            len(cached_items),
            last_fetched_at.isoformat() if last_fetched_at else "hiç",
            min_interval_minutes,
        )
        return cached_items

    items = _fetch_from_api(api_key, source_domains, lang, max_articles, keywords)

    state["last_fetched_at"] = now.isoformat()
    state["quota_month"] = current_month
    state["calls_this_month"] = calls_this_month + 1
    state["items"] = _items_to_cache(items)
    _save_state(output_dir, state)

    logger.info(
        "%s: %d haber çekildi (NewsAPI.ai, bu ay gerçek çağrı #%d/%d)",
        name,
        len(items),
        calls_this_month + 1,
        monthly_call_quota,
    )
    return items
