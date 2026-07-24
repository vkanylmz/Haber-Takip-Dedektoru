"""Telegram bot'unun gelen mesajlarını sürekli dinleyen katman (long polling).

Bu modül, "tek kullanıcıya elle .env'e chat_id eklenmesi" modelini, botla
konuşan HERKESİN kendi kendine abone olabildiği çok-kullanıcılı bir modele
çevirir:

  - Bir kullanıcı botla ilk kez konuşup /start yazdığında, karşılama mesajı
    (+ kullanılabilir komut listesi) gönderilir ve chat_id'si
    `src/db.py > subscribers` tablosuna kaydedilir (zaten aboneyse tekrar
    eklenmez, bkz. `add_subscriber`).
  - /stop yazan bir kullanıcı abonelikten çıkarılır.
  - /turkiye, /abd, /avrupa, /asya: son 24 saatte o bölgeye etiketlenmiş TÜM
    haberleri (önem skorundan bağımsız) isteyen kullanıcıya listeler. Bu,
    "önem skoru eşiğini geçen haberler otomatik herkese gider" akışından
    (bkz. src/notifier.py) TAMAMEN ayrı, kullanıcının isteği üzerine çalışan
    ek bir sorgu katmanıdır - otomatik bildirim davranışını değiştirmez.
  - /takip <kelime>, /takiplerim, /takipsil <kelime>: kullanıcı bazlı anahtar
    kelime/varlık takibi (bkz. src/keyword_alerts.py) - önem skoru eşiğinden
    BAĞIMSIZ, takip edilen kelime geçen her yeni haberde o kullanıcıya özel
    bildirim gider.
  - /yardim (veya /help): kullanılabilir komutları listeler. (Not: Telegram
    bot komutları yalnızca [a-z0-9_] içerebilir - "ı" gibi Türkçe karakterler
    içeremez, bu yüzden "/yardım" değil "/yardim" kullanılır.)
  - Yukarıdaki komutlardan biri OLMAYAN serbest metin mesajları, botun
    sahibine (.env > TELEGRAM_CHAT_ID, ilk değer) gönderenin bilgileriyle
    birlikte iletilir; gönderen kullanıcıya da bir onay mesajı döner (bkz.
    `_forward_feedback`).

python-telegram-bot'un `Application` (Updater) yapısı kullanılır; bu modül
worker.py tarafından ayrı bir arka plan thread'inde başlatılır, böylece RSS
tarama zamanlayıcısını (veya web dashboard'u) bloklamaz. `_run()`, olası bir
ağ hatası/exception sonrası dinleyicinin SESSİZCE ölüp bir daha hiç
çalışmamasını önlemek için sonsuz bir "hata olursa logla + kısa süre bekle +
yeniden başlat" döngüsü içinde çalışır - `python main.py`/`python worker.py`
süreci ayakta olduğu sürece bot dinleyicisi de kalıcı olarak aktif kalır.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from src.db import (
    add_keyword_subscription,
    add_subscriber,
    get_keywords_for_chat,
    get_records_since_by_region,
    remove_keyword_subscription,
    remove_subscriber,
)
from src.telegram_format import chunk_messages, format_news_block

logger = logging.getLogger(__name__)

# run_polling() beklenmedik bir hatayla durursa, yeniden denemeden önce
# beklenecek süre (saniye). Art arda hızlı çökme/yeniden başlama döngüsüne
# girip Telegram'ı gereksiz yormamak için küçük bir sabit bekleme yeterli.
_RESTART_BACKOFF_SECONDS = 5

_REGION_LABELS = {
    "turkiye": "🇹🇷 Türkiye",
    "abd": "🇺🇸 ABD",
    "avrupa": "🇪🇺 Avrupa",
    "asya": "🌏 Asya",
}

_COMMANDS_LIST_TEXT = (
    "/abd - ABD haberleri\n"
    "/avrupa - Avrupa haberleri\n"
    "/turkiye - Türkiye haberleri\n"
    "/asya - Asya haberleri\n"
    "/takip <kelime> - Bir kelimeyi/şirketi takip et\n"
    "/yardim - yardım menüsü"
)

_HELP_TEXT = (
    "<b>Kullanılabilir komutlar</b>\n\n"
    "/start — Abone ol, karşılama mesajını gör\n"
    "/stop — Abonelikten çık\n"
    "/turkiye — Son 24 saatteki Türkiye haberleri\n"
    "/abd — Son 24 saatteki ABD haberleri\n"
    "/avrupa — Son 24 saatteki Avrupa haberleri\n"
    "/asya — Son 24 saatteki Asya haberleri\n"
    "/takip &lt;kelime&gt; — Bir kelimeyi/şirketi takip listene ekle (ör. /takip Tesla)\n"
    "/takiplerim — Takip listeni göster\n"
    "/takipsil &lt;kelime&gt; — Bir kelimeyi takip listenden çıkar\n"
    "/yardim (veya /help) — Bu mesajı gösterir\n\n"
    "Yukarıdaki komutlardan biri değilse yazdığın her mesaj bize geri "
    "bildirim olarak iletilir. 📩\n\n"
    "Not: Önem skoru yüksek (flaş) haberler için otomatik bildirim akışı VE "
    "takip ettiğin kelimelerle ilgili bildirimler, bu komutlardan bağımsız "
    "olarak ayrıca çalışmaya devam eder. Her sabah (hafta içi 09:00) ayrıca "
    "bir günlük özet de alırsın."
)


def _welcome_text(first_name: str, already_subscribed: bool) -> str:
    if already_subscribed:
        return (
            f"Merhaba {first_name}, tekrar hoş geldin! 👋\n\n"
            "Zaten abone listesindesin, önemli finans haberleri gelmeye devam edecek. 📈\n\n"
            f"{_COMMANDS_LIST_TEXT}\n\n"
            "Aboneliğinden çıkmak istersen /stop yazman yeterli."
        )
    return (
        f"Merhaba {first_name}, hoş geldin! 👋\n\n"
        "Ben yapay zeka destekli Finansal Haber Asistanıyım 🤖📈. Senin için 7/24 "
        "piyasa haberlerini tarıyorum.\n\n"
        "Sadece 'önem skoru yüksek', piyasayı hareketlendirebilecek flaş bir "
        "haber gördüğümde sana hiç beklemeden bildirim göndereceğim. 🚀\n\n"
        f"{_COMMANDS_LIST_TEXT}\n\n"
        "Aboneliğinden çıkmak istersen istediğin zaman /stop yazabilirsin."
    )


def _get_admin_chat_id() -> str | None:
    """Botun sahibinin chat_id'si (.env > TELEGRAM_CHAT_ID). Serbest metin
    geri bildirimlerinin iletileceği hedeftir (bkz. `_forward_feedback`).
    Virgülle ayrılmış birden fazla değer varsa (geriye dönük uyumluluk) ilki
    kullanılır."""
    raw = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not raw:
        return None
    first = raw.split(",")[0].strip()
    return first or None


async def _subscribe(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or update.message is None:
        return

    first_name = (user.first_name if user else None) or "Yatırımcı"
    username = user.username if user else None

    added = add_subscriber(chat.id, username=username, first_name=first_name)
    logger.info("/start alındı: chat_id=%s, first_name=%s, yeni_abone=%s", chat.id, first_name, added)
    await update.message.reply_text(_welcome_text(first_name, already_subscribed=not added))


async def _unsubscribe(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    removed = remove_subscriber(chat.id)
    logger.info("/stop alındı: chat_id=%s, silindi=%s", chat.id, removed)
    if removed:
        text = "Aboneliğin iptal edildi, artık haber bildirimi almayacaksın. Tekrar katılmak istersen /start yazman yeterli. 👋"
    else:
        text = "Zaten abone değildin. Katılmak istersen /start yazabilirsin."
    await update.message.reply_text(text)


async def _help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    logger.info("/yardim (veya /help) alındı: chat_id=%s", update.effective_chat.id if update.effective_chat else "?")
    await update.message.reply_text(_HELP_TEXT, parse_mode=ParseMode.HTML)


async def _forward_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Komut olmayan serbest metin mesajlarını bot sahibine iletir."""
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or update.message is None or not update.message.text:
        return

    logger.info("Serbest metin mesajı alındı: chat_id=%s, metin=%r", chat.id, update.message.text[:200])

    admin_chat_id = _get_admin_chat_id()
    if admin_chat_id is None:
        logger.warning("TELEGRAM_CHAT_ID tanımlı değil, geri bildirim kimseye iletilemedi (chat_id=%s).", chat.id)
    else:
        sender_name = (user.first_name if user else None) or "Bilinmeyen"
        username_part = f"@{user.username}" if user and user.username else "(kullanıcı adı yok)"
        forward_text = (
            "📩 <b>Yeni kullanıcı mesajı</b>\n"
            f"Kimden: {html.escape(sender_name)} {html.escape(username_part)}\n"
            f"chat_id: <code>{chat.id}</code>\n\n"
            f"{html.escape(update.message.text)}"
        )
        try:
            await context.bot.send_message(chat_id=admin_chat_id, text=forward_text, parse_mode=ParseMode.HTML)
        except Exception:  # noqa: BLE001 - iletim başarısız olsa da kullanıcıya hata sızmasın
            logger.exception("Kullanıcı mesajı admin'e iletilemedi (chat_id=%s, admin=%s).", chat.id, admin_chat_id)

    await update.message.reply_text("Mesajınız iletildi. Teşekkürler! 🙏")


