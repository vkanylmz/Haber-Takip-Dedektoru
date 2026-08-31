"""Gemini TTS (metin-sese) ile Türkçe konuşma sesi üretimi.

Sesli Günlük Özet özelliği için (bkz. src/voice_digest.py): Gemini'nin ses
üretim modeli (varsayılan `gemini-2.5-flash-preview-tts`, bkz. config.yaml >
tts.gemini_model) ham PCM (24kHz, 16-bit, mono) ses döndürür. Bunu
Telegram'ın `sendAudio` metoduyla düzgün oynatılabilir bir ses balonu olarak
göndermek için MP3'e kodluyoruz (bkz. `_pcm_to_mp3`, `lameenc` - saf Python
bağlaması, ffmpeg gibi bir sistem binary'si GEREKTİRMEZ).

Bu modül, src/summarizer.py'deki `Summarizer` sınıfından BİLİNÇLİ OLARAK
ayrı tutulur: metin üretimi (JSON yanıt bekleyen `generate_content` çağrıları)
ile ses üretimi (`response_modalities=["AUDIO"]`) tamamen farklı bir çağrı
şekli, aralarında paylaşılacak bir mantık yok.

Aynı GEMINI_API_KEY kullanılır (bkz. src/config.py > get_gemini_api_key) -
ek bir servis/anahtar GEREKMEZ.
"""

from __future__ import annotations

import logging

from google import genai
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

# Gemini TTS'in döndürdüğü ham PCM formatı (bkz. ai.google.dev/gemini-api/docs
# /speech-generation) - sabit, model tarafından garanti edilir.
_PCM_SAMPLE_RATE_HZ = 24000
_PCM_CHANNELS = 1
_PCM_SAMPLE_WIDTH_BYTES = 2

# Ses dosyası boyutu/kalite dengesi - konuşma (müzik değil) için 96kbps
# yeterince net, 1-2 dakikalık bir özet için dosya boyutunu de küçük tutar
# (Telegram'a hızlı yükleme).
_MP3_BITRATE_KBPS = 96


def _pcm_to_mp3(pcm_bytes: bytes) -> bytes:
    """Ham PCM (16-bit, mono, 24kHz) veriyi MP3'e kodlar (bkz. modül
    docstring'i - lameenc, ffmpeg gerektirmez)."""
    import lameenc

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(_MP3_BITRATE_KBPS)
    encoder.set_in_sample_rate(_PCM_SAMPLE_RATE_HZ)
    encoder.set_channels(_PCM_CHANNELS)
    encoder.set_quality(2)  # 2 = yüksek kalite (0=en yavaş/en iyi, 9=en hızlı/en düşük)
    mp3_data = encoder.encode(pcm_bytes)
    mp3_data += encoder.flush()
    # lameenc bytearray döner - python-telegram-bot'un InputFile'ı sadece
    # bytes/str/dosya-benzeri (`.read()` olan) nesneleri kabul eder, bytearray
    # İKİSİNE de UYMADIĞINDAN ("'bytearray' object has no attribute 'read'"
    # hatasıyla, gerçek bir denemede doğrulandı) burada bytes'a çevrilir.
    return bytes(mp3_data)


def synthesize_turkish_speech(
    text: str,
    api_key: str,
    model: str = "gemini-2.5-flash-preview-tts",
    voice_name: str = "Kore",
) -> bytes | None:
    """Verilen Türkçe metni Gemini TTS ile sese çevirir, MP3 byte'ları olarak
    döner. Herhangi bir hata durumunda (API hatası, boş yanıt, kodlama
    hatası) exception fırlatmaz, None döner - çağıran taraf (bkz.
    src/voice_digest.py) bu durumda sesli özeti atlar."""
    if not text.strip():
        return None

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=text,
            config=genai_types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=genai_types.SpeechConfig(
                    voice_config=genai_types.VoiceConfig(
                        prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name=voice_name)
                    )
                ),
            ),
        )
        pcm_bytes = response.candidates[0].content.parts[0].inline_data.data
    except Exception:  # noqa: BLE001 - ağ hatası, geçersiz anahtar, kota vb.
        logger.exception("Gemini TTS çağrısı başarısız oldu.")
        return None

    if not pcm_bytes:
        logger.warning("Gemini TTS boş ses verisi döndürdü.")
        return None

    try:
        return _pcm_to_mp3(pcm_bytes)
    except Exception:  # noqa: BLE001 - lameenc kodlama hatası
        logger.exception("PCM->MP3 kodlaması başarısız oldu.")
        return None
