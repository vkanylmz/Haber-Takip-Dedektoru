"""Analiz sayfası (/analiz, bkz. src/web/app.py > company_profile_page) veri
katmanı: kullanıcı bir şirket adı/ticker VEYA genel bir finansal enstrüman
(altın, tahvil, endeks vb.) girdiğinde, son 30 günün ilgili haberlerini,
LLM tarafından üretilmiş kısa bir genel görünüm özetini ve (2026-08-26,
kullanıcı isteği: "Analiz" sayfasına dönüşüm) uygun bir grafik sembolünü
döner.

Mevcut anahtar kelime takibi (/takip, src/keyword_alerts.py) BAĞLI bildirim
akışından tamamen ayrıdır - bu, web dashboard'da isteğe bağlı/anlık bir
sorgudur, hiçbir Telegram bildirimi veya arka plan görevi tetiklemez.

ŞİRKET vs GENEL ENSTRÜMAN ayrımı (`mode` alanı - "company"/"instrument"/"none"):
İki BAĞIMSIZ sinyal "şirket" kabul ettirir (ikisi de haberden BAĞIMSIZ/daha
güvenilir olduğundan LLM'in serbest metin tahminine tercih edilir):
  1. Sorgunun kendisi doğrudan bir BIST ticker'ıysa VE o ticker için
     Fintables önbelleğinde (bkz. src/fintables_financials.py) veri varsa -
     kullanıcı hiç haber olmasa bile "GMTAS" yazıp doğrudan şirket sayfasına
     ulaşabilsin diye (2026-08-26, kullanıcı isteği).
  2. Bulunan haberlerin `company_ticker` alanından bir BIST kodu
     çözümlenebiliyorsa (mevcut davranış, DEĞİŞMEDİ).
Şirket DEĞİLSE VE en az bir haber bulunduysa, LLM'e (outlook özetiyle AYNI
tek çağrıda - bkz. summarizer.py > COMPANY_PROFILE_SYSTEM_PROMPT) sorgunun
en uygun TradingView sembolünü de sorulur, `src/tradingview.py >
validate_symbol` ile GERÇEKTEN var olduğu doğrulanmadan ASLA gösterilmez
(uydurma sembol riski böyle engellenir). Ne şirket eşleşmesi ne haber varsa
(özetlenecek hiçbir şey yok) `mode="none"` döner - sayfa "sonuç bulunamadı"
gösterir.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import get_summarizer_api_key
from src.db import NewsRecord, get_recent_records, get_session
from src.fintables_financials import first_bist_ticker, load_financial_snapshot
from src.summarizer import Summarizer
from src.tradingview import validate_symbol

logger = logging.getLogger(__name__)

_PROFILE_WINDOW_DAYS = 30
_MAX_RECORDS_FOR_LLM = 40  # LLM prompt'unun aşırı büyümesini önlemek için üst sınır


def _empty_result(query: str) -> dict[str, Any]:
    return {
        "query": query,
        "records": [],
        "summary": None,
        "mode": "none",
        "financials_ticker": None,
        "trading_view_symbol": None,
        "trading_view_symbol_valid": None,
    }


def resolve_financials_ticker(query: str) -> tuple[str | None, list[NewsRecord]]:
    """`query`'den (ticker VEYA şirket adı) Fintables önbelleğinde veri
    bulunan bir BIST ticker'ı çözümler - bkz. modül docstring'i > "ŞİRKET vs
    GENEL ENSTRÜMAN" ayrımındaki İKİ sinyal. BİLEREK HİÇBİR LLM ÇAĞRISI
    YAPMAZ (get_company_profile'ın aksine) - `src/web/app.py > finansallar_page`
    gibi sadece finansal özet gösteren, haber/outlook özeti GEREKMEYEN
    çağıranlar için. `records` de döner (aynı sorguyu tekrar
    ÇALIŞTIRMASIN diye - get_company_profile bu fonksiyonu kullanır)."""
    query = query.strip()
    if not query:
        return None, []

    since = datetime.now(timezone.utc) - timedelta(days=_PROFILE_WINDOW_DAYS)
    with get_session() as session:
        records = get_recent_records(session, limit=200, search_query=query, since=since)

    looks_like_ticker = query.isalpha() and query.isupper() and 2 <= len(query) <= 10
    if looks_like_ticker and load_financial_snapshot(query) is not None:
        return query, records

    # Bkz. modül docstring'i - tesadüfi tek isabetleri elemek için bir
    # ticker'ın AYNI aramada en az 2 farklı kayıtta çözümlenmesi ARANIR.
    ticker_counts: dict[str, int] = {}
    for r in records:
        t = first_bist_ticker(r.company_ticker)
        if t:
            ticker_counts[t] = ticker_counts.get(t, 0) + 1
    repeated = [t for t, count in ticker_counts.items() if count >= 2]
    if repeated:
        return max(repeated, key=lambda t: ticker_counts[t]), records
    return None, records


def get_company_profile(query: str, config: dict[str, Any]) -> dict[str, Any]:
    """Verilen arama terimi için Analiz sayfasının tüm verisini döner.

    Döner: {
        "query": str, "records": list[NewsRecord], "summary": str | None,
        "mode": "company" | "instrument" | "none",
        "financials_ticker": str | None,  # SADECE mode="company"
        "trading_view_symbol": str | None,  # "BORSA:SEMBOL" - company modunda
            # OTORİTER "BIST:{ticker}" (LLM'e SORULMAZ), instrument modunda
            # LLM tahmini + validate_symbol ile DOĞRULANMIŞ.
        "trading_view_symbol_valid": bool | None,  # SADECE mode="instrument"
            # anlamlı (company modunda BIST zaten embed'de çalışmadığından
            # doğrulamaya gerek yok - bkz. src/web/app.py > chart render
            # kararı, None kalır).
    }
    `summary` None ise (API anahtarı yok, LLM çağrısı başarısız oldu, veya
    henüz üretilmedi) çağıran taraf sadece haber listesini/grafiği gösterir -
    bu fonksiyon hiçbir zaman exception fırlatmaz."""
    query = query.strip()
    if not query:
        return _empty_result("")

    # --- Şirket tespiti (bkz. modül docstring'i > iki sinyal, artık
    # resolve_financials_ticker'da - src/web/app.py > finansallar_page de
    # AYNI fonksiyonu kullanır, LLM çağrısı OLMADAN). ---
    financials_ticker, records = resolve_financials_ticker(query)

    if financials_ticker:
        summary = _generate_outlook_summary(query, records, config) if records else None
        return {
            "query": query,
            "records": records,
            "summary": summary,
            "mode": "company",
            "financials_ticker": financials_ticker,
            "trading_view_symbol": f"BIST:{financials_ticker}",
            "trading_view_symbol_valid": None,
        }

    # --- Şirket değil: haber yoksa özetlenecek/sembol aranacak hiçbir şey yok ---
    if not records:
        return _empty_result(query)

    # --- Genel enstrüman (bkz. modül docstring'i) ---
    summary, tv_symbol = _generate_outlook_summary_and_symbol(query, records, config)
    tv_valid = validate_symbol(tv_symbol) if tv_symbol else None

    return {
        "query": query,
        "records": records,
        "summary": summary,
        "mode": "instrument" if (tv_symbol and tv_valid) else "none",
        "financials_ticker": None,
        "trading_view_symbol": tv_symbol if tv_valid else None,
        "trading_view_symbol_valid": tv_valid,
    }


def _generate_outlook_summary(query: str, records: list[NewsRecord], config: dict[str, Any]) -> str | None:
    """Şirket modu: sadece özet metni gerekir (sembol zaten OTORİTER
    `BIST:{ticker}` - LLM'e sorulmaz, bkz. get_company_profile)."""
    summary, _symbol = _generate_outlook_summary_and_symbol(query, records, config)
    return summary


def _generate_outlook_summary_and_symbol(
    query: str, records: list[NewsRecord], config: dict[str, Any]
) -> tuple[str | None, str | None]:
    summarizer_cfg = config.get("summarizer", {})
    try:
        provider, api_key = get_summarizer_api_key(summarizer_cfg)
    except RuntimeError as exc:
        logger.info("%s Analiz özeti atlanıyor, yalnızca haber listesi gösterilecek.", exc)
        return None, None

    try:
        output_dir = config.get("app", {}).get("output_dir", "data")
        summarizer = Summarizer(summarizer_cfg, api_key=api_key, provider=provider, output_dir=output_dir)
        summary, tv_symbol = summarizer.summarize_company_profile(query, records[:_MAX_RECORDS_FOR_LLM])
    except Exception:  # noqa: BLE001 - özet başarısız olursa sadece haber listesi gösterilsin
        logger.exception("Analiz özeti üretilirken beklenmeyen hata: %s", query)
        return None, None

    return (summary or None), (tv_symbol or None)
