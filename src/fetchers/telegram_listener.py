"""KULLANICININ KENDİ Telegram hesabıyla (bot API'sinin AKSİNE bir "user
client") belirli bir Telegram kanalını dinleyip yeni mesajları anlık olarak
webhook endpoint'ine (bkz. src/web/app.py > POST /api/webhook/kap-bildirim,
src/fetchers/webhook.py) ileten İSTEĞE BAĞLI/AYRI bir servis.

Motivasyon (bkz. README > "Eklenmeyen Kaynaklar" > KAP): kap.org.tr doğrudan
scrape edilemiyor (robots.txt WAF seviyesinde bot trafiğine kapalı, resmi/
ücretsiz bir RSS/API yok). KAP bildirimlerini ANLIK olarak yeniden yayınlayan
üçüncü taraf Telegram kanalları bu boşluğu doldurur - bu modül böyle bir
kanalı dinleyip mesajları mevcut özetleme/bildirim pipeline'ına sokar,
kap.org.tr'ye hiç istek atmadan.

ÖNEMLİ - VERİ GÜVENİLİRLİĞİ: Bu modül YALNIZCA belirttiğiniz kanaldaki
mesajları AYNEN iletir. KAP'ın kendisiyle hiçbir doğrudan bağlantısı yoktur -
kanalın doğruluğu/hızı/kapsamı TAMAMEN o kanalı işleten üçüncü tarafa
bağlıdır. Hangi kanalı dinleyeceğinize (config.yaml > telegram_listener.channels)
siz karar verirsiniz; bu proje hiçbir kanalı önermez/doğrulamaz.

NEDEN AYRI BİR SÜREÇ (worker.py/main.py'nin İÇİNDE OTOMATİK BAŞLAMAZ):
  1. Telethon "user client"ı GERÇEK bir Telegram hesabıyla giriş yapar -
     telefon numarası + SMS/uygulama koduyla ETKİLEŞİMLİ bir İLK GİRİŞ
     gerektirir (bkz. aşağıdaki "İlk Kurulum"). Bu, `python main.py`'nin
     otomatik/gözetimsiz başlamasını (ör. bir sunucuda) BOZAR.
  2. Kendi API_ID/API_HASH'i (bot token'dan TAMAMEN FARKLI, my.telegram.org'dan
     alınan kişisel geliştirici kimlik bilgileri) ve kendi oturum dosyası
     gerektirir - `python main.py`'nin zaten kullandığı BOT hesabından
     bağımsız, AYRI bir kimlik.
  3. `telethon` paketi VARSAYILAN OLARAK KURULU DEĞİLDİR (bkz.
     requirements.txt > İSTEĞE BAĞLI bölümü) - bu modül import edilmeden
     `python main.py`/`worker.py` kurulu değilse bile SORUNSUZ çalışır
     (diğer opsiyonel bağımlılıklarla - eventregistry, playwright - AYNI
     "lazy import" deseni, bkz. aşağıdaki run_listener).

ÇALIŞTIRMA (proje kök dizininden, `python main.py` ÇALIŞIRKEN AYRI bir
terminalde - webhook endpoint'i AYAKTA OLMALI):
    python -m src.fetchers.telegram_listener

İLK KURULUM (tek seferlik, ETKİLEŞİMLİ):
  1. pip install telethon (bkz. requirements.txt).
  2. https://my.telegram.org/apps adresinden KENDİ Telegram hesabınızla
     giriş yapıp yeni bir "uygulama" oluşturarak kişisel bir api_id/api_hash
     alın.
  3. .env dosyasına ekleyin:
       TELEGRAM_LISTENER_API_ID=...
       TELEGRAM_LISTENER_API_HASH=...
       WEBHOOK_INGEST_SECRET=... (main.py ilk çalıştığınızda otomatik
         üretilip .env'e eklenmiş olmalı - bkz. src/config_setup.py; yoksa
         kendiniz rastgele bir değer girip AYNI değeri hem burada hem de
         webhook'un kontrol ettiği .env değişkeninde kullanın)
  4. config.yaml > telegram_listener.channels'a dinlenecek kanalın kullanıcı
     adını/ID'sini ekleyin (ör. ["@ornek_kap_kanali"]).
  5. `python -m src.fetchers.telegram_listener` komutunu çalıştırın - İLK
     seferde telefon numaranızı ve size SMS/uygulama ile gelen giriş kodunu
     (ve varsa 2FA şifrenizi) İSTEYECEKTİR (interaktif terminal girişi).
     Başarılı girişten sonra oturum `data/state/telegram_listener.session`
     dosyasına kaydedilir - SONRAKİ çalıştırmalarda tekrar sorulmaz (bir
     Windows görev zamanlayıcısı/`pm2`/`nssm` ile gözetimsiz servis olarak
     çalıştırılabilir hale gelir).

GÜVENLİK: `data/state/telegram_listener.session` dosyası hesabınıza TAM
erişim sağlayan bir oturum belirteci içerir (bir bot token'ından bile daha
hassastır) - `.gitignore`'da zaten `data/state/` altında olduğundan repoya
commit edilmez, ama yine de bu dosyayı KİMSEYLE PAYLAŞMAYIN.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import httpx

from src.config import load_config
from src.logging_setup import setup_logging

logger = logging.getLogger(__name__)

_SESSION_PATH = Path("data") / "state" / "telegram_listener.session"

# Mesaj metninden baştaki "TICKER: açıklama" örüntüsünü ayıklamaya çalışır -
# KAP bildirimlerini yeniden yayınlayan üçüncü taraf kanalların çoğu bu
# kalıbı kullanır (bkz. README > KAP özet format örnekleri). Eşleşmezse
# ticker None kalır - webhook endpoint'i/özetleyici yine de ham metni
# işleyebilir (bkz. src/fetchers/webhook.py), bu SADECE bir bonus
# zenginleştirme, zorunlu bir ayrıştırma DEĞİL.
_TICKER_PREFIX_RE = re.compile(r"^\s*([A-ZİŞÖÇĞÜ]{2,6})\s*[:\-–]\s*(.+)$", re.UNICODE | re.DOTALL)

_HTTP_TIMEOUT_SECONDS = 15.0


def _parse_message_text(text: str) -> tuple[str | None, str]:
    """Mesaj metninden (varsa) baştaki TICKER önekini ayırır, geri kalanını
    başlık olarak döner. Eşleşmezse (None, orijinal_metin) döner."""
    match = _TICKER_PREFIX_RE.match(text.strip())
    if not match:
        return None, text.strip()
    ticker, rest = match.groups()
    return ticker, rest.strip()


async def _forward_to_webhook(
    webhook_url: str, webhook_secret: str, title: str, ticker: str | None, raw_text: str
) -> None:
    """Yakalanan mesajı webhook endpoint'ine iletir - ASENKRON (httpx'in
    AsyncClient'ı ile): Telethon'un tek asyncio event loop'unu senkron bir
    HTTP çağrısıyla BLOKLAMAMAK için önemli (bkz. çağıran taraf, _on_new_message
    handler'ı da async'tir). Tek bir mesajın iletilememesi (ağ hatası, webhook
    geçici olarak kapalı vb.) dinleyicinin diğer mesajları işlemeye devam
    etmesini ENGELLEMEMELİ - exception burada tamamen yutulur, sadece loglanır.
    """
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                webhook_url,
                json={
                    "title": title,
                    "text": raw_text,
                    "ticker": ticker,
                    "source": "KAP (Telegram kanal dinleyicisi)",
                },
                headers={"X-Webhook-Secret": webhook_secret},
            )
            response.raise_for_status()
        logger.info("Webhook'a iletildi: %s", title[:80])
    except Exception:  # noqa: BLE001 - tek bir mesajın iletilememesi dinleyiciyi durdurmasın
        logger.exception("Mesaj webhook'a iletilemedi: %s", title[:80])


def run_listener() -> None:
    """Dinleyiciyi başlatır ve süreç boyunca (Ctrl+C'ye kadar) çalıştırır.
    Eksik bağımlılık/yapılandırma durumlarında (paket kurulu değil, API
    kimlik bilgisi/kanal/webhook sırrı eksik) exception FIRLATMAZ - net bir
    hata loglayıp sessizce döner (diğer opsiyonel entegrasyonlarla AYNI
    hata izolasyonu deseni, bkz. modül docstring'i)."""
    try:
        from telethon import TelegramClient, events
    except ImportError:
        logger.error(
            "'telethon' paketi kurulu değil. Kurmak için: pip install telethon "
            "(bkz. requirements.txt > İSTEĞE BAĞLI bölümü, modül docstring'i > İlk Kurulum)."
        )
        return

    config = load_config()
    setup_logging(config.get("app", {}).get("output_dir", "data"))

    listener_cfg = config.get("telegram_listener", {})
    channels = listener_cfg.get("channels") or []
    if not channels:
        logger.error(
            "config.yaml > telegram_listener.channels boş - dinlenecek en az bir "
            "kanal belirtmelisiniz (bkz. modül docstring'i > İlk Kurulum). "
            "Dinleyici başlatılmadı."
        )
        return

    api_id_str = os.environ.get("TELEGRAM_LISTENER_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_LISTENER_API_HASH", "").strip()
    if not api_id_str or not api_hash:
        logger.error(
            "TELEGRAM_LISTENER_API_ID / TELEGRAM_LISTENER_API_HASH .env'de tanımlı "
            "değil (bkz. modül docstring'i > İlk Kurulum, https://my.telegram.org/apps). "
            "Dinleyici başlatılmadı."
        )
        return
    try:
        api_id = int(api_id_str)
    except ValueError:
        logger.error("TELEGRAM_LISTENER_API_ID sayısal bir değer olmalı, '%s' geçersiz.", api_id_str)
        return

    webhook_url = listener_cfg.get("webhook_url") or "http://127.0.0.1:8000/api/webhook/kap-bildirim"
    webhook_secret = os.environ.get("WEBHOOK_INGEST_SECRET", "").strip()
    if not webhook_secret:
        logger.error(
            "WEBHOOK_INGEST_SECRET .env'de tanımlı değil - webhook endpoint'i zaten "
            "bunsuz TÜM istekleri reddedecek (bkz. src/web/app.py > _check_webhook_secret). "
            "Dinleyici başlatılmadı."
        )
        return

    _SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(_SESSION_PATH), api_id, api_hash)

    @client.on(events.NewMessage(chats=channels))
    async def _on_new_message(event) -> None:  # noqa: ANN001 - Telethon'un kendi event tipi
        text = (event.raw_text or "").strip()
        if not text:
            return
        ticker, title = _parse_message_text(text)
        logger.info("Yeni kanal mesajı yakalandı (ticker=%s): %s", ticker, title[:80])
        await _forward_to_webhook(webhook_url, webhook_secret, title[:280], ticker, text)

    logger.info("Telegram kanal dinleyicisi başlatılıyor (kanallar: %s)...", channels)
    client.start()
    logger.info(
        "Telegram kanal dinleyicisi bağlandı, mesajlar bekleniyor -> %s (Ctrl+C ile durdurun).",
        webhook_url,
    )
    try:
        client.run_until_disconnected()
    except KeyboardInterrupt:
        logger.info("Telegram kanal dinleyicisi durduruldu.")


if __name__ == "__main__":
    run_listener()
