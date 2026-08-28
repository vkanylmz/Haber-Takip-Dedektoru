"""BIST şirketleri için Fintables MCP'den çekilen finansal tablo/oran
önbelleği - "Finansal Tablolar + Oran Analizi" özelliği (2026-08-26, kullanıcı
isteği).

MİMARİ KISITI (ÖNEMLİ): Fintables MCP tool'ları (`veri_sorgula` vb.) SADECE
bir Claude Code/MCP client oturumu içinde çağrılabilir - `src/web/app.py`
(Render'da bağımsız çalışan FastAPI süreci) veya `worker.py` (APScheduler,
saf Python) bunlara runtime'da HİÇ erişemez, çünkü bunlar genel bir REST
endpoint DEĞİL. Bu yüzden bu modül İKİ AYRI role bölünür:
  1. Bu dosyadaki fonksiyonlar (`get_financial_watchlist_tickers`,
     `save_financial_snapshot`, `load_financial_snapshot`) SAF PYTHON'dur,
     hiçbir MCP çağrısı içermez - watchlist'i DB'den okur, sonucu DB'ye
     (AppState) yazar/okur.
  2. Gerçek Fintables sorgusu, haftalık çalışan bir Claude Code cloud
     agent'ı (bkz. `schedule` skill ile kurulan "fintables-financials-sync"
     routine'i) tarafından yapılır: o agent `get_financial_watchlist_tickers`
     ile hangi ticker'ların tazelenmesi gerektiğini öğrenir, her biri için
     Fintables MCP'yi CANLI sorgular, sonucu bu modüldeki
     `save_financial_snapshot`'ın beklediği şekle indirger ve DB'ye yazar.

Önbellek AYRI bir SQLAlchemy tablosu/migrasyon GEREKTİRMEZ - mevcut
`AppState` key-value tablosu (bkz. src/db.py, docstring'inde zaten "yeni bir
tablo/migrasyon gerekmeden, sadece yeni bir key ile" kullanılabileceği
belirtilmiş) `key=f"financials:{TICKER}"` ile kullanılır.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from src.db import AppState, get_app_state, get_session, set_app_state

logger = logging.getLogger(__name__)

_STATE_KEY_PREFIX = "financials:"

# `company_ticker` alanı "BIST: THYAO" ya da (KAP'ın stockCodes'u birden
# fazla kod verdiğinde) "BIST: YKB, BIST: YKBNK" formatında olabilir (bkz.
# src/summarizer.py > _kap_company_ticker_from_stock_codes). Sadece BIST
# kodları ilgilendiriyor - NASDAQ/NYSE/FX gibi diğer borsalar bu özelliğin
# kapsamı DIŞINDA.
_BIST_TICKER_RE = re.compile(r"BIST:\s*([A-Z0-9]{2,10})")


def financials_state_key(ticker: str) -> str:
    return f"{_STATE_KEY_PREFIX}{ticker.strip().upper()}"


def extract_bist_tickers(company_ticker: str | None) -> list[str]:
    """`company_ticker` alanından ("BIST: THYAO" formatı) çıplak BIST
    kodlarını çıkarır. BIST-dışı/boş değerlerde boş liste döner."""
    if not company_ticker:
        return []
    return _BIST_TICKER_RE.findall(company_ticker.upper())


def first_bist_ticker(company_ticker: str | None) -> str | None:
    tickers = extract_bist_tickers(company_ticker)
    return tickers[0] if tickers else None


def get_financial_watchlist_tickers(days: int = 14) -> list[str]:
    """Son `days` gün içinde `kap_category='finansal_rapor'` olan KAP
    haberlerindeki BIST ticker'larını (tekilleştirilmiş, alfabetik) döner -
    haftalık sync job'ının "hangi şirketleri tazelemem gerekiyor" sorusunun
    cevabı. Bir finansal rapor haberi geldiği an, o şirketin Fintables
    verisi de KAP'a yeni yayınlanmış demektir (bkz. modül docstring'i)."""
    from src.db import NewsRecord  # döngüsel import'tan kaçınmak için burada

    since = datetime.now(timezone.utc) - timedelta(days=days)
    tickers: set[str] = set()
    with get_session() as session:
        rows = (
            session.query(NewsRecord.company_ticker)
            .filter(NewsRecord.kap_category == "finansal_rapor")
            .filter(NewsRecord.first_seen_at >= since)
            .all()
        )
    for (company_ticker,) in rows:
        tickers.update(extract_bist_tickers(company_ticker))
    return sorted(tickers)


def save_financial_snapshot(
    ticker: str,
    donemler: list[dict[str, Any]],
    oranlar: dict[str, dict[str, float]],
    bilanco_ozet: dict[str, Any],
    sablon: str = "default",
) -> dict[str, Any]:
    """Fintables'tan çekilip indirgenmiş bir şirket finansal özetini
    AppState'e yazar (upsert). Şekiller:

    `donemler`: son ~5 çeyreğin listesi, en yeni İLK sırada, her biri
        {"yil": int, "ay": int (3/6/9/12), "satis_geliri_ceyreklik": int|None,
         "favok_ceyreklik": int|None, "net_kar_ceyreklik": int|None} - TRY,
        Fintables'ın `try_ceyreklik` kolonundan.
    `oranlar`: kategori -> {oran_adı: değer} - bkz. Fintables
        `finansal_tablolar` skill'i > "Finansal Oranlar" (Likidite/Kaldıraç/
        Faaliyet Etkinlik/Karlılık Oranları kategorileri).
    `bilanco_ozet`: son dönem {"toplam_varlik": int|None,
        "toplam_ozkaynak": int|None, "net_borc": int|None} - TRY.

    Döner: DB'ye yazılan tam payload (fetched_at DAHİL)."""
    payload = {
        "ticker": ticker.strip().upper(),
        "sablon": sablon,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "donemler": donemler,
        "oranlar": oranlar,
        "bilanco_ozet": bilanco_ozet,
    }
    set_app_state(financials_state_key(ticker), payload)
    logger.info("Finansal tablo önbelleği güncellendi: %s (%d dönem).", ticker, len(donemler))
    return payload


def load_financial_snapshot(ticker: str) -> dict[str, Any] | None:
    """Bir ticker için önbellekteki finansal özeti döner. Hiç
    tazelenmemişse (henüz sync job'ı bu ticker'ı görmediyse) None döner -
    çağıran taraf (bkz. src/web/app.py > company_profile_page) bunu "henüz
    veri yok" olarak yorumlar, ASLA canlı bir Fintables/MCP çağrısı DENEMEZ
    (bkz. modül docstring'i - web süreci MCP'ye zaten erişemiyor)."""
    if not ticker:
        return None
    return get_app_state(financials_state_key(ticker))


def list_cached_financial_tickers() -> list[str]:
    """Önbellekte (AppState) en az bir finansal tablo kaydı bulunan tüm
    ticker'ları döner - admin/tanılama amaçlı (bkz. manuel ilk çalıştırma
    doğrulaması)."""
    with get_session() as session:
        rows = (
            session.query(AppState.key)
            .filter(AppState.key.like(f"{_STATE_KEY_PREFIX}%"))
            .all()
        )
    return sorted(key[len(_STATE_KEY_PREFIX):] for (key,) in rows)
