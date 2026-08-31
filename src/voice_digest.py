"""Sesli Günlük Özet: akşam 18:00'den sabah 09:00'a kadar (Europe/Istanbul,
config.yaml > tts.window_start_hour/window_end_hour) toplanan, önem skoru
eşiğini (config.yaml > tts.min_importance_score, varsayılan 4) geçen
haberlerden İKİ AYRI sesli özet üretir - GENEL HABERLER (KAP dışı tüm
kaynaklar) ve KAP HABERLERİ (sadece KAP özel durum açıklamaları) - ve
SADECE opt-in olmuş abonelere (Telegram /sesli_ozet_ac komutu, bkz.
src/db.py > Subscriber.voice_digest_enabled) İKİ AYRI Telegram mesajı
(ses + yazılı özet) olarak gönderir.

Neden iki ayrı özet: KAP bildirimleri (şirketlerin resmi özel durum
açıklamaları) ile genel piyasa/ekonomi haberleri farklı ilgi alanları olan
farklı dinleyiciler için önemli olabilir - tek bir karışık anlatı yerine,
kullanıcı hangisini önce/daha dikkatli dinleyeceğine kendisi karar
verebilsin diye ayrılır (2026-08-31, kullanıcı isteği). Opt-in tercihi
BİLİNÇLİ OLARAK tek bir açık/kapalı anahtardır (kategori bazlı DEĞİL) -
kullanıcı kararı: ek karmaşıklık (3 modlu komut ayrıştırma + şema
değişikliği) bu aşamada gereksiz bulundu; opt-in olan abone, o gece dolu
olan kategori(ler)in TÜMÜNÜ alır.

Bir kategoride hiç kayıt yoksa (ör. o gece hiç KAP bildirimi gelmediyse) o
kategori için HİÇBİR ŞEY gönderilmez - boş/anlamsız bir ses dosyası
üretilmez.

TEK bir ses dosyası (kategori başına) üretilir, TÜM opt-in abonelere AYNI
dosya gönderilir - kişiye özel üretim maliyeti/süresi gereksiz olurdu (bkz.
görev tanımı).

Bu özellik, mevcut yazılı Günlük Özet'ten (bkz. src/daily_digest.py, farklı
pencere: son 24 saat + LLM ile "en önemli 5-10'u seç") TAMAMEN AYRI,
bağımsız bir zamanlanmış görevdir (bkz. worker.py > "gunluk_sesli_ozet" cron
job) - biri diğerini etkilemez, biri başarısız olursa diğeri çalışmaya devam
eder.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import get_gemini_api_key, get_summarizer_api_key
from src.db import NewsRecord, get_records_since, get_voice_digest_subscriber_chat_ids
from src.notifier import send_telegram_audio_to_chat_ids, send_telegram_message_to_chat_ids
from src.summarizer import Summarizer
from src.timezone_utils import TURKEY_TZ
from src.tts import synthesize_turkish_speech

logger = logging.getLogger(__name__)

# src/fetchers/kap_fetcher.py > KAP_SOURCE_NAME, src/notifier.py >
# _KAP_SOURCE_NAME, src/news_links.py > _KAP_SOURCE_NAME İLE AYNI değer
# olmalıdır - bu modülün o dosyalara bağımlı olmasını gerektirmeyen, bu
# projede zaten yaygın olan yerel-sabit deseni (bkz. o dosyaların notları).
_KAP_SOURCE_NAME = "KAP"

# Telegram'ın sendAudio caption alanı ~1024 karakterle sınırlı - konuşma
# metni (150-280 kelime) genelde bunu aşar, bu yüzden ses dosyasına KISA bir
# caption konur, TAM yazılı özet ayrı bir mesaj olarak gönderilir (bkz.
# _send_category_digest).
_GENERAL_AUDIO_CAPTION = "🔊 <b>Genel Haberler — Gece Özeti</b>"
_KAP_AUDIO_CAPTION = "🔊 <b>KAP Bildirimleri — Gece Özeti</b>"
_GENERAL_TEXT_HEADER = "📝 <b>Genel Haberler — Yazılı Özet</b>"
_KAP_TEXT_HEADER = "📝 <b>KAP Bildirimleri — Yazılı Özet</b>"


def _is_kap_record(record: NewsRecord) -> bool:
    """Kaydın `sources` alanında (virgülle ayrılmış) tam olarak
    `_KAP_SOURCE_NAME` geçip geçmediğini kontrol eder - bkz.
    src/notifier.py > _is_kap_record İLE AYNI mantık."""
    return _KAP_SOURCE_NAME in (s.strip() for s in (record.sources or "").split(","))


def _night_window_start(tts_cfg: dict[str, Any]) -> datetime:
    """Bir önceki günün (Europe/Istanbul saatiyle) `window_start_hour`'ını
    (varsayılan 18:00) UTC olarak döner - `get_records_since` bunu bekler
    (bkz. src/db.py > NewsRecord.first_seen_at, hep UTC)."""
    window_start_hour = tts_cfg.get("window_start_hour", 18)
    now_tr = datetime.now(TURKEY_TZ)
    since_tr = (now_tr - timedelta(days=1)).replace(
        hour=window_start_hour, minute=0, second=0, microsecond=0
    )
    return since_tr.astimezone(timezone.utc)


def _send_category_digest(
    tts_cfg: dict[str, Any],
    chat_ids: list[str],
    records: list[NewsRecord],
    summarizer: Summarizer,
    gemini_api_key: str,
    category: str,
    filename: str,
    audio_caption: str,
    audio_title: str,
    text_header: str,
) -> None:
    """Tek bir kategori (genel VEYA kap) için metin+ses üretip opt-in
    abonelere gönderir. Kayıt yoksa veya herhangi bir adımda hata/boş sonuç
    alınırsa SESSİZCE atlar (exception fırlatmaz) - diğer kategori bundan
    ETKİLENMEZ (bkz. send_voice_digest, bu fonksiyon iki kez ayrı ayrı
    çağrılır)."""
    if not records:
        logger.info("Gece penceresinde '%s' kategorisinde haber yok, bu kategori atlanıyor.", category)
        return

    try:
        script = summarizer.generate_night_digest_script(records, category=category)
    except Exception:  # noqa: BLE001 - bir kategorinin hatası diğerini etkilemesin
        logger.exception("Sesli özet metni ('%s') üretilirken beklenmeyen bir hata oluştu.", category)
        return

    if not script:
        logger.warning("Sesli özet metni ('%s') boş döndü, bu kategori atlanıyor.", category)
        return

    audio_bytes = synthesize_turkish_speech(
        script,
        api_key=gemini_api_key,
        model=tts_cfg.get("gemini_model", "gemini-2.5-flash-preview-tts"),
        voice_name=tts_cfg.get("voice_name", "Kore"),
    )
    if audio_bytes is None:
        logger.warning("Sesli özet sesi ('%s') üretilemedi, bu kategori atlanıyor.", category)
        return

    audio_sent = send_telegram_audio_to_chat_ids(
        chat_ids, audio_bytes, filename=filename, caption=audio_caption, title=audio_title
    )

    # Ses dosyasının altına yazılı özeti de ekle (bkz. görev tanımı: "kullanıcı
    # sesi dinlemeden de okuyabilsin") - caption alanı bunun için çok kısıtlı
    # (~1024 karakter) olduğundan AYRI bir mesaj olarak gönderilir.
    text_message = f"{text_header}\n\n{html.escape(script)}"
    send_telegram_message_to_chat_ids(chat_ids, text_message)

    logger.info(
        "Sesli özet ('%s') gönderildi: %d haberden üretildi, %d/%d aboneye.",
        category,
        len(records),
        audio_sent,
        len(chat_ids),
    )


def send_voice_digest(config: dict[str, Any]) -> None:
    """Sesli Günlük Özeti (Genel + KAP, bkz. modül docstring'i) hazırlar ve
    opt-in abonelere gönderir. Herhangi bir hata durumunda exception
    fırlatmaz (çağıran taraf, ör. worker.py'deki cron job, sadece loglar -
    bir sonraki günün özeti etkilenmez)."""
    tts_cfg = config.get("tts", {})
    if not tts_cfg.get("enabled", True):
        logger.info("config.yaml > tts.enabled=false, sesli özet atlanıyor.")
        return

    telegram_cfg = config.get("telegram", {})
    if not telegram_cfg.get("enabled", True):
        logger.info("config.yaml > telegram.enabled=false, sesli özet atlanıyor.")
        return

    chat_ids = get_voice_digest_subscriber_chat_ids()
    if not chat_ids:
        logger.info("Sesli özete opt-in olmuş hiç abone yok, sesli özet atlanıyor.")
        return

    since = _night_window_start(tts_cfg)
    records = get_records_since(since)
    min_score = tts_cfg.get("min_importance_score", 4)
    important: list[NewsRecord] = [r for r in records if (r.importance_score or 0) >= min_score]
    if not important:
        logger.info("Gece penceresinde (min. skor %d) önemli haber yok, sesli özet atlanıyor.", min_score)
        return

    kap_records = [r for r in important if _is_kap_record(r)]
    general_records = [r for r in important if not _is_kap_record(r)]

    try:
        provider, api_key = get_summarizer_api_key(config["summarizer"])
    except RuntimeError as exc:
        logger.warning("%s Sesli özet metni üretilemiyor, sesli özet atlanıyor.", exc)
        return

    try:
        gemini_api_key = get_gemini_api_key()
    except RuntimeError as exc:
        # TTS her zaman Gemini kullanır (bkz. src/tts.py) - summarizer.llm_provider
        # "anthropic" olsa BİLE GEMINI_API_KEY tanımlı olmalı, yoksa sesli özet
        # atlanır (yazılı Günlük Özet bundan ETKİLENMEZ).
        logger.warning("%s Sesli özet sesi üretilemiyor, sesli özet atlanıyor.", exc)
        return

    output_dir = config.get("app", {}).get("output_dir", "data")
    summarizer = Summarizer(config["summarizer"], api_key=api_key, provider=provider, output_dir=output_dir)

    _send_category_digest(
        tts_cfg,
        chat_ids,
        general_records,
        summarizer,
        gemini_api_key,
        category="genel",
        filename="sesli_ozet_genel.mp3",
        audio_caption=_GENERAL_AUDIO_CAPTION,
        audio_title="Genel Haberler — Gece Özeti",
        text_header=_GENERAL_TEXT_HEADER,
    )
    _send_category_digest(
        tts_cfg,
        chat_ids,
        kap_records,
        summarizer,
        gemini_api_key,
        category="kap",
        filename="sesli_ozet_kap.mp3",
        audio_caption=_KAP_AUDIO_CAPTION,
        audio_title="KAP Bildirimleri — Gece Özeti",
        text_header=_KAP_TEXT_HEADER,
    )
