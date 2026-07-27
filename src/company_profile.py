"""Şirket/hisse bazlı otomatik profil: web dashboard'da kullanıcı bir şirket
adı girdiğinde (ör. "Tesla"), o şirketle ilgili son 30 günün TÜM haberlerini
(başlık + özet içinde, case-insensitive basit LIKE arama - bkz.
src/db.py > get_recent_records > search_query, arama kutusuyla AYNI mantık)
listeler ve üstte Gemini/Claude ile üretilmiş kısa bir otomatik özet gösterir.

Mevcut anahtar kelime takibi (/takip, src/keyword_alerts.py) BAĞLI bildirim
akışından tamamen ayrıdır - bu, web dashboard'da isteğe bağlı/anlık bir
sorgudur, hiçbir Telegram bildirimi veya arka plan görevi tetiklemez.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import get_summarizer_api_key
from src.db import NewsRecord, get_recent_records, get_session
from src.summarizer import Summarizer

logger = logging.getLogger(__name__)

_PROFILE_WINDOW_DAYS = 30
_MAX_RECORDS_FOR_LLM = 40  # LLM prompt'unun aşırı büyümesini önlemek için üst sınır


def get_company_profile(company_name: str, config: dict[str, Any]) -> dict[str, Any]:
    """Verilen şirket adı için son 30 günün haberlerini ve (mümkünse) LLM
    tarafından üretilmiş bir genel görünüm özetini döner.

    Döner: {"company": str, "records": list[NewsRecord], "summary": str | None}
    `summary` None ise (API anahtarı yok, LLM çağrısı başarısız oldu vb.)
    dashboard sadece haber listesini gösterir - hiçbir zaman exception
    fırlatmaz."""
    company_name = company_name.strip()
    if not company_name:
        return {"company": "", "records": [], "summary": None}

    since = datetime.now(timezone.utc) - timedelta(days=_PROFILE_WINDOW_DAYS)
    with get_session() as session:
        records = get_recent_records(
            session,
            limit=200,
            search_query=company_name,
            since=since,
        )

    if not records:
        return {"company": company_name, "records": [], "summary": None}

    summary = _generate_outlook_summary(company_name, records, config)
    return {"company": company_name, "records": records, "summary": summary}


def _generate_outlook_summary(company_name: str, records: list[NewsRecord], config: dict[str, Any]) -> str | None:
    summarizer_cfg = config.get("summarizer", {})
    try:
        provider, api_key = get_summarizer_api_key(summarizer_cfg)
    except RuntimeError as exc:
        logger.info("%s Şirket profili özeti atlanıyor, yalnızca haber listesi gösterilecek.", exc)
        return None

    try:
        summarizer = Summarizer(summarizer_cfg, api_key=api_key, provider=provider)
        summary = summarizer.summarize_company_profile(company_name, records[:_MAX_RECORDS_FOR_LLM])
    except Exception:  # noqa: BLE001 - profil özeti başarısız olursa sadece haber listesi gösterilsin
        logger.exception("Şirket profili özeti üretilirken beklenmeyen hata: %s", company_name)
        return None

    return summary or None
