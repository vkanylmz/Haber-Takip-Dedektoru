"""Basit bir FastAPI web dashboard'u: veritabanındaki (özetlenmiş, önem
skorlanmış) haberleri listeler, kaynağa göre filtreleme sağlar, sayfa
birkaç dakikada bir otomatik yenilenir (meta refresh — ekstra JS/websocket
gerektirmez).

Çalıştırma (proje kök dizininden, geliştirme amaçlı doğrudan):
    uvicorn src.web.app:app --reload

Normal kullanımda bu, proje kökündeki main.py tarafından worker ile birlikte
başlatılır (bkz. README > Tek Komutla Başlatma).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from src.commodity_report import get_commodity_dashboard_data, start_commodity_background_refresh
from src.company_profile import get_company_profile
from src.fetchers.webhook import DEFAULT_SOURCE_NAME, IncomingDisclosure, process_incoming_disclosure
from src.timezone_utils import format_turkey_time
from src.config import load_config
from src.db import (
    NewsRecord,
    PushSubscription,
    get_all_subscribers_overview,
    get_distinct_sectors,
    get_distinct_sources,
    get_records_by_company_ticker,
    get_records_since,
    get_recent_records,
    get_session,
    get_source_health_summary,
    init_db,
    remove_push_subscription,
    upsert_push_subscription,
)
from src.summarizer import (
    REGION_LABELS,
    SECTOR_LABELS,
    SENTIMENT_LABELS,
    TOP_CATEGORY_LABELS,
    VALID_REGIONS,
    VALID_SENTIMENTS,
    VALID_TOP_CATEGORIES,
)
from src.trend_report import get_dashboard_trend_summary
from src.web.api_v1 import router as api_v1_router
from src.web.market_data import (
    get_market_snapshot,
    get_quotes_for_company_tickers,
    get_ticker_detail,
    start_background_refresh,
)
from src.web_push import get_vapid_public_key

logger = logging.getLogger(__name__)

# Önem skoru filtresi dropdown'ında sunulan eşik seçenekleri (min. skor).
_IMPORTANCE_FILTER_OPTIONS = (3, 4, 5)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Sektör bazında ısı haritasında, yeterli sayıda haberi olmayan bir sektörü
# "az hareketli" (gri) saymak için eşik - bkz. _build_sector_heatmap.
_HEATMAP_MIN_COUNT_FOR_COLOR = 2

# get_distinct_sources/get_distinct_sectors, dropdown seçeneklerini hesaplamak
# için TÜM news_records tablosunu tarıyor (bkz. src/db.py - sources/sector
# normalize edilmiş ayrı tablolar DEĞİL, aynı satırların içinde metin/JSON
# olarak tutuluyor). Gerçek ölçümle doğrulandı (2026-07, bkz. README >
# "Dashboard Performansı"): bu iki sorgu tek başına ~280ms'ye mal oluyordu -
# HER dashboard isteğinde tekrar tekrar. Distinct kaynak/sektör listesi
# saniyeler içinde değişmez (yeni bir kaynak/sektör ancak YENİ bir haber
# türüyle ortaya çıkar) - bu yüzden 60 saniyelik basit bir TTL önbelleği,
# gözle görülür bir gecikme (en fazla 60 sn eski bir liste) pahasına, HER
# istekte bu iki tam-tablo taramasını tekrarlamayı önler.
_FILTER_OPTIONS_CACHE_TTL_SECONDS = 60.0
_filter_options_cache: dict[str, Any] = {"sources": None, "sectors": None, "fetched_at": 0.0}

# --- Ana dashboard (filtresiz varsayılan görünüm) haber listesi önbelleği ---
# GERÇEK teşhisle bulundu (2026-08, "dashboard 1.4-3.2sn'de yükleniyor"
# şikayeti soruşturulurken): yerel profilleme, config yükleme + DB sorgusu +
# Jinja render'ın TOPLAMININ bile SQLite'ta 30-140ms olduğunu gösterdi -
# yani üretimdeki 1.4-3.2sn'lik farkın kaynağı ne şablon render'ı ne de
# Python tarafı hesaplama; sektör ısı haritasındakiyle AYNI kök neden
# (Render/Oregon <-> Neon/Frankfurt kıtalararası DB round-trip, ~200ms-2sn)
# burada da geçerli - dashboard() HER istekte CANLI bir Postgres sorgusu
# yapıyordu, market-data/heatmap/ticker-quotes'un aksine önbelleksiz.
#
# Yalnızca FİLTRESİZ varsayılan görünüm (kaynak/sektör/bölge/duygu/önem/arama
# YOK, sıralama=en yeni) önbelleklenir - ziyaretçilerin BÜYÜK ÇOĞUNLUĞU bu
# görünümü görür (ilk ziyaret, yer imi, paylaşılan link). Aktif olarak
# filtre/arama uygulayan kullanıcı için önbellek atlanır, CANLI sorgu
# çalışır (o durumda "en fazla 30sn eski" bir sonuç yerine kullanıcının
# az önce uyguladığı filtrenin GERÇEKTEN yansıdığından emin olunur).
_DASHBOARD_DEFAULT_CACHE_TTL_SECONDS = 30.0
_dashboard_default_cache: dict[str, Any] = {"records": None, "fetched_at": 0.0}
_dashboard_default_lock = threading.Lock()


def _get_cached_default_records(session, max_items: int) -> list[NewsRecord]:
    """Filtresiz/varsayılan ("en yeni", filtre yok) dashboard sorgusu için
    TTL önbelleği - `_get_cached_filter_options` ile AYNI thundering-herd
    korumalı çift-kontrol kilit deseni (bkz. o fonksiyondaki not)."""
    now = time.monotonic()
    if _dashboard_default_cache["records"] is not None and (now - _dashboard_default_cache["fetched_at"]) < _DASHBOARD_DEFAULT_CACHE_TTL_SECONDS:
        return _dashboard_default_cache["records"]

    with _dashboard_default_lock:
        now = time.monotonic()
        if _dashboard_default_cache["records"] is not None and (now - _dashboard_default_cache["fetched_at"]) < _DASHBOARD_DEFAULT_CACHE_TTL_SECONDS:
            return _dashboard_default_cache["records"]
        records = get_recent_records(session, limit=max_items, sort_order="newest")
        _dashboard_default_cache["records"] = records
        _dashboard_default_cache["fetched_at"] = now
        return records


# --- Web rotaları için config önbelleği ---
# `load_config()` (bkz. src/config.py) HER çağrıda .env'i disk'ten okuyup
# config.yaml'ı (19KB) yeniden YAML-parse ediyor - yerel ölçümle bu tek
# başına ~20-30ms (bkz. yukarıdaki dashboard önbelleği notu, aynı
# soruşturma). worker/main.py/lifespan gibi diğer çağıranlar İÇİN
# `load_config()`'in kendisi BİLEREK değiştirilmedi (config.yaml çalışırken
# değişebilir, o yollarda "her zaman taze" davranışı korunmalı) - burada
# yalnızca web istek işleyicileri için 60sn'lik (filter-options önbelleğiyle
# TUTARLI) bir TTL sarmalayıcı eklendi.
_WEB_CONFIG_CACHE_TTL_SECONDS = 60.0
_web_config_cache: dict[str, Any] = {"config": None, "fetched_at": 0.0}


def _get_cached_web_config() -> dict[str, Any]:
    now = time.monotonic()
    if _web_config_cache["config"] is not None and (now - _web_config_cache["fetched_at"]) < _WEB_CONFIG_CACHE_TTL_SECONDS:
        return _web_config_cache["config"]
    config = load_config()
    _web_config_cache["config"] = config
    _web_config_cache["fetched_at"] = now
    return config


# --- Sektör ısı haritası önbelleği ---
# GERÇEK teşhisle bulundu (2026-07, çoklu kullanıcı performans soruşturması
# devamı): bu sorgunun Neon Postgres SUNUCUSUNDAKİ çalışma süresi sadece
# ~1.8ms (EXPLAIN ANALYZE ile doğrulandı, index eksikliği YOK, sorgu zaten
# hızlı) - asıl maliyet Render (Oregon) ile Neon (eu-central-1/Frankfurt)
# arasındaki KITALARARASI ağ gecikmesi (her istekte ~200ms+ round-trip,
# ölçülen toplam süre 350ms-2000ms arası değişken). Isı haritasının altında
# yatan veri worker'ın tarama döngüsüyle (dakikada bir) değişiyor, saniyede
# bir DEĞİL - bu yüzden market_data.py'deki piyasa verisiyle AYNI mimari
# uygulanıyor: kullanıcı isteğinden TAMAMEN BAĞIMSIZ bir arka plan görevi
# önbelleği proaktif tazeler, `sector_heatmap()` normalde HİÇ DB round-trip
# BEKLEMEZ. (İLK denemede basit bir reaktif TTL-cache kullanılmıştı, ama
# GERÇEK testte TTL süresi dolduğunda 2-3 eşzamanlı isteğin AYNI kilit
# bekleme sorununu - ~2 sn - yeniden yarattığı ölçüldü; bu yüzden piyasa
# verisindekiyle TUTARLI olacak şekilde proaktif modele yükseltildi.)
#
# Burada `threading` kullanılıyor (asyncio DEĞİL): _build_sector_heatmap
# senkron/bloklayan bir SQLAlchemy oturumu açıyor - market_data.py'nin
# aksine (o httpx ile native async), bu iş asyncio event loop'unu
# BLOKLARDI; bir arka plan THREAD'i bu riski taşımaz.
_SECTOR_HEATMAP_BACKGROUND_INTERVAL_SECONDS = 20.0
_sector_heatmap_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}
_sector_heatmap_lock = threading.Lock()
_sector_heatmap_background_thread: threading.Thread | None = None
_sector_heatmap_background_stop = threading.Event()


def _compute_fear_greed_index(records: list[NewsRecord]) -> int:
    """Sentiment dağılımından basit bir "Piyasa Duygusu" (Korku/Açgözlülük)
    endeksi hesaplar (0-100, 50=nötr). Hem ana dashboard'da hem "Detaylı
    İnceleme" sayfasında (bkz. src/web/app.py) AYNI hesaplama kullanılır -
    tek bir kaynak-of-truth, iki yerde tutarsızlık olmaz."""
    fg_score = 0
    fg_valid_count = 0
    for r in records[:50]:
        if r.sentiment == "pozitif":
            fg_score += 1
            fg_valid_count += 1
        elif r.sentiment == "negatif":
            fg_score -= 1
            fg_valid_count += 1
        elif r.sentiment == "notr":
            fg_valid_count += 1

    if fg_valid_count == 0:
        return 50  # varsayılan nötr

    normalized = fg_score / fg_valid_count  # -1.0 ile 1.0 arası
    return int((normalized + 1.0) / 2.0 * 100)


def _get_cached_filter_options(session) -> tuple[list[str], list[dict[str, str]]]:
    now = time.monotonic()
    if _filter_options_cache["sources"] is not None and (now - _filter_options_cache["fetched_at"]) < _FILTER_OPTIONS_CACHE_TTL_SECONDS:
        return _filter_options_cache["sources"], _filter_options_cache["sectors"]

    sources = get_distinct_sources(session)
    sectors = [{"slug": s, "label": SECTOR_LABELS.get(s, s)} for s in get_distinct_sectors(session)]
    _filter_options_cache["sources"] = sources
    _filter_options_cache["sectors"] = sectors
    _filter_options_cache["fetched_at"] = now
    return sources, sectors


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    db_path = config.get("database", {}).get("path", "data/finans_haber.db")
    init_db(db_path)
    # Piyasa verisi önbelleğini kullanıcı isteklerinden bağımsız arka planda
    # proaktif tutan görevi başlat (bkz. src/web/market_data.py >
    # start_background_refresh) - bu olmadan get_market_snapshot() artık
    # kendiliğinden yenilenmez (TTL kontrolü kaldırıldı, bkz. o modüldeki
    # not), yalnızca ilk isteğin döndürdüğü veride sonsuza kadar kalırdı.
    start_background_refresh()
    # Sektör ısı haritası için de AYNI mimari (bkz. yukarıdaki
    # _SECTOR_HEATMAP_BACKGROUND_INTERVAL_SECONDS notu).
    start_sector_heatmap_background_refresh()
    
    # Emtia raporu için eklendi (Phase 2 optimizasyonu)
    start_commodity_background_refresh()
    yield


app = FastAPI(title="Finansal Haber Dashboard", lifespan=lifespan)

# Genel/dış kullanıma açık, API-key korumalı REST API (bkz.
# src/web/api_v1.py) - dashboard'un yukarıdaki iç `/api/*` rotalarından
# TAMAMEN AYRI, kendi router'ında. Bu app'e (yerel `python main.py`
# geliştirme/test ortamı) eklenmesi opsiyoneldi ama tutarlılık için
# eklendi - GERÇEK dış kullanım hedefi Render (bkz. api/index.py).
app.include_router(api_v1_router)


def _record_to_view(record: NewsRecord, threshold: int) -> dict[str, Any]:
    score = record.importance_score
    if score is None:
        badge_class = "badge-unknown"
        badge_text = "?"
    elif score >= 5:
        badge_class = "badge-critical"
        badge_text = str(score)
    elif score >= threshold:
        badge_class = "badge-important"
        badge_text = str(score)
    elif score >= 3:
        badge_class = "badge-medium"
        badge_text = str(score)
    else:
        badge_class = "badge-low"
        badge_text = str(score)

    sentiment = (record.sentiment or "").strip().lower()
    sentiment_class = {
        "pozitif": "sentiment-positive",
        "negatif": "sentiment-negative",
        "notr": "sentiment-neutral",
    }.get(sentiment, "sentiment-unknown")
    sentiment_label = SENTIMENT_LABELS.get(sentiment)

    sector_labels = [SECTOR_LABELS.get(s, s) for s in record.sectors_list()]
    source_comparison = record.source_comparison_list()

    return {
        # "Detaylı İnceleme" sayfasındaki "Öne Çıkanlar" etiketlerinin
        # tıklanınca doğru karta kaydırılabilmesi için kararlı bir DOM
        # kimliği (bkz. detayli_inceleme.html) - group_key zaten benzersiz/
        # alfanumerik (bkz. src/db.py > compute_group_key), başka bir yerde
        # kullanılmıyorsa hiçbir şeyi bozmaz.
        "record_id": record.group_key,
        "title": record.title,
        "title_tr": record.title_tr,
        "sources": record.sources,
        "published_at": format_turkey_time(record.published_at) if record.published_at else "tarih bilinmiyor",
        "summary": record.summary or "(özet yok)",
        "key_points": record.key_points_list(),
        "importance_score": score,
        "importance_reason": record.importance_reason or "",
        "notified": record.notified,
        "links": record.links_list(),
        "badge_class": badge_class,
        "badge_text": badge_text,
        "is_highlighted": score is not None and score >= threshold,
        "sentiment_class": sentiment_class,
        "sentiment_label": sentiment_label,
        "sector_labels": sector_labels,
        "source_comparison": source_comparison,
        "company_ticker": record.company_ticker,
        "ticker_quote": None,
    }


def _build_sector_heatmap() -> list[dict[str, Any]]:
    """Son 24 saatteki haberleri sektöre göre gruplayıp basit bir "etki
    yoğunluğu" hesaplar: haber sayısı * ortalama önem skoru. Renk tonu,
    pozitif/negatif sentiment dağılımının net yönüne göre belirlenir (yeşil =
    net pozitif, kırmızı = net negatif, gri = az hareket). Karmaşık bir
    korelasyon modeli DEĞİLDİR - kasıtlı olarak basit/anlaşılır tutulmuştur."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    records = get_records_since(since)

    stats: dict[str, dict[str, float]] = {}
    for r in records:
        # Henüz sektör sınıflandırması yapılmamış kayıtlar (ör. bu özellik
        # eklenmeden önce özetlenmiş eski haberler, bkz. src/db.py migrasyon
        # notu) sessizce atlanır - "diger" gibi gerçek bir etikete
        # dönüştürülmez (regions için de aynı yaklaşım kullanılıyor, bkz.
        # get_records_since_by_region).
        for sector in r.sectors_list():
            s = stats.setdefault(sector, {"count": 0, "score_sum": 0, "pos": 0, "neg": 0, "notr": 0})
            s["count"] += 1
            if r.importance_score is not None:
                s["score_sum"] += r.importance_score
            if r.sentiment == "pozitif":
                s["pos"] += 1
            elif r.sentiment == "negatif":
                s["neg"] += 1
            elif r.sentiment == "notr":
                s["notr"] += 1

    if not stats:
        return []

    activity_by_sector = {}
    for sector, s in stats.items():
        avg_score = (s["score_sum"] / s["count"]) if s["count"] else 0.0
        activity_by_sector[sector] = s["count"] * avg_score

    max_activity = max(activity_by_sector.values()) or 1.0

    result: list[dict[str, Any]] = []
    for sector, s in stats.items():
        count = int(s["count"])
        avg_score = round((s["score_sum"] / count), 1) if count else 0.0
        activity_norm = activity_by_sector[sector] / max_activity
        sentiment_lean = (s["pos"] - s["neg"]) / count if count else 0.0

        low_activity = count < _HEATMAP_MIN_COUNT_FOR_COLOR or abs(sentiment_lean) < 0.15
        if low_activity:
            color = f"rgba(100, 116, 139, {0.25 + 0.25 * activity_norm:.2f})"
        elif sentiment_lean >= 0:
            alpha = 0.25 + 0.6 * activity_norm * min(1.0, 0.4 + sentiment_lean)
            color = f"rgba(34, 197, 94, {alpha:.2f})"
        else:
            alpha = 0.25 + 0.6 * activity_norm * min(1.0, 0.4 - sentiment_lean)
            color = f"rgba(239, 68, 68, {alpha:.2f})"

        result.append(
            {
                "sector": sector,
                "label": SECTOR_LABELS.get(sector, sector),
                "count": count,
                "avg_importance": avg_score,
                "positive": int(s["pos"]),
                "negative": int(s["neg"]),
                "neutral": int(s["notr"]),
                "color": color,
            }
        )

    result.sort(key=lambda item: activity_by_sector[item["sector"]], reverse=True)
    return result


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    source: str | None = None,
    sector: str | None = None,
    region: str | None = None,
    sentiment: str | None = None,
    # str olarak alınıyor (int | None DEĞİL): "Tüm Önem Skorları" seçeneği
    # boş bir değerle (min_importance=) form gönderir - FastAPI/Pydantic bunu
    # doğrudan `int | None` alana koyarsa boş string'i int'e çeviremediği için
    # 422 hatası döner. Bu yüzden burada str olarak alınıp aşağıda elle
    # (boşsa None'a düşecek şekilde) int'e çevriliyor.
    min_importance: str | None = None,
    q: str | None = None,
    sort: str = "newest",
) -> HTMLResponse:
    config = _get_cached_web_config()
    threshold = config.get("importance", {}).get("threshold", 4)
    max_items = config.get("web", {}).get("max_items", 100)

    min_importance_value: int | None = None
    if min_importance:
        try:
            min_importance_value = int(min_importance)
        except ValueError:
            min_importance_value = None

    sort_normalized = "oldest" if sort == "oldest" else "newest"

    # Filtresiz/varsayılan görünüm (bkz. _get_cached_default_records notu)
    # önbellekten okunur - aktif filtre/arama varsa CANLI sorguya düşülür.
    is_default_view = sort_normalized == "newest" and not any(
        [source, sector, region, sentiment, min_importance_value, q]
    )

    with get_session() as session:
        if is_default_view:
            records = _get_cached_default_records(session, max_items)
        else:
            records = get_recent_records(
                session,
                limit=max_items,
                source_filter=source or None,
                sector_filter=sector or None,
                region_filter=region or None,
                sentiment_filter=sentiment or None,
                min_importance=min_importance_value,
                search_query=q or None,
                sort_order=sort_normalized,
            )
        sources, sectors = _get_cached_filter_options(session)

    regions = [{"slug": r, "label": REGION_LABELS.get(r, r)} for r in VALID_REGIONS]
    sentiments = [{"slug": s, "label": SENTIMENT_LABELS.get(s, s)} for s in VALID_SENTIMENTS]

    fear_greed_index = _compute_fear_greed_index(records)

    views = [_record_to_view(r, threshold) for r in records]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "records": views,
            "sources": sources,
            "selected_source": source or "",
            "sectors": sectors,
            "selected_sector": sector or "",
            "regions": regions,
            "selected_region": region or "",
            "sentiments": sentiments,
            "selected_sentiment": sentiment or "",
            "importance_options": _IMPORTANCE_FILTER_OPTIONS,
            "selected_min_importance": min_importance_value,
            "selected_query": q or "",
            "selected_sort": sort_normalized,
            "threshold": threshold,
            "total_count": len(views),
            "fear_greed_index": fear_greed_index,
        },
    )


