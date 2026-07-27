"""Haftalık (her Pazartesi 09:30) ve aylık (ayın ilk Pazartesi'si) TREND
RAPORU: bir önceki dönemde toplanan haberler arasından en çok haber alan
sektörler, genel duygu dağılımı (% pozitif/negatif/nötr), en yüksek önem
skorlu 3 haber ve bir önceki döneme göre haber hacmindeki değişim (%)
hesaplanır ve subscribers tablosundaki TÜM abonelere gönderilir.

Bu, mevcut ANLIK bildirim akışına (src/notifier.py) VE günlük özete
(src/daily_digest.py) HİÇ dokunmaz; tamamen ayrı, ek bir zamanlanmış
görevdir (bkz. worker.py > "haftalik_trend"/"aylik_trend" cron job'ları).
Web dashboard'daki küçük trend paneli de (bkz. src/web/app.py >
/api/trend-summary) AYNI `compute_trend_data` fonksiyonunu kullanır, böylece
Telegram raporu ile dashboard paneli hep tutarlı kalır.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from dateutil.relativedelta import relativedelta

from src.db import NewsRecord, get_all_subscriber_chat_ids, get_records_between
from src.notifier import send_telegram_message_to_chat_ids
from src.summarizer import SECTOR_LABELS

logger = logging.getLogger(__name__)

_TOP_SECTORS_COUNT = 5
_TOP_NEWS_COUNT = 3


def _period_bounds_weekly(now: datetime) -> tuple[datetime, datetime, datetime, datetime]:
    """Geçen haftanın (dün gece yarısına kadarki 7 gün değil, dün de dahil
    tam 7x24 saat) ve ondan önceki haftanın sınırlarını döner:
    (bu_hafta_baslangic, bu_hafta_bitis, gecen_hafta_baslangic, gecen_hafta_bitis)."""
    current_end = now
    current_start = now - timedelta(days=7)
    previous_start = current_start - timedelta(days=7)
    previous_end = current_start
    return current_start, current_end, previous_start, previous_end


def _period_bounds_monthly(now: datetime) -> tuple[datetime, datetime, datetime, datetime]:
    """Ayın ilk Pazartesi'si tetiklendiğinde, "bu ay"ın verisi neredeyse
    boştur (henüz birkaç gün geçmiştir) - bu yüzden aylık rapor, TAMAMLANMIŞ
    bir önceki takvim ayını ("geçen ay") ve ondan önceki tam ayı ("geçen ayın
    öncesi") karşılaştırır."""
    this_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_start = this_month_start - relativedelta(months=1)
    two_months_ago_start = last_month_start - relativedelta(months=1)
    return last_month_start, this_month_start, two_months_ago_start, last_month_start


def compute_trend_data(records_current: list[NewsRecord], records_previous: list[NewsRecord]) -> dict[str, Any]:
    """Verilen iki dönemin (bu dönem / önceki dönem) haber kayıtlarından
    haftalık/aylık trend raporunun/panelin ihtiyaç duyduğu tüm istatistikleri
    hesaplar. Telegram raporu VE web dashboard paneli AYNI bu fonksiyonu
    kullanır (bkz. modül docstring'i)."""
    sector_counts: dict[str, int] = {}
    pos = neg = notr = 0

    for r in records_current:
        for sector in r.sectors_list():
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if r.sentiment == "pozitif":
            pos += 1
        elif r.sentiment == "negatif":
            neg += 1
        elif r.sentiment == "notr":
            notr += 1

    total_count = len(records_current)
    sentiment_total = pos + neg + notr
    sentiment_pct = {
        "pozitif": round(100 * pos / sentiment_total, 1) if sentiment_total else 0.0,
        "negatif": round(100 * neg / sentiment_total, 1) if sentiment_total else 0.0,
        "notr": round(100 * notr / sentiment_total, 1) if sentiment_total else 0.0,
    }

    top_sectors = [
        {"sector": s, "label": SECTOR_LABELS.get(s, s), "count": c}
        for s, c in sorted(sector_counts.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_SECTORS_COUNT]
    ]

    top_news = sorted(
        (r for r in records_current if r.importance_score is not None),
        key=lambda r: r.importance_score,
        reverse=True,
    )[:_TOP_NEWS_COUNT]

    previous_count = len(records_previous)
    if previous_count > 0:
        volume_change_pct = round(100 * (total_count - previous_count) / previous_count, 1)
    elif total_count > 0:
        volume_change_pct = None  # önceki dönemde hiç veri yok - anlamlı bir % hesaplanamaz
    else:
        volume_change_pct = 0.0

    return {
        "total_count": total_count,
        "previous_count": previous_count,
        "volume_change_pct": volume_change_pct,
        "top_sectors": top_sectors,
        "sentiment_pct": sentiment_pct,
        "sentiment_counts": {"pozitif": pos, "negatif": neg, "notr": notr},
        "top_news": [
            {
                "title": r.title,
                "importance_score": r.importance_score,
                "sources": r.sources,
            }
            for r in top_news
        ],
    }


def _format_volume_change(pct: float | None) -> str:
    if pct is None:
        return "(önceki dönemle karşılaştırılamıyor - önceki dönemde veri yok)"
    arrow = "📈" if pct > 0 else ("📉" if pct < 0 else "➖")
    sign = "+" if pct > 0 else ""
    return f"{arrow} {sign}{pct}% (bir önceki döneme göre)"


def format_trend_message(data: dict[str, Any], title: str) -> str:
    lines = [f"<b>{html.escape(title)}</b>\n"]

    lines.append(f"📰 Toplam haber: <b>{data['total_count']}</b>  {_format_volume_change(data['volume_change_pct'])}")

    if data["top_sectors"]:
        lines.append("\n<b>🏭 En Çok Haber Alan Sektörler</b>")
        for item in data["top_sectors"]:
            lines.append(f"• {html.escape(item['label'])}: {item['count']} haber")
    else:
        lines.append("\n<b>🏭 Sektör verisi yok.</b>")

    sp = data["sentiment_pct"]
    lines.append(
        f"\n<b>📊 Genel Duygu Dağılımı</b>\n"
        f"📈 Pozitif: %{sp['pozitif']}  ·  📉 Negatif: %{sp['negatif']}  ·  ➖ Nötr: %{sp['notr']}"
    )

    if data["top_news"]:
        lines.append("\n<b>⭐ En Yüksek Önem Skorlu Haberler</b>")
        for item in data["top_news"]:
            score = item["importance_score"] if item["importance_score"] is not None else "?"
            lines.append(f"• [{score}/5] {html.escape(item['title'])} <i>({html.escape(item['sources'])})</i>")
    else:
        lines.append("\n<b>⭐ Bu dönem için öne çıkan haber bulunamadı.</b>")

    return "\n".join(lines)


def _send_trend_report(config: dict[str, Any], period_bounds_fn, title: str) -> None:
    """Ortak gönderim mantığı - haftalık ve aylık rapor arasındaki tek fark
    dönem sınırlarının nasıl hesaplandığı (bkz. `_period_bounds_weekly` /
    `_period_bounds_monthly`) ve başlık metni."""
    telegram_cfg = config.get("telegram", {})
    if not telegram_cfg.get("enabled", True):
        logger.info("config.yaml > telegram.enabled=false, %s atlanıyor.", title)
        return

    chat_ids = get_all_subscriber_chat_ids()
    if not chat_ids:
        logger.info("Hiç Telegram abonesi yok, %s atlanıyor.", title)
        return

    now = datetime.now(timezone.utc)
    current_start, current_end, previous_start, previous_end = period_bounds_fn(now)

    records_current = get_records_between(current_start, current_end)
    records_previous = get_records_between(previous_start, previous_end)

    if not records_current:
        logger.info("Bu dönemde hiç haber toplanmamış, %s atlanıyor.", title)
        return

    data = compute_trend_data(records_current, records_previous)
    message = format_trend_message(data, title)

    total_sent = send_telegram_message_to_chat_ids(chat_ids, message)
    logger.info("%s gönderildi: %d/%d aboneye.", title, total_sent, len(chat_ids))


def send_weekly_trend_report(config: dict[str, Any]) -> None:
    """Haftalık trend raporunu hazırlar ve TÜM abonelere gönderir. Herhangi
    bir hata durumunda exception fırlatmaz (çağıran taraf, ör. worker.py'deki
    cron job, sadece loglar - bir sonraki haftanın raporu etkilenmez)."""
    try:
        _send_trend_report(config, _period_bounds_weekly, "📅 Haftalık Trend Raporu")
    except Exception:  # noqa: BLE001 - trend raporu asla tüm uygulamayı etkilemesin
        logger.exception("Haftalık trend raporu gönderilirken beklenmeyen bir hata oluştu.")


def send_monthly_trend_report(config: dict[str, Any]) -> None:
    """Aylık trend raporunu hazırlar ve TÜM abonelere gönderir. Herhangi bir
    hata durumunda exception fırlatmaz (bkz. send_weekly_trend_report)."""
    try:
        _send_trend_report(config, _period_bounds_monthly, "🗓️ Aylık Trend Raporu")
    except Exception:  # noqa: BLE001
        logger.exception("Aylık trend raporu gönderilirken beklenmeyen bir hata oluştu.")


def get_dashboard_trend_summary(period: str = "weekly") -> dict[str, Any]:
    """Web dashboard'daki trend panelinin (bkz. src/web/app.py >
    /api/trend-summary) kullandığı veri - Telegram raporuyla AYNI
    `compute_trend_data` mantığını kullanır, tutarlılık garantidir."""
    now = datetime.now(timezone.utc)
    period_bounds_fn = _period_bounds_monthly if period == "monthly" else _period_bounds_weekly
    current_start, current_end, previous_start, previous_end = period_bounds_fn(now)

    records_current = get_records_between(current_start, current_end)
    records_previous = get_records_between(previous_start, previous_end)
    return compute_trend_data(records_current, records_previous)
