"""Sesli Günlük Özet: akşam 18:00'den sabah 09:00'a kadar (Europe/Istanbul,
config.yaml > tts.window_start_hour/window_end_hour) toplanan, önem skoru
eşiğini (config.yaml > tts.min_importance_score, varsayılan 4) geçen
haberlerin AKICI bir Türkçe konuşma metnini Gemini/Claude'a ürettirip (bkz.
src/summarizer.py > generate_night_digest_script) Gemini TTS ile sese
çevirir (bkz. src/tts.py) ve SADECE opt-in olmuş abonelere (Telegram
/sesli_ozet_ac komutu, bkz. src/db.py > Subscriber.voice_digest_enabled)
Telegram üzerinden `sendAudio` ile gönderir.

TEK bir ses dosyası üretilir, TÜM opt-in abonelere AYNI dosya gönderilir -
kişiye özel üretim maliyeti/süresi gereksiz olurdu (bkz. görev tanımı).

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

# Telegram'ın sendAudio caption alanı ~1024 karakterle sınırlı - konuşma
# metni (150-280 kelime) genelde bunu aşar, bu yüzden ses dosyasına KISA bir
# caption konur, TAM yazılı özet ayrı bir mesaj olarak gönderilir (bkz.
# send_voice_digest).
_AUDIO_CAPTION = "🎧 <b>Sesli Günlük Özet</b> — gece boyunca önemli gelişmeler"


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


def send_voice_digest(config: dict[str, Any]) -> None:
    """Sesli Günlük Özeti hazırlar ve opt-in abonelere gönderir. Herhangi bir
    hata durumunda exception fırlatmaz (çağıran taraf, ör. worker.py'deki
    cron job, sadece loglar - bir sonraki günün özeti etkilenmez)."""
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

    try:
        provider, api_key = get_summarizer_api_key(config["summarizer"])
    except RuntimeError as exc:
        logger.warning("%s Sesli özet metni üretilemiyor, sesli özet atlanıyor.", exc)
        return

    output_dir = config.get("app", {}).get("output_dir", "data")
    summarizer = Summarizer(config["summarizer"], api_key=api_key, provider=provider, output_dir=output_dir)
    try:
        script = summarizer.generate_night_digest_script(important)
    except Exception:  # noqa: BLE001 - sesli özet asla tüm uygulamayı etkilemesin
        logger.exception("Sesli özet metni üretilirken beklenmeyen bir hata oluştu.")
        return

    if not script:
        logger.warning("Sesli özet metni boş döndü, sesli özet atlanıyor.")
        return

    try:
        gemini_api_key = get_gemini_api_key()
    except RuntimeError as exc:
        # TTS her zaman Gemini kullanır (bkz. src/tts.py) - summarizer.llm_provider
        # "anthropic" olsa BİLE GEMINI_API_KEY tanımlı olmalı, yoksa sesli özet
        # atlanır (yazılı Günlük Özet bundan ETKİLENMEZ).
        logger.warning("%s Sesli özet sesi üretilemiyor, sesli özet atlanıyor.", exc)
        return

    audio_bytes = synthesize_turkish_speech(
        script,
        api_key=gemini_api_key,
        model=tts_cfg.get("gemini_model", "gemini-2.5-flash-preview-tts"),
        voice_name=tts_cfg.get("voice_name", "Kore"),
    )
    if audio_bytes is None:
        logger.warning("Sesli özet sesi üretilemedi, sesli özet atlanıyor.")
        return

    audio_sent = send_telegram_audio_to_chat_ids(
        chat_ids,
        audio_bytes,
        filename="sesli_ozet.mp3",
        caption=_AUDIO_CAPTION,
        title="Sesli Günlük Özet",
    )

    # Ses dosyasının altına yazılı özeti de ekle (bkz. görev tanımı: "kullanıcı
    # sesi dinlemeden de okuyabilsin") - caption alanı bunun için çok kısıtlı
    # (~1024 karakter) olduğundan AYRI bir mesaj olarak gönderilir.
    text_message = f"📝 <b>Yazılı özet:</b>\n\n{html.escape(script)}"
    send_telegram_message_to_chat_ids(chat_ids, text_message)

    logger.info(
        "Sesli özet gönderildi: %d haberden üretildi, %d/%d aboneye.",
        len(important),
        audio_sent,
        len(chat_ids),
    )
