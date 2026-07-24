"""Hafta içi her sabah 09:00'da (Europe/Istanbul) çalışan GÜNLÜK ÖZET raporu.

Bir önceki 24 saatte toplanan TÜM haberler arasından, sadece 1-5 önem
skoruna göre sıralamak yerine Gemini/Claude'a TEK bir ek çağrı ile
"gerçekten piyasa/yatırımcı için değerli, aksiyon alınabilir" 5-10 haberi
seçtirir (bkz. Summarizer.select_daily_highlights, src/summarizer.py >
DAILY_DIGEST_SYSTEM_PROMPT) ve sonucu subscribers tablosundaki TÜM abonelere
Telegram üzerinden gönderir.

Bu özellik, mevcut ANLIK bildirim akışına (src/notifier.py - önem skoru
eşiğini geçen haberler özetlenir özetlenmez tek tek gönderilir) HİÇ
dokunmaz; tamamen ayrı, ek bir zamanlanmış görevdir (bkz. worker.py >
"gunluk_ozet" cron job).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import get_summarizer_api_key
from src.db import NewsRecord, get_all_subscriber_chat_ids, get_records_since
from src.notifier import send_telegram_message_to_chat_ids
from src.summarizer import Summarizer
from src.telegram_format import chunk_messages, format_news_block

logger = logging.getLogger(__name__)

_MIN_HIGHLIGHTS_FOR_NO_NOTE = 5
_FALLBACK_MAX_HIGHLIGHTS = 10


def _fallback_top_by_score(records: list[NewsRecord]) -> list[tuple[NewsRecord, str]]:
    """LLM seçimi hiç yapılamadığında (API anahtarı yok, çağrı başarısız vb.)
    kullanılan basit geri dönüş: önem skoruna göre ilk 10."""
    scored = sorted(records, key=lambda r: (r.importance_score or 0), reverse=True)
    return [(r, "") for r in scored[:_FALLBACK_MAX_HIGHLIGHTS]]


def _select_highlights(config: dict[str, Any], records: list[NewsRecord]) -> list[tuple[NewsRecord, str]]:
    summarizer_cfg = config["summarizer"]
    try:
        provider, api_key = get_summarizer_api_key(summarizer_cfg)
    except RuntimeError as exc:
        logger.warning(
            "%s Günlük özet için LLM seçimi yapılamıyor, önem skoruna göre ilk %d haber kullanılacak.",
            exc,
            _FALLBACK_MAX_HIGHLIGHTS,
        )
        return _fallback_top_by_score(records)

    summarizer = Summarizer(summarizer_cfg, api_key=api_key, provider=provider)
    selections = summarizer.select_daily_highlights(records)
    if not selections:
        logger.warning(
            "Günlük özet için LLM seçimi boş döndü, önem skoruna göre ilk %d haber kullanılacak.",
            _FALLBACK_MAX_HIGHLIGHTS,
        )
        return _fallback_top_by_score(records)

    return [(records[s["index"]], s.get("reason", "")) for s in selections]


def send_daily_digest(config: dict[str, Any]) -> None:
    """Günlük özeti hazırlar ve TÜM abonelere gönderir. Herhangi bir hata
    durumunda exception fırlatmaz (çağıran taraf, ör. worker.py'deki cron
    job, sadece loglar - bir sonraki günün özeti etkilenmez)."""
    telegram_cfg = config.get("telegram", {})
    if not telegram_cfg.get("enabled", True):
        logger.info("config.yaml > telegram.enabled=false, günlük özet atlanıyor.")
        return

    chat_ids = get_all_subscriber_chat_ids()
    if not chat_ids:
        logger.info("Hiç Telegram abonesi yok, günlük özet atlanıyor.")
        return

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    records = get_records_since(since)
    if not records:
        logger.info("Son 24 saatte hiç haber toplanmamış, günlük özet atlanıyor.")
        return

    try:
        highlights = _select_highlights(config, records)
    except Exception:  # noqa: BLE001 - günlük özet asla tüm uygulamayı etkilemesin
        logger.exception("Günlük özet için haber seçimi başarısız oldu, önem skoruna göre geri dönülüyor.")
        highlights = _fallback_top_by_score(records)

    if not highlights:
        logger.info("Günlük özet için seçilecek haber bulunamadı, gönderim atlanıyor.")
        return

    note = None
    if len(highlights) < _MIN_HIGHLIGHTS_FOR_NO_NOTE:
        note = "Not: Bugün az sayıda önemli haber vardı."

    header = f"☀️ <b>Günaydın! Dünden bugüne önemli {len(highlights)} haber:</b>"
    if note:
        header += f"\n<i>{note}</i>"

    blocks = [format_news_block(record, extra_reason=reason or None) for record, reason in highlights]
    messages = chunk_messages(header, blocks)

    total_sent = 0
    for message in messages:
        total_sent += send_telegram_message_to_chat_ids(chat_ids, message)

    logger.info(
        "Günlük özet gönderildi: %d haber, %d/%d aboneye (%d parça mesaj).",
        len(highlights),
        total_sent,
        len(chat_ids) * len(messages) if messages else 0,
        len(messages),
    )
