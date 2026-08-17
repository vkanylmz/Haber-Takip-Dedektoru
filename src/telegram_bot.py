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
  - /kap_sadece <süre>, /kap_durum, /kap_bitir: abone geçici olarak "sadece
    KAP" moduna geçebilir - bu süre boyunca KAP-dışı hiçbir haber bildirimi
    gitmez (bkz. src/db.py > Subscriber.kap_only_until, src/notifier.py >
    _is_kap_record). Süre dolunca AYRI bir işlem/job GEREKMEDEN otomatik
    olarak normal moda döner (lazy-expiry, bkz. o alanın docstring'i).
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
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.company_profile import get_company_profile
from src.config import load_config
from src.db import (
    add_keyword_subscription,
    add_subscriber,
    get_keywords_for_chat,
    get_records_by_company_ticker,
    get_records_since,
    get_records_since_by_region,
    get_source_health_summary,
    get_subscriber_kap_only,
    get_subscriber_threshold,
    remove_keyword_subscription,
    remove_subscriber,
    search_records,
    set_subscriber_kap_only,
    set_subscriber_threshold,
)
from src.summarizer import SECTOR_LABELS
from src.telegram_format import chunk_messages, format_news_block
from src.timezone_utils import TURKEY_TZ, format_turkey_time
from src.web.market_data import get_ticker_detail

logger = logging.getLogger(__name__)

# run_polling() beklenmedik bir hatayla durursa, yeniden denemeden önce
# beklenecek süre (saniye). Art arda hızlı çökme/yeniden başlama döngüsüne
# girip Telegram'ı gereksiz yormamak için küçük bir sabit bekleme yeterli.
_RESTART_BACKOFF_SECONDS = 5

_bot_loop: asyncio.AbstractEventLoop | None = None
_active_application: Application | None = None
_stop_event = threading.Event()

_REGION_LABELS = {
    "turkiye": "🇹🇷 Türkiye",
    "abd": "🇺🇸 ABD",
    "avrupa": "🇪🇺 Avrupa",
    "asya": "🌏 Asya",
}

# TEK KANONİK KOMUT KILAVUZU. /yardim komutu VE /start sonrası herkese
# gönderilip sabitlenen (pin) kılavuz mesajı (bkz. _send_and_pin_guide) AYNI
# bu sabiti kullanır - böylece ikisi arasında ASLA tutarsızlık/eksiklik
# olamaz (bkz. gereksinim: "ikisi de güncel komut listesini yansıtsın").
#
# ÖNEMLİ: Buradaki liste, build_application()'da FİİLEN kayıtlı olan
# CommandHandler'ların TAMAMINI (grep ile doğrulanmış, tahmin değil) içerir.
# Yeni bir komut eklendiğinde bu metni de güncellemeyi unutmayın - aksi
# halde kılavuz eksik kalır.
_GUIDE_TEXT = (
    "<b>📖 Kullanım Kılavuzu — Tüm Komutlar</b>\n\n"

    "<b>📰 Haber Filtreleme (Bölgesel)</b>\n"
    "/turkiye — Son 24 saatteki Türkiye haberleri\n"
    "/abd — Son 24 saatteki ABD haberleri\n"
    "/avrupa — Son 24 saatteki Avrupa haberleri\n"
    "/asya — Son 24 saatteki Asya haberleri\n\n"

    "<b>🔍 Arama &amp; Analiz</b>\n"
    "/ara &lt;kelime&gt; — Geçmiş TÜM haberlerde (başlık + özet) ara (ör. /ara faiz)\n"
    "/sirket &lt;isim&gt; — Bir şirket/varlık için son 30 günün haberleri + AI tarafından üretilmiş genel görünüm özeti (ör. /sirket Tesla)\n"
    "/hisse &lt;TICKER&gt; — Bir hissenin GÜNCEL fiyatı/günlük değişimi + son 30 günün ilgili haberleri (ör. /hisse ADM veya /hisse BIST: THYAO)\n"
    "/sektorler (veya /sektoranaliz) — Son 24 saatteki sektör analizi: hangi sektörde kaç haber var, ortalama önem, genel duygu eğilimi\n"
    "/kaynaksagligi — Her haber kaynağının son 24 saatteki başarı oranı, ortalama haber sayısı, son başarılı/başarısız çalışma zamanı\n\n"

    "<b>🔔 Bildirim &amp; Takip Ayarları</b>\n"
    "/takip &lt;kelime&gt; — Bir kelimeyi/şirketi takip listene ekle, o kelime geçen her yeni haberde bildirim al (ör. /takip Tesla)\n"
    "/takiplerim — Takip listeni göster\n"
    "/takipsil &lt;kelime&gt; — Bir kelimeyi takip listenden çıkar\n"
    "/esik &lt;1-5&gt; — Anlık flaş haber bildirimlerin için önem eşiğini kendine göre ayarla (ör. /esik 3 — bu skor ve üzerinde bildirim alırsın)\n"
    "/esikgoster — Şu anki bildirim eşiğini gösterir\n"
    "/kap_sadece &lt;süre&gt; — Geçici olarak SADECE KAP (özel durum açıklaması) bildirimleri al, diğer haberler dursun (ör. /kap_sadece 2 saat, /kap_sadece bugün — argümansız yazarsan buton seçenekleri çıkar)\n"
    "/kap_durum — Sadece-KAP modunda olup olmadığını ve kalan süreyi gösterir\n"
    "/kap_bitir — Sadece-KAP modundan erken çık, normal bildirimlere dön\n\n"

    "<b>⚙️ Hesap</b>\n"
    "/start — Abone ol / karşılama mesajını tekrar gör\n"
    "/stop — Abonelikten çık\n"
    "/yardim (veya /help) — Bu kılavuzu tekrar gösterir\n\n"

    "Yukarıdaki komutlardan biri değilse yazdığın her mesaj bize geri "
    "bildirim olarak iletilir. 📩\n\n"
    "<i>Not: Kendi eşiğini (/esik) karşılayan/geçen haberler için otomatik "
    "anlık bildirim akışı VE takip ettiğin kelimelerle ilgili bildirimler, "
    "bu komutlardan bağımsız olarak ayrıca çalışmaya devam eder. Her sabah "
    "(hafta içi 09:00) bir günlük özet, her Pazartesi bir haftalık trend "
    "raporu, ayın ilk Pazartesi'sinde de ayrıca bir aylık trend raporu "
    "alırsın.</i>"
)

_MIN_THRESHOLD = 1
_MAX_THRESHOLD = 5

# --- "Sadece KAP" modu (bkz. /kap_sadece, /kap_durum, /kap_bitir) ---
# Süre serbest metin OLARAK da girilebilir (ör. "/kap_sadece 2 saat") - bu
# yüzden bir regex parser var; argümansız çağrıldığında ("/kap_sadece") ise
# aşağıdaki 3 buton (InlineKeyboardMarkup) gösterilir - ikisi birden (hem
# hızlı tıklama hem esneklik) kullanıcı isteğiydi (bkz. sohbet, 2026-08-17).
_KAP_ONLY_DURATION_UNIT_SECONDS = {
    "dk": 60,
    "dakika": 60,
    "sa": 3600,
    "saat": 3600,
    "g": 86400,
    "gun": 86400,
    "gün": 86400,
}
_KAP_ONLY_DURATION_RE = re.compile(r"^\s*(\d+)\s*([a-zçğıöşü]+)\s*$", re.IGNORECASE)

# callback_data içindeki kod -> (buton etiketi, timedelta üretici). "bugun"
# özel bir durum (Türkiye saatiyle günün geri kalanı) olduğundan timedelta
# yerine None + ayrı bir dal kullanılır (bkz. _kap_only_duration_for_code).
_KAP_ONLY_PRESETS = [
    ("1s", "1 Saat"),
    ("bugun", "Bugün"),
    ("3g", "3 Gün"),
]


def _kap_only_end_of_today_turkey() -> datetime:
    """Türkiye saatiyle "bugünün sonu" (23:59:59) - UTC'ye çevrilmiş olarak
    döner. `/kap_sadece bugün` ve inline "Bugün" butonu tarafından kullanılır."""
    now_tr = datetime.now(TURKEY_TZ)
    end_of_day_tr = now_tr.replace(hour=23, minute=59, second=59, microsecond=0)
    return end_of_day_tr.astimezone(timezone.utc)


def _parse_kap_only_duration(text: str) -> datetime | None:
    """Serbest metin bir süre ifadesini ("1 saat", "3 gün", "bugün" gibi)
    ayrıştırıp UTC bir bitiş zamanına çevirir. Anlaşılamazsa None döner."""
    normalized = text.strip().lower()
    if normalized in ("bugun", "bugün"):
        return _kap_only_end_of_today_turkey()

    match = _KAP_ONLY_DURATION_RE.match(normalized)
    if not match:
        return None
    amount = int(match.group(1))
    unit_seconds = _KAP_ONLY_DURATION_UNIT_SECONDS.get(match.group(2))
    if unit_seconds is None or amount <= 0:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=amount * unit_seconds)


