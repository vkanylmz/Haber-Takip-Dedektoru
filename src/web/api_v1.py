"""Genel/dış kullanıma açık, API-key korumalı REST API (`/api/v1/*`).

BİLİNÇLİ OLARAK dashboard'un İÇ kullandığı `/api/*` rotalarından (bkz.
src/web/app.py) TAMAMEN AYRI bir router - kimlik doğrulaması GEREKTİRMEYEN
iç rotaların davranışını/güvenliğini bu yeni katman hiçbir şekilde
ETKİLEMEZ, sadece MEVCUT veri fonksiyonlarını (get_ticker_detail,
get_market_snapshot, get_commodity_dashboard_data, vb.) yeniden kullanır -
kod tekrarı yok, ama YETKİLENDİRME/RATE-LIMIT katmanı her `/api/v1/*`
isteğinde AYRICA uygulanır (bkz. require_api_key).

GÜVENLİK: Bu router'ın hiçbir response modeli abone listesi/Telegram
chat_id'si/API anahtarlarının kendisi gibi hassas bir alan İÇERMEZ -
sadece haber/piyasa verisi (bkz. NewsOut, CompanyDetailOut vb. modelleri,
hepsi elle seçilmiş/whitelist alanlar).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from src.commodity_report import get_commodity_dashboard_data
from src.db import (
    ApiKey,
    NewsRecord,
    get_api_key_by_value,
    get_calendar_events_filtered,
    get_public_news_by_id,
    get_public_news_page,
    get_records_by_company_ticker,
    record_api_key_usage,
)
from src.web.market_data import get_market_snapshot, get_ticker_detail

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["public-api-v1"])


# --------------------------------------------------------------------------
# Kimlik doğrulama + rate-limit (bkz. src/db.py > ApiKey)
# --------------------------------------------------------------------------

# Render KASITLI OLARAK tek worker çalıştırdığından (bkz. render.yaml >
# "BİRDEN FAZLA worker EKLEMEYİN" notu, src/web/market_data.py'deki AYNI
# mimari karar) süreç-içi/in-memory bir sayaç burada GÜVENİLİRDİR - N
# worker olsaydı her biri kendi sayacını tutar, gerçek limit N katına
# çıkardı (piyasa verisi önbelleğiyle AYNI mimari kısıt/karar).
RATE_LIMITS: dict[str, dict[str, int]] = {
    "standard": {"per_minute": 60, "per_day": 5000},
    # "Sınırsız" yerine (bir hatalı istemci döngüsü sunucuyu GERÇEKTEN
    # aşırı yükleyebilir) çok yüksek ama SONLU bir değer - kullanıcının
    # KENDİ kullanımı için pratikte hiç dokunulmayacak bir tavan.
    "admin": {"per_minute": 1000, "per_day": 200_000},
}

_rate_limit_state: dict[str, dict[str, float]] = {}
_rate_limit_lock = threading.Lock()


def _check_rate_limit(key: str, tier: str) -> None:
    """Sabit pencereli (fixed-window) basit bir rate-limit: her anahtar
    için ayrı bir dakikalık VE günlük sayaç tutulur, pencere süresi
    dolunca sıfırlanır. Süreç yeniden başlarsa (ör. Render deploy)
    sayaçlar sıfırlanır - bu KASITLI/kabul edilebilir bir basitleştirme
    (kötüye kullanıma karşı MAKUL bir engel, askeri sınıf bir çözüm
    DEĞİL - kişisel/küçük ölçekli bir API için yeterli)."""
    limits = RATE_LIMITS.get(tier, RATE_LIMITS["standard"])
    now = time.time()
    with _rate_limit_lock:
        state = _rate_limit_state.setdefault(
            key, {"minute_start": now, "minute_count": 0.0, "day_start": now, "day_count": 0.0}
        )
        if now - state["minute_start"] >= 60:
            state["minute_start"] = now
            state["minute_count"] = 0.0
        if now - state["day_start"] >= 86400:
            state["day_start"] = now
            state["day_count"] = 0.0

        if state["minute_count"] >= limits["per_minute"]:
            raise HTTPException(
                status_code=429,
                detail=f"Dakikalık istek limiti ({limits['per_minute']}) aşıldı. Lütfen bir dakika sonra tekrar deneyin.",
            )
        if state["day_count"] >= limits["per_day"]:
            raise HTTPException(status_code=429, detail=f"Günlük istek limiti ({limits['per_day']}) aşıldı.")

        state["minute_count"] += 1
        state["day_count"] += 1


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> ApiKey:
    """TÜM `/api/v1/*` rotalarının ortak bağımlılığı (bkz. her endpoint'teki
    `Depends(require_api_key)`). `X-API-Key` header'ı eksik/geçersizse 401,
    rate-limit aşıldıysa 429 döner. Anahtarın KENDİSİ hiçbir response'a
    YANSITILMAZ (bkz. modül docstring'i)."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header eksik.")
    api_key_row = get_api_key_by_value(x_api_key)
    if api_key_row is None:
        raise HTTPException(status_code=401, detail="Geçersiz veya devre dışı API anahtarı.")
    _check_rate_limit(x_api_key, api_key_row.tier)
    record_api_key_usage(x_api_key)
    return api_key_row


def _ensure_utc(value: datetime | None) -> datetime | None:
    """Query param'dan gelen bir `datetime` timezone bilgisi İÇERMİYORSA
    (ör. "2026-07-01T00:00:00") UTC varsayar - Postgres'teki `timestamptz`
    kolonuyla (bkz. NewsRecord.first_seen_at) karşılaştırırken tip
    uyuşmazlığı hatasını önler."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Yanıt modelleri (SADECE genel/güvenli alanlar - bkz. modül docstring'i)
# --------------------------------------------------------------------------


class LinkOut(BaseModel):
    source: str
    link: str


class NewsOut(BaseModel):
    id: str
    title: str
    title_tr: str | None = None
    summary: str | None = None
    key_points: list[str] = []
    importance_score: int | None = None
    importance_reason: str | None = None
    sources: list[str] = []
    sector: list[str] = []
    regions: list[str] = []
    sentiment: str | None = None
    market_impact: str | None = None
    top_category: str | None = None
    company_ticker: str | None = None
    published_at: str | None = None
    first_seen_at: str | None = None
    links: list[LinkOut] = []


def _record_to_public(record: NewsRecord) -> NewsOut:
    return NewsOut(
        id=record.group_key,
        title=record.title,
        title_tr=record.title_tr,
        summary=record.summary,
        key_points=record.key_points_list(),
        importance_score=record.importance_score,
        importance_reason=record.importance_reason,
        sources=[s.strip() for s in (record.sources or "").split(",") if s.strip()],
        sector=record.sectors_list(),
        regions=record.regions_list(),
        sentiment=record.sentiment,
        market_impact=record.market_impact,
        top_category=record.top_category,
        company_ticker=record.company_ticker,
        published_at=record.published_at.isoformat() if record.published_at else None,
        first_seen_at=record.first_seen_at.isoformat() if record.first_seen_at else None,
        links=[LinkOut(source=item.get("source", ""), link=item.get("link", "")) for item in record.links_list()],
    )


class NewsPageOut(BaseModel):
    page: int
    page_size: int
    total: int
    has_next: bool
    items: list[NewsOut]


class CompanyQuoteOut(BaseModel):
    symbol: str
    price: float
    change_pct: float
    currency: str
    is_open: bool
    is_delayed: bool
    delay_minutes: int


class CompanyNewsOut(BaseModel):
    title: str
    title_tr: str | None = None
    summary: str | None = None
    sources: str
    published_at: str | None = None
    link: str | None = None


class CompanyDetailOut(BaseModel):
    ticker: str
    quote: CompanyQuoteOut | None = None
    history: list[dict[str, Any]] = []
    news: list[CompanyNewsOut] = []


class EconomicCalendarEventOut(BaseModel):
    id: int
    event_time: str
    country_code: str
    country_name: str
    event_name: str
    importance: int
    previous_value: str | None = None
    actual_value: str | None = None
    forecast_value: str | None = None
    reference_period: str | None = None


class EconomicCalendarPageOut(BaseModel):
    events: list[EconomicCalendarEventOut]


# --------------------------------------------------------------------------
# Endpoint'ler
# --------------------------------------------------------------------------


@router.get("/news", response_model=NewsPageOut, summary="Haberleri listele (filtrelenebilir, sayfalanabilir)")
def list_news(
    source: str | None = Query(default=None, description="Kaynak adına göre filtrele (ör. 'Bloomberg HT')"),
    sector: str | None = Query(default=None, description="Sektör slug'ı (ör. 'finans', 'enerji', 'teknoloji')"),
    region: str | None = Query(default=None, description="Bölge slug'ı: turkiye, abd, avrupa, asya, diger"),
    sentiment: str | None = Query(default=None, description="Duygu etiketi: pozitif, negatif, notr"),
    min_importance: int | None = Query(default=None, ge=1, le=5, description="En az bu önem skoruna sahip haberler"),
    date_from: datetime | None = Query(default=None, description="Bu tarihten SONRAKİ haberler (ISO 8601, ör. 2026-07-01T00:00:00Z)"),
    date_to: datetime | None = Query(default=None, description="Bu tarihten ÖNCEKİ haberler (ISO 8601)"),
    page: int = Query(default=1, ge=1, description="Sayfa numarası (1'den başlar)"),
    page_size: int = Query(default=20, ge=1, le=100, description="Sayfa başına kayıt (en fazla 100)"),
    api_key: ApiKey = Depends(require_api_key),
) -> NewsPageOut:
    records, total = get_public_news_page(
        page=page,
        page_size=page_size,
        source=source,
        sector=sector,
        region=region,
        sentiment=sentiment,
        min_importance=min_importance,
        date_from=_ensure_utc(date_from),
        date_to=_ensure_utc(date_to),
    )
    return NewsPageOut(
        page=page,
        page_size=page_size,
        total=total,
        has_next=(page * page_size) < total,
        items=[_record_to_public(r) for r in records],
    )


@router.get("/news/{news_id}", response_model=NewsOut, summary="Tek bir haberin detayı")
def get_news_detail(news_id: str, api_key: ApiKey = Depends(require_api_key)) -> NewsOut:
    record = get_public_news_by_id(news_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Haber bulunamadı.")
    return _record_to_public(record)


@router.get("/companies/{ticker}", response_model=CompanyDetailOut, summary="Şirket güncel fiyatı + ilgili son haberler")
async def get_company(
    ticker: str,
    exchange: str = Query(default="NASDAQ", description="Borsa kodu (ör. NASDAQ, NYSE, BIST, LSE)"),
    api_key: ApiKey = Depends(require_api_key),
) -> CompanyDetailOut:
    company_ticker = f"{exchange.strip().upper()}: {ticker.strip().upper()}"
    detail = await get_ticker_detail(company_ticker)

    since = datetime.now(timezone.utc) - timedelta(days=30)
    records = get_records_by_company_ticker(company_ticker, limit=10, since=since)
    news = [
        CompanyNewsOut(
            title=r.title,
            title_tr=r.title_tr,
            summary=(r.summary or "")[:280],
            sources=r.sources,
            published_at=r.first_seen_at.isoformat() if r.first_seen_at else None,
            link=(r.links_list()[0]["link"] if r.links_list() else None),
        )
        for r in records
    ]

    quote_out = None
    if detail and detail.get("quote"):
        q = detail["quote"]
        quote_out = CompanyQuoteOut(
            symbol=q["symbol"],
            price=q["price"],
            change_pct=q["change_pct"],
            currency=q["currency"],
            is_open=q["is_open"],
            is_delayed=q["is_delayed"],
            delay_minutes=q["delay_minutes"],
        )

    return CompanyDetailOut(
        ticker=company_ticker,
        quote=quote_out,
        history=detail["history"] if detail else [],
        news=news,
    )


@router.get("/market-data", summary="Güncel piyasa şeridi (döviz/emtia/endeks)")
async def market_data_v1(api_key: ApiKey = Depends(require_api_key)) -> list[dict[str, Any]]:
    """Dashboard'daki piyasa şeridiyle AYNI, sürekli arka planda taze
    tutulan önbellekten okur (bkz. src/web/market_data.py >
    get_market_snapshot) - bu uç nokta EK bir Yahoo Finance isteği
    TETİKLEMEZ, ek yük getirmez."""
    return await get_market_snapshot()


@router.get("/commodities", summary="Haftalık emtia raporu (fiyat + LLM analizi + ilgili şirketler)")
def commodities_v1(api_key: ApiKey = Depends(require_api_key)) -> dict[str, Any]:
    """Dashboard'daki emtia paneliyle AYNI önbellekten okur (bkz.
    src/commodity_report.py > get_commodity_dashboard_data) - EK bir LLM
    çağrısı YAPMAZ."""
    return get_commodity_dashboard_data()


@router.get("/sectors/heatmap", summary="Sektör ısı haritası (son 24 saat)")
def sectors_heatmap_v1(api_key: ApiKey = Depends(require_api_key)) -> list[dict[str, Any]]:
    """Dashboard'daki ısı haritasıyla AYNI önbellekten okur. Fonksiyon
    ÇAĞRI ANINDA (modül düzeyinde DEĞİL) import edilir - src/web/app.py
    da bu router'ı `include_router` ile eklediğinden, modül düzeyinde bir
    import DÖNGÜSEL BAĞIMLILIK (circular import) yaratırdı."""
    from src.web.app import _get_cached_sector_heatmap

    return _get_cached_sector_heatmap()


def _parse_importance_param(importance: str | None) -> set[int]:
    """"2,3" -> {2, 3}. Boş/geçersiz girdi (ör. "abc") sessizce varsayılana
    ({2, 3}) düşer - kullanıcı kararı: "Kullanıcı özel olarak başka bir
    importance istemediği sürece 1★ dönme" (2026-08-19)."""
    if not importance:
        return {2, 3}
    values = {int(p) for p in importance.split(",") if p.strip().isdigit() and int(p) in (1, 2, 3)}
    return values or {2, 3}