#  Her sütun (makro/şirket/siyasi) için bağımsız çapraz-sektör alt filtresi
#  query param adı - kullanıcı gereksiniminde önerilen isimlendirme (bkz.
#  sohbet): "makro_sector", "sirket_sector", "siyasi_sector".
_COLUMN_SECTOR_PARAM = {slug: f"{slug}_sector" for slug in VALID_TOP_CATEGORIES if slug != "diger"}


@app.get("/detayli-inceleme", response_class=HTMLResponse)
def detayli_inceleme_page(
    request: Request,
    sort: str = "newest",
    source: str | None = None,
    sector: str | None = None,
    region: str | None = None,
    sentiment: str | None = None,
    # str olarak alınıyor - bkz. dashboard() route'undaki AYNI notun gerekçesi
    # (boş "Tüm Önem Skorları" seçeneği int alana 422 hatası verir).
    min_importance: str | None = None,
    makro_sector: str | None = None,
    sirket_sector: str | None = None,
    siyasi_sector: str | None = None,
) -> HTMLResponse:
    """"Detaylı İnceleme" sayfası: 3 kategoriyi (makro/şirket/siyasi, bkz.
    src/summarizer.py > VALID_TOP_CATEGORIES) AYNI ANDA, yan yana 3 sütun
    halinde gösteren bir görünüm - sekme/tıklama YOK, kullanıcı üçünü de tek
    ekranda karşılaştırır (bkz. gereksinim).

    Ana dashboard'daki TÜM genel filtreler (kaynak/sektör/bölge/duygu/önem
    skoru) burada da mevcuttur ve verildiğinde ÜÇ SÜTUNA DA AYNI ANDA
    uygulanır (bkz. kullanıcı gereksinimi, 2026-07). Bunun ÜSTÜNE, her sütun
    kendi BAĞIMSIZ "çapraz sektör" alt filtresine sahiptir (`makro_sector`,
    `sirket_sector`, `siyasi_sector` query param'ları) - ör. "Siyasi"
    sütununda `siyasi_sector=teknoloji` verilirse, o sütunda YALNIZCA
    top_category=siyasi VE sector listesinde "teknoloji" olan haberler
    görünür; diğer iki sütun bundan ETKİLENMEZ (bkz. get_recent_records >
    additional_sector_filter, src/db.py).

    NOT: `top_category` YENİ bir alan olduğundan, bu özellik eklenmeden
    ÖNCE özetlenmiş eski haberlerde bu alan boştur (None) - dedup/group_key
    önbelleği zaten özetlenmiş haberleri yeniden özetlemediğinden (bkz.
    src/main.py > _reuse_or_mark_for_summarization), geçmiş haberler
    otomatik olarak GERİYE DÖNÜK sınıflandırılmaz. Yalnızca bu değişiklikten
    SONRA yeni özetlenen haberler sütunlarda görünür - zamanla artar.
    """
    config = load_config()
    threshold = config.get("importance", {}).get("threshold", 4)
    # Sütun başına makul bir üst sınır - 3 sütun AYNI ANDA render edildiğinden
    # (ana dashboard'daki TEK liste yerine), ana dashboard'un max_items'ı
    # (100) burada 3 KERE uygulanırsa sayfa gereksiz ağırlaşır; her sütun
    # kendi içinde zaten bağımsız kaydırılabilir olduğundan 30 fazlasıyla
    # yeterli.
    per_column_limit = 30

    min_importance_value: int | None = None
    if min_importance:
        try:
            min_importance_value = int(min_importance)
        except ValueError:
            min_importance_value = None

    sort_normalized = "oldest" if sort == "oldest" else "newest"

    # Yalnızca 3 ana sütun gösterilir (bkz. gereksinim) - "diger" bilerek
    # bir sütun olarak sunulmaz.
    category_slugs = [c for c in VALID_TOP_CATEGORIES if c != "diger"]

    # Sütun bazlı alt filtre değerleri - hangi query param'ın hangi sütuna
    # ait olduğu _COLUMN_SECTOR_PARAM'da tanımlı, burada gelen değerlerle
    # eşleniyor (her sütun BAĞIMSIZ, birbirini etkilemez - bkz. gereksinim).
    column_sub_sector = {
        "makro": makro_sector or None,
        "sirket": sirket_sector or None,
        "siyasi": siyasi_sector or None,
    }

    columns: list[dict[str, Any]] = []
    all_records: list[NewsRecord] = []
    with get_session() as session:
        sources, sectors = _get_cached_filter_options(session)
        for slug in category_slugs:
            records = get_recent_records(
                session,
                limit=per_column_limit,
                category_filter=slug,
                source_filter=source or None,
                sector_filter=sector or None,
                region_filter=region or None,
                sentiment_filter=sentiment or None,
                min_importance=min_importance_value,
                additional_sector_filter=column_sub_sector[slug],
                sort_order=sort_normalized,
            )
            all_records.extend(records)
            views = [_record_to_view(r, threshold) for r in records]
            # Sütun önizlemesi: o kategorideki EN ÖNEMLİ birkaç haber başlığı
            # (zaten çekilmiş `views`'tan, EK bir sorgu YAPILMADAN) -
            # kullanıcının seçtiği sıralamadan (en yeni/en eski) BAĞIMSIZ
            # olarak önem skoruna göre.
            preview = sorted(
                views,
                key=lambda v: v["importance_score"] if v["importance_score"] is not None else 0,
                reverse=True,
            )[:5]
            columns.append(
                {
                    "slug": slug,
                    "label": TOP_CATEGORY_LABELS.get(slug, slug),
                    "records": views,
                    "preview": preview,
                    "count": len(views),
                    "sub_filter_param": _COLUMN_SECTOR_PARAM[slug],
                    "selected_sub_sector": column_sub_sector[slug] or "",
                }
            )

    # NOT: Şirket ticker'larının GERÇEK fiyatı artık BURADA (sayfa render'ı
    # SIRASINDA) çekilmiyor. GERÇEK Render testinde (2026-07) bu senkron/
    # bloklayan yaklaşımın, Render'ın kısıtlı 0.1 vCPU'su altında 9 sembolden
    # sadece ~1'inin timeout içinde tamamlanmasına yol açtığı ölçüldü. Bunun
    # yerine sayfa tamamen yüklendikten SONRA, tarayıcıda arka planda
    # `/api/ticker-quotes` endpoint'i çağrılır (bkz. detayli_inceleme.html
    # <script>, ve aşağıdaki `ticker_quotes_endpoint` route'u) - sayfa
    # render'ı hiçbir şekilde bloklanmaz.
    # Piyasa Duygusu, bu sayfada gösterilen 3 kategorinin BİRLEŞİMİNDEN
    # (union) hesaplanır - ana dashboard'daki gibi TEK bir filtrelenmiş
    # listeden değil, çünkü bu sayfada artık "tek liste" kavramı yok.
    fear_greed_index = _compute_fear_greed_index(all_records)

    regions = [{"slug": r, "label": REGION_LABELS.get(r, r)} for r in VALID_REGIONS]
    sentiments = [{"slug": s, "label": SENTIMENT_LABELS.get(s, s)} for s in VALID_SENTIMENTS]

    return templates.TemplateResponse(
        request,
        "detayli_inceleme.html",
        {
            "columns": columns,
            "selected_sort": sort_normalized,
            "threshold": threshold,
            "fear_greed_index": fear_greed_index,
            "sources": sources,
            "selected_source": source or "",
            "sectors": sectors,
            "selected_sector": sector or "",
            "regions": regions,
            "selected_region": region or "",
            "sentiments": sentiments,
            "selected_sentiment": sentiment or "",
            "importance_options": _IMPORTANCE_FILTER_OPTIONS,
            "selected_min_importance": min_importance_value,
        },
    )


