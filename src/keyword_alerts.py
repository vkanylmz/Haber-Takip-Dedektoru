"""Kullanıcı bazlı anahtar kelime/varlık takibi bildirimleri.

Kullanıcılar Telegram'da /takip <kelime> yazarak kendi takip listelerine
ekledikleri kelimeleri (ör. "Tesla", "TCMB", "Fed") takip eder. Her yeni
haber özetlenip önem skoru + bölge + duygu etiketi hesaplandıktan sonra, bu
modül o haberin başlığı/özeti içinde geçen anahtar kelimeleri kontrol eder
ve eşleşme varsa İLGİLİ KULLANICIYA ÖZEL bir bildirim gönderir.

ÖNEMLİ: Bu akış, mevcut "önem skoru eşiğini geçince TÜM abonelere gönder"
akışından (bkz. src/notifier.py) TAMAMEN BAĞIMSIZ çalışır - önem skoru
eşiği geçmese, hatta hiç skorlanamamış olsa bile, takip edilen kelime
geçiyorsa bildirim gider. Aynı (kullanıcı, haber) çifti için birden fazla
bildirim gitmemesi `src/db.py > KeywordNotification` (was_keyword_notified/
mark_keyword_notified) ile garanti edilir.
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

from src.db import get_all_keyword_subscriptions, mark_keyword_notified, was_keyword_notified
from src.notifier import send_telegram_message_to_chat_ids
from src.telegram_format import format_news_block

if TYPE_CHECKING:
    from src.db import NewsRecord

logger = logging.getLogger(__name__)


def _format_keyword_alert(keyword: str, record: "NewsRecord") -> str:
    header = f"🔔 <b>'{html.escape(keyword)}'</b> ile ilgili yeni haber:"
    return f"{header}\n\n{format_news_block(record)}"


def check_keyword_matches_and_notify(record: "NewsRecord") -> None:
    """Verilen (yeni özetlenmiş/güncellenmiş) haberi, TÜM kullanıcıların
    takip ettiği kelimelerle karşılaştırır; eşleşen ve daha önce
    bildirilmemiş her (kullanıcı, kelime) için ayrı bir bildirim gönderir.

    Basit, case-insensitive substring eşleşmesi kullanılır (başlık + özet
    içinde). Hiçbir durumda exception fırlatmaz - bir kullanıcıya gönderim
    hatası diğerlerini etkilemez.
    """
    subscriptions = get_all_keyword_subscriptions()
    if not subscriptions:
        return

    haystack = f"{record.title}\n{record.summary or ''}".lower()

    for sub in subscriptions:
        keyword_clean = sub.keyword.strip()
        if not keyword_clean or keyword_clean.lower() not in haystack:
            continue

        if was_keyword_notified(sub.chat_id, record.group_key):
            continue

        try:
            message = _format_keyword_alert(keyword_clean, record)
            sent = send_telegram_message_to_chat_ids([sub.chat_id], message)
        except Exception:  # noqa: BLE001 - bir kullanıcıya gönderim hatası diğerlerini etkilemesin
            logger.exception(
                "Anahtar kelime bildirimi gönderilemedi (chat_id=%s, kelime=%r, haber=%s).",
                sub.chat_id,
                keyword_clean,
                record.title,
            )
            continue

        if not sent:
            continue

        logger.info(
            "Anahtar kelime bildirimi gönderildi: chat_id=%s, kelime=%r, haber=%s",
            sub.chat_id,
            keyword_clean,
            record.title,
        )

        # "Bildirildi" işaretlemesi AYRI bir try/except içinde: bu adım
        # (ör. geçici bir veritabanı kilidi yüzünden) başarısız olursa bile
        # döngü diğer abonelere/kelimelere devam etmeli - tek bir kullanıcının
        # işaretleme hatası, geri kalan eşleşmelerin hiç kontrol edilmemesine
        # yol açmamalı. (Not: mesaj zaten gönderildiği için, bu durumda haber
        # bir sonraki tarama turunda TEKRAR gönderilebilir - db.py'deki
        # WAL/busy_timeout ayarları bu senaryoyu pratikte ortadan kaldırır,
        # ama tamamen imkansız kılmaz; bu yüzden ayrıca izole ediliyor.)
        try:
            mark_keyword_notified(sub.chat_id, record.group_key)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Anahtar kelime bildirimi 'gönderildi' olarak işaretlenemedi "
                "(chat_id=%s, kelime=%r, haber=%s) - mesaj zaten gitti, bir "
                "sonraki taramada tekrar gönderilebilir.",
                sub.chat_id,
                keyword_clean,
                record.title,
            )