@router.get(
    "/economic-calendar",
    response_model=EconomicCalendarPageOut,
    summary="Ekonomik takvim (TR/US/DE/JP/CN/GB/EA, ülke/tarih/önem filtrelenebilir)",
)
def economic_calendar_v1(
    country: str | None = Query(default=None, description="Ülke/bölge kodu: TR, US, DE, JP, CN, GB, EA (EA = Euro Bölgesi, DE'den AYRI)"),
    date: str | None = Query(default=None, description="Tek gün, YYYY-MM-DD (Türkiye yerel günü) - verilirse start_date/end_date yok sayılır"),
    start_date: str | None = Query(default=None, description="Tarih aralığı başlangıcı, YYYY-MM-DD (Türkiye yerel günü)"),
    end_date: str | None = Query(default=None, description="Tarih aralığı bitişi (dahil), YYYY-MM-DD (Türkiye yerel günü)"),
    importance: str | None = Query(default=None, description="Virgülle ayrılmış önem seviyeleri, ör. '2,3' veya '1'. Varsayılan: 2,3 (1 yıldız varsayılan olarak DÖNMEZ)"),
    api_key: ApiKey = Depends(require_api_key),
) -> EconomicCalendarPageOut:
    """`src/db.py > get_calendar_events_filtered` üzerinden mevcut Trading
    Economics tabanlı `economic_calendar_events` tablosunu okur - EK bir
    dış istek TETİKLEMEZ (veri zaten worker.py > _economic_calendar_refresh_job/
    _economic_calendar_watch_job tarafından periyodik güncellenir, bkz.
    modül docstring'i)."""
    try:
        events = get_calendar_events_filtered(
            country_code=country,
            start_date=date or start_date,
            end_date=None if date else end_date,
            importance=_parse_importance_param(importance),
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Geçersiz tarih formatı - YYYY-MM-DD kullanın.")

    return EconomicCalendarPageOut(
        events=[
            EconomicCalendarEventOut(
                id=e.id,
                event_time=e.event_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                country_code=e.country_code,
                country_name=e.country_name,
                event_name=e.event_name,
                importance=e.importance,
                previous_value=e.previous_value,
                actual_value=e.actual_value,
                forecast_value=e.forecast_value,
                reference_period=e.reference_period,
            )
            for e in events
        ]
    )
