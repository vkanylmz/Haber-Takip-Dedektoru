"""Telegram üzerinden SADECE önem skoru eşiği geçen haberler için bildirim.

Davranış (gereksinim):
  - Bir haber grubunun `importance_score` değeri, config.yaml >
    `importance.threshold` değerine EŞİT veya BÜYÜKSE bildirim gönderilir.
    Eşiğin altındaki haberler asla Telegram'a gitmez; onlar yalnızca normal
    çıktıda (CLI/Markdown) ve web dashboard'da görünür.
  - Aynı haber (aynı `group_key`) için birden fazla bildirim gitmemesi,
    src/db.py'deki `NewsRecord.notified` bayrağı üzerinden `src/main.py`
    tarafında sağlanır — bu modül yalnızca "gönder" işlemine bakar, tekrar
    kontrolü burada yapılmaz (çağıran taraf zaten notified=False olan
    kayıtları filtreler).
  - Bildirim, TEK bir chat_id'ye değil, `src/db.py > subscribers` tablosundaki
    TÜM abonelere gönderilir. Abonelik, kullanıcıların bota /start yazmasıyla
    kendi kendine oluşur (bkz. src/telegram_bot.py) — burada .env'den okunan
    sabit bir chat_id yoktur.
  - Gönderim sırasında biri botu engellemişse ("Forbidden: bot was blocked by
    the user") o kişi otomatik olarak subscribers tablosundan çıkarılır,
    diğer abonelere gönderim etkilenmez (hata izolasyonu).
  - Abonelere art arda hızlı gönderim Telegram'ın rate limitine takılabileceği
    için mesajlar arasına küçük bir bekleme konur.
  - TELEGRAM_BOT_TOKEN tanımlı değilse, hiç abone yoksa veya
    `python-telegram-bot` paketi kurulu değilse: exception fırlatmadan uyarı
    loglanır ve gönderim atlanır (diğer tüm entegrasyonlarla aynı hata
    izolasyonu mantığı).
  - "Sadece KAP" modundaki aboneler (bkz. Telegram /kap_sadece komutu,
    src/db.py > Subscriber.kap_only_until) KAP-DIŞI kaynaklı haberleri BU
    MODÜL SEVİYESİNDE almaz - bkz. _is_kap_record + get_subscriber_chat_ids_for_score
    > is_kap parametresi.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from typing import TYPE_CHECKING

from src.telegram_format import sentiment_emoji

if TYPE_CHECKING:
    from src.db import NewsRecord

logger = logging.getLogger(__name__)

# Telegram'ın toplu mesaj gönderiminde pratikte saniyede ~30 mesajlık bir
# limiti vardır; abone sayısı arttıkça bu limite takılmamak için art arda
# gönderimler arasına küçük bir bekleme koyuyoruz.
_SEND_DELAY_SECONDS = 0.05

# src/fetchers/kap_fetcher.py > KAP_SOURCE_NAME ile AYNI değer olmalıdır -
# "sadece KAP" modundaki abonelerin (bkz. /kap_sadece komutu) KAP DIŞI
# bildirimleri almaması için bir kaydın KAP kaynaklı olup olmadığını
# belirlemede kullanılır (bkz. _is_kap_record). Burada webhook.py'deki
# DEFAULT_SOURCE_NAME gibi yerel bir sabit olarak tutulur (bu modülün
# src.fetchers.kap_fetcher'a bağımlı olmasını gerektirmez).
_KAP_SOURCE_NAME = "KAP"


def _is_kap_record(record: "NewsRecord") -> bool:
    """Kaydın `sources` alanında (virgülle ayrılmış, ör. "Bloomberg HT,
    CNBC-e") tam olarak `_KAP_SOURCE_NAME` geçip geçmediğini kontrol eder -
    basit bir substring kontrolü DEĞİL (ör. yanlışlıkla "KAPİTAL" gibi bir
    kaynak adını eşleştirmesin diye)."""
    return _KAP_SOURCE_NAME in (s.strip() for s in (record.sources or "").split(","))


def _format_message(record: "NewsRecord") -> str:
    title = html.escape(record.title)
    sources = html.escape(record.sources)
    reason = html.escape(record.importance_reason or "")
    summary = html.escape(record.summary or "")
    market_impact = html.escape(record.market_impact or "")
    score = record.importance_score if record.importance_score is not None else "?"

    links = record.links_list()
    if links:
        # İlk linki ana link olarak göster, diğerlerini alt satırlarda listele.
        link_lines = "\n".join(
            f'• <a href="{html.escape(l["link"])}">{html.escape(l["source"])}</a>' for l in links
        )
    else:
        link_lines = "(link yok)"

    stars = "⭐" * (record.importance_score or 0)
    emoji = sentiment_emoji(record.sentiment)
    emoji_prefix = f"{emoji} " if emoji else ""
    
    impact_str = f"💡 <b>Piyasa Etkisi:</b> {market_impact}\n\n" if market_impact else ""

    return (
        f"<b>🚨 Önemli Finans Haberi</b> {stars}\n\n"
        f"<b>{emoji_prefix}{title}</b>\n"
        f"<i>Kaynak(lar): {sources}</i>\n\n"
        f"<b>Önem Skoru:</b> {score}/5 — {reason}\n\n"
        f"<b>Özet:</b> {summary}\n\n{impact_str}"
        f"<b>Linkler:</b>\n{link_lines}"
    )


async def _send_to_all(bot_token: str, chat_ids: list[str], message: str) -> int:
    from telegram import Bot
    from telegram.constants import ParseMode
    from telegram.error import Forbidden, TelegramError

    from src.db import remove_subscriber

    sent_count = 0
    async with Bot(token=bot_token) as bot:
        for i, chat_id in enumerate(chat_ids):
            try:
                await bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.HTML)
                sent_count += 1
            except Forbidden:
                # Kullanıcı botu engellemiş -> abonelikten otomatik çıkar,
                # diğer abonelere gönderim devam etsin (hata izolasyonu).
                logger.warning("Chat ID %s botu engellemiş, abonelikten çıkarılıyor.", chat_id)
                remove_subscriber(chat_id)
            except TelegramError as e:
                logger.error("Chat ID %s icin mesaj basarisiz: %s", chat_id, e)
            except Exception:  # noqa: BLE001 - tek bir aboneye gönderim hatası diğerlerini etkilemesin
                logger.exception("Chat ID %s icin beklenmeyen hata", chat_id)

            if i < len(chat_ids) - 1:
                await asyncio.sleep(_SEND_DELAY_SECONDS)

    return sent_count


def send_telegram_notification(record: "NewsRecord") -> bool:
    """Verilen kayıt için, KENDİ önem eşiğini (importance_threshold, bkz.
    Telegram /esik komutu) karşılayan abonelere Telegram mesajı gönderir -
    artık TÜM abonelere değil: haberin importance_score'u, bir abonenin
    eşiğine eşit veya ondan büyükse o aboneye gider (bkz.
    src/db.py > get_subscriber_chat_ids_for_score).

    En az bir aboneye başarıyla gönderildiyse True, hiçbir sebeple (token
    eksik, eşiği karşılayan abone yok, paket kurulu değil, ağ hatası vb.)
    gönderilemediyse False döner. Hiçbir durumda exception fırlatmaz.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        logger.warning(
            "TELEGRAM_BOT_TOKEN tanımlı değil, Telegram bildirimi atlanıyor (haber: %s).",
            record.title,
        )
        return False

    try:
        import telegram  # noqa: F401
    except ImportError:
        logger.warning(
            "'python-telegram-bot' paketi kurulu değil (pip install -r "
            "requirements.txt). Telegram bildirimi atlanıyor."
        )
        return False

    from src.db import get_subscriber_chat_ids_for_score

    score = record.importance_score if record.importance_score is not None else 0
    chat_ids = get_subscriber_chat_ids_for_score(score, is_kap=_is_kap_record(record))
    if not chat_ids:
        logger.info(
            "Bu skoru (%s) karşılayan (kendi eşiğine göre) hiç abone yok, bildirim "
            "atlanıyor (haber: %s).",
            score,
            record.title,
        )
        return False

    message = _format_message(record)

    try:
        sent_count = asyncio.run(_send_to_all(bot_token, chat_ids, message))
    except Exception as exc:  # noqa: BLE001 - ağ hatası, geçersiz token vb.
        logger.error("Telegram bildirimi gönderilemedi (%s): %s", record.title, exc)
        return False

    if sent_count == 0:
        return False

    logger.info(
        "Telegram bildirimi gönderildi: %s (skor=%s) (%d/%d aboneye)",
        record.title,
        record.importance_score,
        sent_count,
        len(chat_ids),
    )
    return True


def send_telegram_message_to_chat_ids(chat_ids: list[str], message: str) -> int:
    """Genel amaçlı yardımcı: hazırlanmış (HTML) bir mesajı verilen chat_id
    listesine gönderir. `send_telegram_notification` ile AYNI alt yapıyı
    (Forbidden hatasında otomatik abonelik iptali, gönderimler arası bekleme)
    kullanır - src/daily_digest.py (günlük özet) ve src/keyword_alerts.py
    (anahtar kelime bildirimi) bu fonksiyonu kullanır.

    Döner: başarıyla gönderilen mesaj sayısı (0 = hiç gönderilemedi). Hiçbir
    durumda exception fırlatmaz.
    """
    if not chat_ids:
        return 0

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN tanımlı değil, mesaj gönderilemedi.")
        return 0

    try:
        import telegram  # noqa: F401
    except ImportError:
        logger.warning("'python-telegram-bot' paketi kurulu değil, mesaj gönderilemedi.")
        return 0

    try:
        return asyncio.run(_send_to_all(bot_token, chat_ids, message))
    except Exception:  # noqa: BLE001 - ağ hatası, geçersiz token vb.
        logger.exception("Toplu Telegram mesajı gönderilemedi.")
        return 0