@app.get("/sirket-profili", response_class=HTMLResponse)
def company_profile_page(request: Request, q: str | None = None) -> HTMLResponse:
    """Şirket/hisse bazlı otomatik profil sayfası (bkz. src/company_profile.py):
    kullanıcı bir şirket adı girer, son 30 günün ilgili haberleri + LLM
    tarafından üretilmiş kısa bir genel görünüm özeti gösterilir."""
    config = load_config()
    threshold = config.get("importance", {}).get("threshold", 4)

    profile: dict[str, Any] | None = None
    if q:
        profile = get_company_profile(q, config)

    views = [_record_to_view(r, threshold) for r in profile["records"]] if profile else []

    return templates.TemplateResponse(
        request,
        "company_profile.html",
        {
            "query": q or "",
            "profile": profile,
            "records": views,
        },
    )


@app.get("/kaynak-sagligi", response_class=HTMLResponse)
def source_health_page(request: Request) -> HTMLResponse:
    """Kaynak sağlık paneli: her fetcher'ın son 24 saatteki başarı oranı,
    ortalama haber sayısı, son başarılı/başarısız çalışma zamanı (bkz.
    src/db.py > get_source_health_summary, record_source_health)."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    raw_rows = get_source_health_summary(since)

    def _fmt(dt: datetime | None) -> str:
        return format_turkey_time(dt) if dt else "—"

    rows = [
        {
            **row,
            "last_success_display": _fmt(row["last_success_at"]),
            "last_failure_display": _fmt(row["last_failure_at"]),
        }
        for row in raw_rows
    ]
    return templates.TemplateResponse(request, "source_health.html", {"rows": rows})


_admin_basic_auth = HTTPBasic()


def _check_admin_secret(credentials: HTTPBasicCredentials = Depends(_admin_basic_auth)) -> None:
    """HTTP Basic Auth ile korur - kullanıcı adı ÖNEMSİZDİR (kontrol edilmez),
    yalnızca ŞİFRE alanı mevcut `WEBHOOK_INGEST_SECRET` (.env) ile karşılaştırılır.
    Webhook endpoint'inin (bkz. _check_webhook_secret) AYNI paylaşılan sırrını
    yeniden kullanır - kullanıcı isteği gereği ayrı bir kullanıcı/parola
    veritabanı/auth sistemi KURULMADI.

    HTTP Basic (URL query param YERİNE) BİLİNÇLİ tercih edildi: tarayıcı
    kimlik bilgilerini native bir login kutusuyla ister, sır bu sayede
    URL'de/tarayıcı geçmişinde/sunucu access log'unda AÇIK METİN olarak
    KALMAZ (bkz. Authorization header, URL'den ayrı taşınır).

    Sır .env'de HİÇ tanımlanmamışsa (webhook endpoint'iyle AYNI davranış,
    bkz. _check_webhook_secret) sayfa TAMAMEN kapalı tutulur (503) -
    varsayılan olarak güvenli taraf. `secrets.compare_digest` ZAMANLAMA
    SALDIRISINA karşı sabit-zamanlı karşılaştırma sağlar (basit `==` yerine).

    AYRICA BİLEREK sadece BURADA (src/web/app.py) tanımlıdır, api/index.py'ye
    (Render, herkese açık salt-okunur dashboard) KAYITLI DEĞİLDİR - abone
    listesi kişisel veri (chat_id, kullanıcı adı) içerdiğinden, /sirket-profili'nin
    dışarıda bırakılmasıyla AYNI desen (bkz. api/index.py modül docstring'i)
    kullanılarak bu sayfa hiçbir zaman herkese açık deploy'a taşınmaz."""
    expected = os.environ.get("WEBHOOK_INGEST_SECRET", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="WEBHOOK_INGEST_SECRET .env'de tanımlı değil, admin sayfası devre dışı.",
        )
    if not secrets.compare_digest(credentials.password, expected):
        raise HTTPException(status_code=401, detail="Geçersiz şifre.", headers={"WWW-Authenticate": "Basic"})


@app.get("/admin/subscribers", response_class=HTMLResponse)
def admin_subscribers(request: Request, _: None = Depends(_check_admin_secret)) -> HTMLResponse:
    """Telegram abonelerinin (chat_id, kullanıcı adı, önem eşiği, "sadece
    KAP" modu, takip ettiği kelimeler, abone olma/son aktif olma zamanı)
    tablo halinde listelendiği admin görünümü - bkz. src/db.py >
    get_all_subscribers_overview. HTTP Basic Auth ile korunur (bkz.
    _check_admin_secret) - kullanıcı adı: (boş bırakılabilir), şifre:
    WEBHOOK_INGEST_SECRET."""
    raw_rows = get_all_subscribers_overview()

    def _fmt(dt: datetime | None) -> str:
        return format_turkey_time(dt) if dt else "—"

    rows = [
        {
            **row,
            "subscribed_at_display": _fmt(row["subscribed_at"]),
            "last_active_at_display": _fmt(row["last_active_at"]),
            "kap_only_until_display": _fmt(row["kap_only_until"]) if row["kap_only_active"] else None,
        }
        for row in raw_rows
    ]
    return templates.TemplateResponse(
        request,
        "admin_subscribers.html",
        {"rows": rows, "total_count": len(rows)},
    )


@app.get("/api/market-data")
async def market_data() -> list[dict[str, Any]]:
    """Dashboard'daki canlı piyasa şeridinin periyodik (AJAX) olarak çektiği
    veri (bkz. src/web/market_data.py ve templates/dashboard.html)."""
    return await get_market_snapshot()


@app.get("/api/ticker-quotes")
async def ticker_quotes_endpoint(tickers: str = "") -> dict[str, Any]:
    """"Detaylı İnceleme" > Şirket kartlarındaki borsa/ticker etiketleri için
    GERÇEK anlık fiyat/günlük değişim - bkz. src/web/market_data.py >
    get_quotes_for_company_tickers. `tickers` virgülle ayrılmış "BORSA: SEMBOL"
    listesi (bkz. detayli_inceleme.html <script>, sayfa TAMAMEN yüklendikten
    SONRA tarayıcıda çağrılır - sayfa render'ını HİÇ bloklamaz, bkz.
    detayli_inceleme_page'teki not). `async def` olması ÖNEMLİ: sayfa
    render'ının aksine burada asyncio.run() gerekmez, FastAPI'nin kendi
    event loop'unda doğrudan çalışır.

    Çözülemeyen/başarısız sembolller sessizce sonuçta YER ALMAZ (frontend
    bu durumda o etikete sadece fiyat eklemeden bırakır, hata göstermez)."""
    parsed = [t.strip() for t in tickers.split(",") if t.strip()]
    if not parsed:
        return {}
    return await get_quotes_for_company_tickers(parsed)


@app.get("/api/commodity-weekly-report")
def commodity_weekly_report_endpoint() -> dict[str, Any]:
    """Dashboard'daki "Haftalık Emtia Raporu" panelinin (bkz. Faz 2)
    çektiği veri - bkz. src/commodity_report.py > get_commodity_dashboard_data
    (önbellekten, LLM analizi/şirket isimleri DAHİL - ek bir LLM çağrısı
    YAPMAZ; önbellek boşsa tek seferlik LLM'siz fiyat-only fallback).

    `def` (senkron) - iç kısımda `asyncio.run()` çağrılabiliyor (fallback
    yolunda), bu yüzden `async def` OLAMAZ (FastAPI zaten senkron route'ları
    kendi thread pool'unda çalıştırır, event loop çakışması olmaz)."""
    return get_commodity_dashboard_data()


@app.get("/api/company-detail")
async def company_detail_endpoint(ticker: str = "", name: str = "") -> dict[str, Any]:
    """Haftalık emtia raporu panelindeki tıklanabilir şirket kartı/chip'i
    (bkz. dashboard.html > şirket detay modalı) için detay verisi: güncel
    fiyat/değişim + 1 aylık sparkline geçmişi (bkz. src/web/market_data.py >
    get_ticker_detail, kendi 30 sn/5 dk önbellekleriyle) + veritabanında bu
    ticker'a (company_ticker alanı TAM eşleşen) etiketlenmiş son 30 günün
    haberleri (bkz. src/db.py > get_records_by_company_ticker).

    BİLİNÇLİ olarak `/sirket-profili` sayfasının (src/company_profile.py)
    AKSİNE hiçbir LLM çağrısı YAPMAZ - modal her açıldığında ek maliyet/
    gecikme olmadan anlık açılabilsin diye (gereksinim: emtia raporu zaten
    haftada bir LLM analizi üretiyor, modal AYRICA bir LLM çağrısı
    tetiklemesin).

    `ticker` "BORSA: SEMBOL" formatında beklenir (ör. "NYSE: FCX" - bkz.
    src/summarizer.py > Summarizer._parse_commodity_companies). Boş/geçersiz
    ticker'da veya Yahoo'dan hiçbir veri alınamadığında `quote`/`history`
    boş/None döner - exception FIRLATMAZ, frontend bu durumda ilgili
    bölümleri sessizce gizler."""
    ticker = ticker.strip()
    if not ticker:
        return {"ticker": ticker, "name": name, "quote": None, "history": [], "news": []}

    detail = await get_ticker_detail(ticker)
    since = datetime.now(timezone.utc) - timedelta(days=30)
    records = get_records_by_company_ticker(ticker, limit=10, since=since)
    news = [
        {
            "title": r.title,
            "sources": r.sources,
            "summary": (r.summary or "")[:280],
            "published_at": format_turkey_time(r.first_seen_at),
            "link": (r.links_list()[0]["link"] if r.links_list() else ""),
        }
        for r in records
    ]

    return {
        "ticker": ticker,
        "name": name,
        "quote": detail["quote"] if detail else None,
        "history": detail["history"] if detail else [],
        "news": news,
    }


def _sector_heatmap_background_loop() -> None:
    """Süreç boyunca sürekli çalışır: kullanıcı isteklerinden BAĞIMSIZ olarak
    her _SECTOR_HEATMAP_BACKGROUND_INTERVAL_SECONDS (20 sn) bir önbelleği
    proaktif tazeler (bkz. modül başındaki mimari not - market_data.py'deki
    _background_refresh_loop ile AYNI desen, sync/threading versiyonu)."""
    while not _sector_heatmap_background_stop.is_set():
        try:
            data = _build_sector_heatmap()
            with _sector_heatmap_lock:
                _sector_heatmap_cache["data"] = data
                _sector_heatmap_cache["fetched_at"] = time.monotonic()
        except Exception:  # noqa: BLE001 - bir tur başarısız olursa döngü YİNE DE devam etsin
            logger.exception("Arka plan sektör ısı haritası tazeleme döngüsünde beklenmeyen hata.")
        _sector_heatmap_background_stop.wait(_SECTOR_HEATMAP_BACKGROUND_INTERVAL_SECONDS)


def start_sector_heatmap_background_refresh() -> None:
    """Arka plan tazeleme thread'ini başlatır - uygulama lifespan'ından TEK
    sefer çağrılır. İdempotenttir (zaten çalışan bir thread varsa tekrar
    başlatmaz)."""
    global _sector_heatmap_background_thread
    if _sector_heatmap_background_thread is not None and _sector_heatmap_background_thread.is_alive():
        return
    _sector_heatmap_background_thread = threading.Thread(
        target=_sector_heatmap_background_loop, daemon=True, name="sector-heatmap-refresh"
    )
    _sector_heatmap_background_thread.start()


def _get_cached_sector_heatmap() -> list[dict[str, Any]]:
    """NORMAL DURUMDA hiçbir DB round-trip'i BEKLEMEZ - arka plan görevinin
    az önce yazdığı hazır önbellekten okur. Tek istisna: süreç yeni
    başladıysa ve arka plan görevi henüz ilk tazelemesini yapmadıysa, o TEK
    seferlik soğuk başlangıç anında burada senkron bir fallback yapılır
    (`_sector_heatmap_lock` thundering-herd korumasıyla, market_data.py'deki
    get_market_snapshot ile AYNI desen)."""
    if _sector_heatmap_cache["data"] is not None:
        return _sector_heatmap_cache["data"]

    with _sector_heatmap_lock:
        if _sector_heatmap_cache["data"] is not None:
            return _sector_heatmap_cache["data"]
        data = _build_sector_heatmap()
        _sector_heatmap_cache["data"] = data
        _sector_heatmap_cache["fetched_at"] = time.monotonic()
        return data


@app.get("/api/sector-heatmap")
def sector_heatmap() -> list[dict[str, Any]]:
    """Dashboard'daki sektör ısı haritasının periyodik (AJAX) olarak çektiği
    veri (bkz. _build_sector_heatmap, _get_cached_sector_heatmap)."""
    return _get_cached_sector_heatmap()


@app.get("/api/trend-summary")
def trend_summary(period: str = "weekly") -> dict[str, Any]:
    """Dashboard'daki trend panelinin periyodik/isteğe bağlı (AJAX) olarak
    çektiği veri (bkz. src/trend_report.py > get_dashboard_trend_summary).
    Telegram'a gönderilen haftalık/aylık raporla AYNI hesaplama mantığını
    kullanır - bu yüzden ikisi arasında hiçbir tutarsızlık olmaz."""
    period_norm = "monthly" if period == "monthly" else "weekly"
    return get_dashboard_trend_summary(period_norm)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------
# Web Push bildirimleri (bkz. src/web_push.py) - Telegram'a PARALEL, TAMAMEN
# AYRI bir kanal. Sekme/tarayıcı KAPALIYKEN bile çalışır (bkz. sw.js).
# --------------------------------------------------------------------------

_SW_JS_PATH = Path(__file__).resolve().parent / "static" / "sw.js"


@app.get("/sw.js")
def service_worker_file() -> FileResponse:
    """Service Worker dosyasını KÖK yoldan (/sw.js) servis eder - fiziksel
    dosya `src/web/static/sw.js`'de dursa da, bir Service Worker'ın
    varsayılan "scope"u kendi URL'inin bulunduğu dizin olduğundan (bkz.
    sw.js docstring'i), `/static/sw.js` olarak servis edilseydi scope
    `/static/`'le sınırlı kalır, dashboard'un geri kalanını (ana sayfa `/`
    dahil) KAPSAMAZDI.

    `Cache-Control: no-cache` KASITLI: tarayıcılar Service Worker
    dosyasını genelde zaten kendi güncelleme mantığıyla (byte-karşılaştırma)
    kontrol eder, ama agresif bir HTTP önbelleği bu kontrolü GECİKTİREBİLİR -
    özellikle geliştirme/güncelleme sırasında yeni sürümün hemen
    yakalanmasını garanti eder."""
    return FileResponse(_SW_JS_PATH, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


_VY_LOGO_PATH = Path(__file__).resolve().parent / "static" / "vy-logo.svg"


@app.get("/vy-logo.svg")
def vy_logo_file() -> FileResponse:
    """Header'daki küçük VY logosu - sw.js ile AYNI nedenle (basit tek
    dosyalık statik varlıklar için tam bir StaticFiles mount'u yerine
    doğrudan bir route) kök yoldan servis edilir."""
    return FileResponse(_VY_LOGO_PATH, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/push/vapid-public-key")
def push_vapid_public_key() -> dict[str, str]:
    """Frontend'in `PushManager.subscribe({applicationServerKey: ...})`
    çağrısı için gereken VAPID genel anahtarı (bkz. src/web_push.py >
    get_vapid_public_key). Herkese açık bir anahtardır - gizli DEĞİLDİR
    (VAPID'in özel anahtarı hiçbir zaman istemciye gönderilmez)."""
    return {"publicKey": get_vapid_public_key()}


class _PushFilters(BaseModel):
    """Bir push aboneliğinin hangi haberler için bildirim almak istediğini
    belirten filtre - dashboard'un MEVCUT filtre formuyla (bkz.
    dashboard.html > `<form class="filter">`) BİREBİR AYNI eksenler. Her
    alan BOŞ LİSTE/None ise o eksende filtre YOK (her değer eşleşir) -
    bkz. src/web_push.py > _record_matches_filters."""

    sources: list[str] = []
    sectors: list[str] = []
    regions: list[str] = []
    sentiments: list[str] = []
    min_importance: int | None = None


class _PushSubscriptionPayload(BaseModel):
    """Tarayıcının `PushSubscription.toJSON()` çıktısı (endpoint/keys) +
    isteğe bağlı `filters` (bkz. dashboard.html > subscribeToPushWithFilters).
    `filters` gönderilmezse (Faz 1 tarzı ham abonelik) mevcut filtre
    (varsa) KORUNUR - bkz. src/db.py > upsert_push_subscription."""

    endpoint: str
    keys: dict[str, str]
    filters: _PushFilters | None = None


@app.post("/api/push/subscribe")
def push_subscribe(payload: _PushSubscriptionPayload) -> JSONResponse:
    """Tarayıcının push aboneliğini (+ isteğe bağlı filtresini) kaydeder/
    günceller (bkz. src/db.py > upsert_push_subscription). Kullanıcı
    dashboard'daki filtre formunu değiştirip "Bu filtrelere göre bildirim
    al" dediğinde AYNI endpoint tekrar çağrılır - yeni bir abonelik
    OLUŞTURULMAZ, mevcut olan GÜNCELLENİR (bkz. o fonksiyonun "endpoint
    zaten varsa güncelle" mantığı)."""
    p256dh = payload.keys.get("p256dh", "")
    auth = payload.keys.get("auth", "")
    if not payload.endpoint or not p256dh or not auth:
        return JSONResponse({"error": "Eksik abonelik bilgisi."}, status_code=400)

    filters_dict = payload.filters.model_dump() if payload.filters is not None else None
    upsert_push_subscription(payload.endpoint, p256dh, auth, filters_dict)
    return JSONResponse({"status": "ok"})


class _PushUnsubscribePayload(BaseModel):
    endpoint: str


@app.post("/api/push/unsubscribe")
def push_unsubscribe(payload: _PushUnsubscribePayload) -> JSONResponse:
    """Kullanıcı dashboard'da bildirimleri kapattığında VEYA tarayıcı
    aboneliği kendiliğinden geçersiz kıldığında çağrılır (bkz.
    dashboard.html > unsubscribeFromPush)."""
    remove_push_subscription(payload.endpoint)
    return JSONResponse({"status": "ok"})


@app.get("/api/push/subscription-status")
def push_subscription_status(endpoint: str = "") -> dict[str, Any]:
    """Dashboard sayfa yüklenince, tarayıcının HALİHAZIRDA sahip olduğu bir
    push aboneliğinin (varsa) hangi filtreyle kaydedildiğini gösterebilmek
    için (bkz. dashboard.html > updatePushButtonState). Abonelik yoksa/
    endpoint boşsa `{"subscribed": false}` döner."""
    if not endpoint:
        return {"subscribed": False}
    with get_session() as session:
        row = session.query(PushSubscription).filter_by(endpoint=endpoint).one_or_none()
        if row is None:
            return {"subscribed": False}
        filters = None
        if row.filters:
            try:
                filters = json.loads(row.filters)
            except ValueError:
                filters = None
        return {"subscribed": True, "filters": filters}


# --------------------------------------------------------------------------
# Event-Driven Ingestion: dış kaynaklardan (ör. src/fetchers/telegram_listener.py
# - KAP bildirimlerini yayınlayan bir Telegram kanalı) PUSH edilen tekil
# haber/bildirim webhook'u (bkz. src/fetchers/webhook.py). BİLİNÇLİ OLARAK
# api/index.py'ye (Vercel/Render'daki salt-okunur public dashboard) YANSITILMAZ
# - GERÇEK bir LLM çağrısı + TÜM abonelere Telegram/Web Push bildirimi
# TETİKLER (tıpkı /sirket-profili gibi, bkz. api/index.py modül docstring'i),
# bu yüzden yalnızca yerel `python main.py`'de (bu dosyada) kalır.
# --------------------------------------------------------------------------


class _WebhookDisclosurePayload(BaseModel):
    """Dış bir kaynaktan (ör. src/fetchers/telegram_listener.py, veya
    kullanıcının kendi elle çalıştırdığı bir cURL/otomasyon aracı) PUSH
    edilen tekil bir KAP/BIST bildirimi. `title` DIŞINDA hepsi opsiyonel -
    ör. yalnızca ham metin geldiğinde (ticker/tarih ayrıştırılamadığında)
    bile pipeline'a sokulabilsin diye (bkz. src/fetchers/webhook.py >
    process_incoming_disclosure - özetleyici zaten ham metinden bir özet
    üretebiliyor)."""

    title: str
    text: str = ""
    ticker: str | None = None
    source: str = DEFAULT_SOURCE_NAME
    link: str = ""
    published_at: datetime | None = None


def _check_webhook_secret(x_webhook_secret: str | None) -> None:
    """`.env > WEBHOOK_INGEST_SECRET` ile eşleşen bir `X-Webhook-Secret`
    header'ı zorunlu kılar. Bu endpoint GERÇEK bir LLM çağrısı + tüm
    abonelere bildirim TETİKLEDİĞİNDEN (bkz. process_incoming_disclosure),
    src/web/api_v1.py'deki genel/dış API'nin AKSİNE kimlik doğrulamasız
    bırakılamaz - ama o API'nin veritabanı-destekli çok-kullanıcılı
    `ApiKey` mekanizması burada GEREKSİZ karmaşıklık olurdu (bu endpoint
    yalnızca kullanıcının KENDİ dinleyici script'i tarafından çağrılır) -
    bu yüzden basit, tek bir paylaşılan sır yeterli.

    Sır .env'de HİÇ tanımlanmamışsa (ör. kullanıcı henüz eklemedi) endpoint'i
    "kimlik doğrulamasız açık" bırakmak yerine TAMAMEN kapalı tutar (503) -
    varsayılan olarak güvenli taraf."""
    expected = os.environ.get("WEBHOOK_INGEST_SECRET", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="WEBHOOK_INGEST_SECRET .env'de tanımlı değil, webhook endpoint'i devre dışı.",
        )
    if not x_webhook_secret or x_webhook_secret != expected:
        raise HTTPException(status_code=401, detail="Geçersiz veya eksik X-Webhook-Secret header'ı.")


@app.post("/api/webhook/kap-bildirim")
def webhook_kap_bildirim(
    payload: _WebhookDisclosurePayload,
    background_tasks: BackgroundTasks,
    x_webhook_secret: str | None = Header(default=None),
) -> JSONResponse:
    """Dış bir kaynaktan (bkz. src/fetchers/telegram_listener.py veya elle
    bir cURL isteği) anlık KAP/BIST bildirimi kabul eder ve mevcut özetleme/
    önem skorlama/kayıt/bildirim pipeline'ına sokar (bkz.
    src/fetchers/webhook.py > process_incoming_disclosure).

    İşleme (LLM çağrısı + rate-limit bekleme + Telegram/Web Push gönderimi,
    saniyeler sürebilir) `BackgroundTasks` ile arka plana alınır - istek
    hemen 202 döner, çağıran taraf (ör. Telegram dinleyicisi, tek bir
    asyncio event loop'unda çalışıyor) saniyelerce bloklanmaz.

    Örnek kullanım (elle test için):
        curl -X POST http://127.0.0.1:8000/api/webhook/kap-bildirim \\
             -H "Content-Type: application/json" \\
             -H "X-Webhook-Secret: <WEBHOOK_INGEST_SECRET>" \\
             -d '{"title": "Ornek Sirket A.S. onemli bir sozlesme imzaladi.", "ticker": "ORNEK"}'
    """
    _check_webhook_secret(x_webhook_secret)

    if not payload.title.strip():
        return JSONResponse({"error": "title alanı zorunlu ve boş olamaz."}, status_code=400)

    config = load_config()
    disclosure = IncomingDisclosure(
        title=payload.title,
        text=payload.text,
        ticker=payload.ticker,
        source=(payload.source or DEFAULT_SOURCE_NAME),
        link=payload.link,
        published_at=payload.published_at,
    )
    background_tasks.add_task(process_incoming_disclosure, disclosure, config)
    logger.info("Webhook üzerinden dış bildirim kabul edildi (arka planda işlenecek): %s", payload.title)
    return JSONResponse({"status": "accepted"}, status_code=202)