def _kap_only_duration_for_code(code: str) -> datetime | None:
    """Inline buton callback_data kodunu ("1s", "bugun", "3g") bir UTC bitiş
    zamanına çevirir - bkz. _KAP_ONLY_PRESETS."""
    if code == "bugun":
        return _kap_only_end_of_today_turkey()
    if code == "1s":
        return datetime.now(timezone.utc) + timedelta(hours=1)
    if code == "3g":
        return datetime.now(timezone.utc) + timedelta(days=3)
    return None


def _welcome_text(first_name: str, already_subscribed: bool) -> str:
    """Karşılama mesajı KASITLI OLARAK komut listesini tekrarlamaz - hemen
    ardından ayrıca gönderilip sohbette SABİTLENECEK (pin) olan kapsamlı
    kılavuza (bkz. _GUIDE_TEXT, _send_and_pin_guide) işaret eder. Böylece
    tek bir kanonik komut listesi olur, iki yerde tutulup birbirinden
    eksik/farklı kalma riski olmaz."""
    if already_subscribed:
        return (
            f"Merhaba {first_name}, tekrar hoş geldin! 👋\n\n"
            "Zaten abone listesindesin, önemli finans haberleri gelmeye devam edecek. 📈\n\n"
            "Az sonra tüm komutları içeren bir kılavuz mesajı göndereceğim ve "
            "sohbetine sabitleyeceğim (istediğin an başa dönebilirsin). 📌\n\n"
            "Aboneliğinden çıkmak istersen /stop yazman yeterli."
        )
    return (
        f"Merhaba {first_name}, hoş geldin! 👋\n\n"
        "Ben yapay zeka destekli Finansal Haber Asistanıyım 🤖📈. Senin için 7/24 "
        "piyasa haberlerini tarıyorum.\n\n"
        "Sadece 'önem skoru yüksek', piyasayı hareketlendirebilecek flaş bir "
        "haber gördüğümde sana hiç beklemeden bildirim göndereceğim. 🚀\n\n"
        "Az sonra tüm komutları içeren bir kılavuz mesajı göndereceğim ve "
        "sohbetine sabitleyeceğim (istediğin an başa dönebilirsin). 📌\n\n"
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


async def _send_and_pin_guide(bot, chat_id: str | int) -> bool:
    """`_GUIDE_TEXT` mesajını gönderir ve kullanıcının (bire bir/özel)
    sohbetinde SABİTLER (bkz. Telegram Bot API > pinChatMessage - özel
    sohbetlerde botun ekstra bir "yönetici" hakkına ihtiyacı yoktur, gruplar/
    kanallardan farklı olarak).

    Döner: sabitleme başarılıysa True. Mesaj gönderimi başarısız olursa
    (ör. kullanıcı botu engellemiş) exception YUKARI FIRLATILIR (çağıran
    taraf - /start handler'ı veya toplu gönderim script'i - bunu kendi
    hata izolasyonuyla ele alır); ama mesaj gönderildiği HALDE sadece
    sabitleme başarısız olursa (nadir bir API hatası) sessizce loglanıp
    False döner - kullanıcı en azından mesajı almış olur."""
    message = await bot.send_message(chat_id=chat_id, text=_GUIDE_TEXT, parse_mode=ParseMode.HTML)
    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=message.message_id, disable_notification=True)
        return True
    except Exception:  # noqa: BLE001 - sabitleme başarısız olsa da mesaj zaten gönderildi
        logger.exception("Kılavuz mesajı sabitlenemedi (mesaj yine de gönderildi): chat_id=%s", chat_id)
        return False