def _make_region_handler(region: str):
    label = _REGION_LABELS[region]

    async def _handler(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return

        logger.info("/%s alındı: chat_id=%s", region, update.effective_chat.id if update.effective_chat else "?")

        since = datetime.now(timezone.utc) - timedelta(hours=24)
        records = get_records_since_by_region(region, since)

        if not records:
            await update.message.reply_text(f"{label}: Bugün bu bölgeden haber bulunamadı.")
            return

        header = f"<b>{label} — Son 24 Saat ({len(records)} haber)</b>"
        blocks = [format_news_block(r) for r in records]
        messages = chunk_messages(header, blocks)

        for i, message_text in enumerate(messages):
            await update.message.reply_text(
                message_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
            if i < len(messages) - 1:
                await asyncio.sleep(0.05)

    return _handler


async def _track_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    keyword = " ".join(context.args).strip() if context.args else ""
    if not keyword:
        await update.message.reply_text("Kullanım: /takip <kelime>  (örn. /takip Tesla)")
        return

    added = add_keyword_subscription(chat.id, keyword)
    logger.info("/takip alındı: chat_id=%s, keyword=%r, eklendi=%s", chat.id, keyword, added)
    if added:
        await update.message.reply_text(
            f"'{keyword}' takip listene eklendi. 🔔 Bu kelime geçen yeni haberlerde bildirim alacaksın."
        )
    else:
        await update.message.reply_text(f"'{keyword}' zaten takip listende.")


async def _list_tracked_keywords(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    keywords = get_keywords_for_chat(chat.id)
    logger.info("/takiplerim alındı: chat_id=%s, sayı=%d", chat.id, len(keywords))
    if not keywords:
        await update.message.reply_text("Henüz hiç kelime takip etmiyorsun. Eklemek için: /takip <kelime>")
        return

    listing = "\n".join(f"• {html.escape(k)}" for k in keywords)
    await update.message.reply_text(f"<b>Takip listen:</b>\n{listing}", parse_mode=ParseMode.HTML)


async def _untrack_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    keyword = " ".join(context.args).strip() if context.args else ""
    if not keyword:
        await update.message.reply_text("Kullanım: /takipsil <kelime>  (örn. /takipsil Tesla)")
        return

    removed = remove_keyword_subscription(chat.id, keyword)
    logger.info("/takipsil alındı: chat_id=%s, keyword=%r, silindi=%s", chat.id, keyword, removed)
    if removed:
        await update.message.reply_text(f"'{keyword}' takip listenden çıkarıldı.")
    else:
        await update.message.reply_text(f"'{keyword}' zaten takip listende değildi.")


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Herhangi bir handler'da yakalanmamış bir hata olursa (ör. geçici bir
    veritabanı kilidi - artık db.py > _configure_sqlite_for_concurrency ile
    büyük ölçüde önlendi, ama başka nedenlerle de olabilir), python-telegram-bot
    bunu VARSAYILAN olarak sadece loglar ve kullanıcıya HİÇBİR yanıt gitmez -
    kullanıcı için "komut hiç çalışmadı" gibi görünür. Bu handler, en azından
    kullanıcıya bir geri bildirim dönmesini garanti eder."""
    logger.error("Handler içinde beklenmeyen hata oluştu: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "Bir şeyler ters gitti, lütfen birkaç saniye sonra tekrar dener misin? 🙏"
            )
        except Exception:  # noqa: BLE001 - hata bildirimi bile başarısız olursa sessizce vazgeç
            pass


def build_application(bot_token: str) -> Application:
    application = Application.builder().token(bot_token).build()
    application.add_handler(CommandHandler("start", _subscribe))
    application.add_handler(CommandHandler("stop", _unsubscribe))
    application.add_handler(CommandHandler(["yardim", "help"], _help))
    for region in _REGION_LABELS:
        application.add_handler(CommandHandler(region, _make_region_handler(region)))
    application.add_handler(CommandHandler("takip", _track_keyword))
    application.add_handler(CommandHandler("takiplerim", _list_tracked_keywords))
    application.add_handler(CommandHandler("takipsil", _untrack_keyword))
    # Yukarıdaki komutlardan biri olmayan serbest metin -> bot sahibine iletilir.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _forward_feedback))
    application.add_error_handler(_error_handler)
    return application


def _run(bot_token: str) -> None:
    """Bot dinleyicisini KALICI olarak çalışır durumda tutar.

    `application.run_polling()` normal şartlarda sonsuza kadar bloklar; ama
    beklenmeyen bir ağ hatası/exception ile dönerse (ör. geçici bir DNS/
    bağlantı sorunu), bu döngü olmadan arka plan thread'i SESSİZCE ölür ve
    bot bir daha hiç yanıt vermez - web dashboard ve RSS worker etkilenmediği
    için bu fark edilmesi zor bir hatadır. Bu yüzden her istisna loglanıp
    kısa bir bekleme sonrası dinleyici otomatik olarak yeniden başlatılır.
    """
    while True:
        try:
            application = build_application(bot_token)
            logger.info("Telegram bot dinleyicisi başlatıldı (long polling).")
            # stop_signals=None: bu, ana thread OLMAYAN bir arka plan
            # thread'inde çalışır; sinyal işleyicileri yalnızca ana thread'de
            # kurulabilir.
            application.run_polling(stop_signals=None, close_loop=True)
            logger.warning(
                "Telegram bot dinleyicisi beklenmedik şekilde durdu, %s sn sonra yeniden başlatılacak.",
                _RESTART_BACKOFF_SECONDS,
            )
        except Exception:  # noqa: BLE001 - dinleyici asla sessizce ölmesin
            logger.exception(
                "Telegram bot dinleyicisinde beklenmeyen bir hata oluştu, %s sn sonra yeniden başlatılacak.",
                _RESTART_BACKOFF_SECONDS,
            )
        time.sleep(_RESTART_BACKOFF_SECONDS)


def start_bot_listener_thread(bot_token: str) -> threading.Thread | None:
    """Telegram bot dinleyicisini ayrı bir daemon thread'de, KALICI olarak
    (hata durumunda otomatik yeniden başlayacak şekilde) başlatır.

    Worker'ın (RSS tarama) zamanlayıcısını veya web dashboard'unu bloklamaz.
    `bot_token` boşsa hiçbir şey yapmadan None döner.
    """
    if not bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN tanımlı değil, bot dinleyicisi başlatılmadı.")
        return None

    thread = threading.Thread(target=_run, args=(bot_token,), name="telegram-bot-listener", daemon=True)
    thread.start()
    return thread