async def _subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or update.message is None:
        return

    first_name = (user.first_name if user else None) or "Yatırımcı"
    username = user.username if user else None

    default_threshold = load_config().get("importance", {}).get("threshold", 4)
    added = add_subscriber(chat.id, username=username, first_name=first_name, default_threshold=default_threshold)
    logger.info("/start alındı: chat_id=%s, first_name=%s, yeni_abone=%s", chat.id, first_name, added)
    await update.message.reply_text(_welcome_text(first_name, already_subscribed=not added))

    try:
        pinned = await _send_and_pin_guide(context.bot, chat.id)
        logger.info("/start sonrası kılavuz gönderildi: chat_id=%s, sabitlendi=%s", chat.id, pinned)
    except Exception:  # noqa: BLE001 - kılavuz gönderimi başarısız olsa da /start akışı bozulmasın
        logger.exception("/start sonrası kılavuz mesajı gönderilemedi: chat_id=%s", chat.id)


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
    await update.message.reply_text(_GUIDE_TEXT, parse_mode=ParseMode.HTML)


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


async def _sector_analysis(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Web dashboard'daki sektör ısı haritasının (bkz. src/web/app.py >
    _build_sector_heatmap) Telegram'daki metin karşılığı: son 24 saatteki
    haberleri sektöre göre gruplayıp haber sayısı + ortalama önem skoru +
    sentiment dağılımını özetler. En çok haberli sektör en üstte."""
    if update.message is None:
        return

    logger.info("/sektorler alındı: chat_id=%s", update.effective_chat.id if update.effective_chat else "?")

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    records = get_records_since(since)

    stats: dict[str, dict[str, int]] = {}
    for r in records:
        # Henüz sektör sınıflandırması yapılmamış haberler sessizce atlanır
        # (bkz. src/web/app.py > _build_sector_heatmap'teki aynı yaklaşım).
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
        await update.message.reply_text("Son 24 saatte sektör etiketli haber bulunamadı.")
        return

    lines = ["<b>🏭 Sektör Analizi — Son 24 Saat</b>\n"]
    for sector, s in sorted(stats.items(), key=lambda kv: kv[1]["count"], reverse=True):
        count = s["count"]
        avg_score = (s["score_sum"] / count) if count else 0.0
        mood_counts = {"pozitif": s["pos"], "negatif": s["neg"], "notr": s["notr"]}
        top_mood = max(mood_counts, key=mood_counts.get)
        if list(mood_counts.values()).count(mood_counts[top_mood]) > 1:
            mood_label = "karışık"
        else:
            mood_label = {
                "pozitif": "çoğunlukla pozitif",
                "negatif": "çoğunlukla negatif",
                "notr": "çoğunlukla nötr",
            }[top_mood]
        label = SECTOR_LABELS.get(sector, sector)
        lines.append(
            f"🏭 <b>{html.escape(label)}</b>: {count} haber, ortalama önem {avg_score:.1f}, {mood_label} "
            f"(📈{s['pos']} / 📉{s['neg']} / ➖{s['notr']})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _search_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ara <kelime>: dashboard'daki arama kutusuyla (bkz. src/db.py >
    get_recent_records > search_query) AYNI basit case-insensitive LIKE
    mantığını kullanarak geçmiş TÜM haberlerde (başlık + özet) arama yapar."""
    if update.message is None:
        return

    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text("Kullanım: /ara <kelime>  (örn. /ara faiz)")
        return

    logger.info("/ara alındı: chat_id=%s, sorgu=%r", update.effective_chat.id if update.effective_chat else "?", query)

    records = search_records(query, limit=15)
    if not records:
        await update.message.reply_text(f"'{query}' için haber bulunamadı.")
        return

    header = f"<b>🔎 '{html.escape(query)}' için {len(records)} sonuç</b> (en fazla 15 gösterilir)"
    blocks = [format_news_block(r) for r in records]
    messages = chunk_messages(header, blocks)

    for i, message_text in enumerate(messages):
        await update.message.reply_text(message_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        if i < len(messages) - 1:
            await asyncio.sleep(0.05)


async def _company_profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sirket <isim>: web dashboard'daki Şirket Profili sayfasının (bkz.
    src/company_profile.py > get_company_profile) Telegram karşılığı - son 30
    günün ilgili haberleri + LLM tarafından üretilmiş kısa bir genel görünüm
    özeti. LLM çağrısı birkaç saniye sürebileceğinden, kullanıcıya "yazıyor"
    göstergesi gösterilir (bkz. ChatAction.TYPING)."""
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    company_name = " ".join(context.args).strip() if context.args else ""
    if not company_name:
        await update.message.reply_text("Kullanım: /sirket <isim>  (örn. /sirket Tesla)")
        return

    logger.info("/sirket alındı: chat_id=%s, sirket=%r", chat.id, company_name)

    try:
        await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    except Exception:  # noqa: BLE001 - "yazıyor" göstergesi başarısız olsa da akış devam etsin
        pass

    config = load_config()
    profile = get_company_profile(company_name, config)

    if not profile["records"]:
        await update.message.reply_text(f"'{company_name}' için son 30 günde haber bulunamadı.")
        return

    lines = [f"<b>🏢 Şirket Profili — {html.escape(profile['company'])}</b> (son 30 gün, {len(profile['records'])} haber)\n"]

    if profile["summary"]:
        lines.append(f"🤖 <b>Genel Görünüm:</b>\n{html.escape(profile['summary'])}\n")
    else:
        lines.append("🤖 <i>Otomatik özet şu an üretilemedi, aşağıda ilgili haberler listeleniyor.</i>\n")

    lines.append("📰 <b>Öne Çıkan Haberler:</b>")
    top_records = sorted(
        (r for r in profile["records"] if r.importance_score is not None),
        key=lambda r: r.importance_score,
        reverse=True,
    )[:10] or profile["records"][:10]

    for r in top_records:
        score = r.importance_score if r.importance_score is not None else "?"
        link = r.links_list()[0]["link"] if r.links_list() else None
        title_part = f'<a href="{html.escape(link)}">{html.escape(r.title)}</a>' if link else html.escape(r.title)
        title_tr_part = f" <i>({html.escape(r.title_tr)})</i>" if getattr(r, "title_tr", None) else ""
        lines.append(f"• [{score}/5] {title_part}{title_tr_part} <i>({html.escape(r.sources)})</i>")

    messages = chunk_messages("\n".join(lines[:1]), lines[1:])
    for i, message_text in enumerate(messages):
        await update.message.reply_text(message_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        if i < len(messages) - 1:
            await asyncio.sleep(0.05)


async def _stock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hisse <TICKER>: Haftalık Emtia Raporu'ndaki tıklanabilir şirket
    kartlarının (bkz. src/web/market_data.py > get_ticker_detail, dashboard
    şirket detay modalıyla AYNI veri kaynağı) Telegram karşılığı - güncel
    fiyat/değişim + son 30 günün `company_ticker` alanı TAM eşleşen
    haberleri (bkz. src/db.py > get_records_by_company_ticker - LLM çağrısı
    YOK, /sirket'in aksine anında yanıt verir).

    Borsa belirtilmezse (ör. "/hisse ADM") NASDAQ/NYSE varsayılır - ikisi de
    Yahoo sembolüne sonek eklemediğinden (bkz. _EXCHANGE_YAHOO_SUFFIX)
    aralarında pratik bir fark yoktur. "BORSA: SEMBOL" formatında da (ör.
    "/hisse BIST: THYAO") girilebilir."""
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    raw = " ".join(context.args).strip() if context.args else ""
    if not raw:
        await update.message.reply_text("Kullanım: /hisse <TICKER>  (örn. /hisse ADM veya /hisse BIST: THYAO)")
        return

    company_ticker = raw if ":" in raw else f"NASDAQ: {raw.upper()}"
    logger.info("/hisse alındı: chat_id=%s, ticker=%r", chat.id, company_ticker)

    detail = await get_ticker_detail(company_ticker)
    if detail is None or detail.get("quote") is None:
        await update.message.reply_text(f"'{raw}' için güncel fiyat bulunamadı (sembol geçersiz/desteklenmiyor olabilir).")
        return

    quote = detail["quote"]
    arrow = "📈" if quote["change_pct"] >= 0 else "📉"
    sign = "+" if quote["change_pct"] >= 0 else ""
    lines = [
        f"<b>{html.escape(quote['label'])}</b>",
        f"{quote['price']} {html.escape(quote['currency'])} ({arrow} {sign}{quote['change_pct']}%)",
    ]
    if quote.get("is_delayed"):
        lines.append(f"<i>({quote['delay_minutes']} dk gecikmeli veri)</i>")

    since = datetime.now(timezone.utc) - timedelta(days=30)
    records = get_records_by_company_ticker(company_ticker, limit=5, since=since)
    if records:
        lines.append("\n📰 <b>Son Haberler:</b>")
        for r in records:
            link = r.links_list()[0]["link"] if r.links_list() else None
            title_part = f'<a href="{html.escape(link)}">{html.escape(r.title)}</a>' if link else html.escape(r.title)
            title_tr_part = f" <i>({html.escape(r.title_tr)})</i>" if getattr(r, "title_tr", None) else ""
            lines.append(f"• {title_part}{title_tr_part} <i>({html.escape(r.sources)})</i>")
    else:
        lines.append("\n<i>Son 30 günde bu ticker'a etiketlenmiş haber yok.</i>")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def _source_health_command(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """/kaynaksagligi: web dashboard'daki Kaynak Sağlığı panelinin (bkz.
    src/db.py > get_source_health_summary) Telegram karşılığı - her
    kaynağın son 24 saatteki başarı oranı, ortalama haber sayısı, son
    başarılı/başarısız çalışma zamanı."""
    if update.message is None:
        return

    logger.info("/kaynaksagligi alındı: chat_id=%s", update.effective_chat.id if update.effective_chat else "?")

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = get_source_health_summary(since)
    if not rows:
        await update.message.reply_text("Son 24 saatte hiç fetcher çalıştırma kaydı yok.")
        return

    def _fmt(dt: datetime | None) -> str:
        return format_turkey_time(dt) if dt else "—"

    lines = ["<b>🩺 Kaynak Sağlığı — Son 24 Saat</b>\n"]
    for row in rows:
        rate = row["success_rate"]
        emoji = "🟢" if rate >= 90 else ("🟡" if rate >= 50 else "🔴")
        lines.append(
            f"{emoji} <b>{html.escape(row['source'])}</b>: %{rate} başarı ({row['total_runs']} çalıştırma), "
            f"ort. {row['avg_item_count']} haber\n"
            f"   Son başarılı: {_fmt(row['last_success_at'])} · Son başarısız: {_fmt(row['last_failure_at'])}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


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


async def _set_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/esik <1-5>: kullanıcının kendi anlık bildirim önem eşiğini ayarlar
    (bkz. src/db.py > Subscriber.importance_threshold, src/notifier.py).
    Abone değilse (hiç /start yazmamışsa) ayarlanacak bir kaydı olmadığı için
    reddedilir - önce /start ile abone olması istenir."""
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    arg = context.args[0].strip() if context.args else ""
    if not arg.isdigit() or not (_MIN_THRESHOLD <= int(arg) <= _MAX_THRESHOLD):
        await update.message.reply_text(
            f"Kullanım: /esik <{_MIN_THRESHOLD}-{_MAX_THRESHOLD}>  (örn. /esik 3 — "
            f"3 ve üzeri önem skorlu haberlerde bildirim al)"
        )
        return

    threshold = int(arg)
    updated = set_subscriber_threshold(chat.id, threshold)
    logger.info("/esik alındı: chat_id=%s, esik=%d, guncellendi=%s", chat.id, threshold, updated)
    if updated:
        await update.message.reply_text(
            f"Bildirim eşiğin {threshold}/5 olarak ayarlandı. Artık {threshold} ve üzeri "
            "önem skorlu haberlerde anlık bildirim alacaksın. 🔔"
        )
    else:
        await update.message.reply_text("Önce /start yazıp abone olman gerekiyor.")


async def _show_threshold(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """/esikgoster: kullanıcının şu anki bildirim eşiğini gösterir."""
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    threshold = get_subscriber_threshold(chat.id)
    logger.info("/esikgoster alındı: chat_id=%s, esik=%s", chat.id, threshold)
    if threshold is None:
        await update.message.reply_text("Henüz abone değilsin. Katılmak için /start yazabilirsin.")
        return

    await update.message.reply_text(
        f"Şu anki bildirim eşiğin: {threshold}/5. Değiştirmek için: /esik <1-5>"
    )


def _kap_only_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"kap_only:{code}") for code, label in _KAP_ONLY_PRESETS]]
    )


async def _kap_only_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/kap_sadece [süre]: aboneyi geçici olarak "sadece KAP" moduna alır -
    bu süre boyunca KAP dışı hiçbir haber bildirimi gitmez (bkz.
    src/notifier.py > _is_kap_record, src/db.py > Subscriber.kap_only_until).

    Argüman verilmezse (`/kap_sadece`) hızlı seçim için 3 buton gösterilir;
    argüman serbest metinse ("2 saat", "3 gün", "bugün" gibi) ayrıştırılmaya
    çalışılır (bkz. _parse_kap_only_duration)."""
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    arg = " ".join(context.args).strip() if context.args else ""
    logger.info("/kap_sadece alındı: chat_id=%s, arg=%r", chat.id, arg)

    if not arg:
        await update.message.reply_text(
            "Ne kadar süreyle sadece KAP (özel durum açıklaması) bildirimleri "
            "almak istersin? Aşağıdan seç, ya da serbest metin yaz "
            "(ör. /kap_sadece 2 saat).",
            reply_markup=_kap_only_keyboard(),
        )
        return

    until = _parse_kap_only_duration(arg)
    if until is None:
        await update.message.reply_text(
            "Anlayamadım. Örnekler: /kap_sadece 1 saat, /kap_sadece 3 gün, "
            "/kap_sadece bugün — ya da argümansız /kap_sadece yazıp butonlardan seç."
        )
        return

    updated = set_subscriber_kap_only(chat.id, until)
    if not updated:
        await update.message.reply_text("Önce /start yazıp abone olman gerekiyor.")
        return

    await update.message.reply_text(
        f"✅ Sadece KAP moduna geçtin. {format_turkey_time(until)}'e kadar (Türkiye saati) "
        "SADECE özel durum açıklamaları bildirimi alacaksın, diğer haberler durdu. "
        "Erken bitirmek için: /kap_bitir"
    )


async def _kap_only_callback(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Yukarıdaki `_kap_only_keyboard()` butonlarına basıldığında tetiklenir
    (bkz. build_application > CallbackQueryHandler, pattern="^kap_only:")."""
    query = update.callback_query
    if query is None or query.message is None or query.data is None:
        return
    await query.answer()

    code = query.data.split(":", 1)[-1]
    until = _kap_only_duration_for_code(code)
    chat_id = query.message.chat_id
    logger.info("kap_only butonu alındı: chat_id=%s, code=%s", chat_id, code)

    if until is None:
        return

    updated = set_subscriber_kap_only(chat_id, until)
    if not updated:
        await query.edit_message_text("Önce /start yazıp abone olman gerekiyor.")
        return

    await query.edit_message_text(
        f"✅ Sadece KAP moduna geçtin. {format_turkey_time(until)}'e kadar (Türkiye saati) "
        "SADECE özel durum açıklamaları bildirimi alacaksın, diğer haberler durdu. "
        "Erken bitirmek için: /kap_bitir"
    )


async def _kap_only_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """/kap_durum: aboneye şu an sadece-KAP modunda olup olmadığını ve
    (öyleyse) kalan süreyi gösterir."""
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    until = get_subscriber_kap_only(chat.id)
    logger.info("/kap_durum alındı: chat_id=%s, kap_only_until=%s", chat.id, until)
    if until is None:
        await update.message.reply_text(
            "Şu an normal moddasın (tüm önemli haberleri alıyorsun). Sadece KAP "
            "moduna geçmek için: /kap_sadece <süre>"
        )
        return

    await update.message.reply_text(
        f"🔔 Şu an SADECE KAP modundasın, {format_turkey_time(until)}'e kadar (Türkiye saati). "
        "Erken bitirmek için: /kap_bitir"
    )


async def _kap_only_stop(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """/kap_bitir: sadece-KAP modunu erken sonlandırır (süresi zaten dolmuşsa
    da zararsız - normal moda geri döner)."""
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    updated = set_subscriber_kap_only(chat.id, None)
    logger.info("/kap_bitir alındı: chat_id=%s, guncellendi=%s", chat.id, updated)
    if not updated:
        await update.message.reply_text("Önce /start yazıp abone olman gerekiyor.")
        return

    await update.message.reply_text("Sadece-KAP modundan çıkıldı, artık tüm önemli haberleri alacaksın. 📰")


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
    application.add_handler(CommandHandler(["sektorler", "sektoranaliz"], _sector_analysis))
    application.add_handler(CommandHandler("ara", _search_news))
    application.add_handler(CommandHandler("sirket", _company_profile_command))
    application.add_handler(CommandHandler("hisse", _stock_command))
    application.add_handler(CommandHandler("kaynaksagligi", _source_health_command))
    application.add_handler(CommandHandler("takip", _track_keyword))
    application.add_handler(CommandHandler("takiplerim", _list_tracked_keywords))
    application.add_handler(CommandHandler("takipsil", _untrack_keyword))
    application.add_handler(CommandHandler("esik", _set_threshold))
    application.add_handler(CommandHandler("esikgoster", _show_threshold))
    application.add_handler(CommandHandler("kap_sadece", _kap_only_mode))
    application.add_handler(CommandHandler("kap_durum", _kap_only_status))
    application.add_handler(CommandHandler("kap_bitir", _kap_only_stop))
    application.add_handler(CallbackQueryHandler(_kap_only_callback, pattern=r"^kap_only:"))
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
    global _bot_loop, _active_application
    _bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_bot_loop)

    while not _stop_event.is_set():
        try:
            application = build_application(bot_token)
            _active_application = application
            logger.info("Telegram bot dinleyicisi başlatıldı (long polling).")
            # stop_signals=None: bu, ana thread OLMAYAN bir arka plan
            # thread'inde çalışır; sinyal işleyicileri yalnızca ana thread'de
            # kurulabilir.
            application.run_polling(stop_signals=None, close_loop=True)
            
            if _stop_event.is_set():
                break
                
            logger.warning(
                "Telegram bot dinleyicisi beklenmedik şekilde durdu, %s sn sonra yeniden başlatılacak.",
                _RESTART_BACKOFF_SECONDS,
            )
        except Exception:  # noqa: BLE001 - dinleyici asla sessizce ölmesin
            if _stop_event.is_set():
                break
            logger.exception(
                "Telegram bot dinleyicisinde beklenmeyen bir hata oluştu, %s sn sonra yeniden başlatılacak.",
                _RESTART_BACKOFF_SECONDS,
            )
        
        if not _stop_event.is_set():
            _bot_loop.run_until_complete(asyncio.sleep(_RESTART_BACKOFF_SECONDS))
            
    _bot_loop.close()
    logger.info("Telegram bot dinleyicisi başarıyla sonlandırıldı.")


def start_bot_listener_thread(bot_token: str) -> threading.Thread | None:
    """Telegram bot dinleyicisini ayrı bir daemon thread'de, KALICI olarak
    (hata durumunda otomatik yeniden başlayacak şekilde) başlatır.

    Worker'ın (RSS tarama) zamanlayıcısını veya web dashboard'unu bloklamaz.
    `bot_token` boşsa hiçbir şey yapmadan None döner.
    """
    if not bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN tanımlı değil, bot dinleyicisi başlatılmadı.")
        return None

    _stop_event.clear()
    thread = threading.Thread(target=_run, args=(bot_token,), name="telegram-bot-listener", daemon=False)
    thread.start()
    return thread

def stop_bot_listener_thread() -> None:
    """Çalışan bot thread'ine durma sinyali gönderir ve graceful şekilde kapatır."""
    _stop_event.set()
    if _bot_loop and _bot_loop.is_running() and _active_application:
        async def _stop():
            if _active_application.updater and _active_application.updater.running:
                await _active_application.updater.stop()
            if _active_application.running:
                await _active_application.stop()
        try:
            future = asyncio.run_coroutine_threadsafe(_stop(), _bot_loop)
            future.result(timeout=5)
        except Exception:
            pass
