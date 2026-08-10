"""Dış sistemlerden (bir Telegram kanal dinleyicisi, elle bir cURL isteği,
mobil bildirim yönlendirici vb.) PUSH edilen tekil haber/bildirim yüklerini
mevcut özetleme/kaydetme/bildirim pipeline'ına sokan modül.

Diğer fetcher'lardan (rss_fetcher, scrape_fetcher, licensed_aggregator)
MİMARİ OLARAK FARKLIDIR: onlar worker'ın zamanlanmış taramasında PERİYODİK
olarak ÇEKER (pull); bu modül ise dışarıdan aktif olarak İTİLEN (push) tekil
bir haberi ANLIK olarak işler (bkz. src/web/app.py > POST
/api/webhook/kap-bildirim, bu modülün tek çağıranı).

NEDEN VAR (2026-08-10): KAP (Kamuyu Aydınlatma Platformu, kap.org.tr)
robots.txt'i bot trafiğine WAF seviyesinde kapalı olduğundan doğrudan scrape
EDİLEMİYOR (bkz. README > "Eklenmeyen Kaynaklar"). Bunun yerine kullanıcı,
KAP bildirimlerini ANLIK olarak ileten üçüncü bir kaynağı (ör. bir Telegram
kanalı, bkz. src/fetchers/telegram_listener.py) webhook endpoint'ine
YÖNLENDİREREK aynı sonuca (anlık KAP bildirim işleme) kap.org.tr'ye hiç
istek atmadan ulaşır. Bu modül KAP'a ÖZGÜ değildir - herhangi bir dış
kaynaktan gelen tekil bir haber/bildirim metnini aynı şekilde işleyebilir.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.main import summarize_and_persist_groups
from src.models import NewsGroup, NewsItem

logger = logging.getLogger(__name__)

DEFAULT_SOURCE_NAME = "KAP (dış bildirim)"


@dataclass
class IncomingDisclosure:
    """Dış bir kaynaktan gelen tekil bir bildirimin normalize edilmiş hali -
    HTTP katmanından (bkz. src/web/app.py'deki Pydantic modeli) bilerek
    ayrı tutulur: bu modül FastAPI'ye bağımlı DEĞİLDİR, başka bir HTTP
    framework'ü veya doğrudan Python çağrısıyla da (ör. testte) kullanılabilir.
    """

    title: str
    text: str = ""
    ticker: str | None = None
    source: str = DEFAULT_SOURCE_NAME
    link: str = ""
    published_at: datetime | None = None


def _build_news_item(disclosure: IncomingDisclosure) -> NewsItem:
    title = disclosure.title.strip()
    if disclosure.ticker:
        ticker = disclosure.ticker.strip().upper()
        # Ticker zaten başlığın başında geçmiyorsa (ör. dinleyici mesajdan
        # "TICKER: ..." önekini ayrıca ayıklayıp `ticker` alanına koyduysa)
        # başa ekle - dashboard/Telegram'da hangi şirkete ait olduğu ilk
        # bakışta görünsün diye (mevcut kaynakların çoğu zaten başlıkta
        # şirket adını taşır, bu SADECE eksikse tamamlayan bir bonus).
        if ticker and not title.upper().startswith(ticker):
            title = f"{ticker}: {title}"

    published_at = disclosure.published_at or datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        # `src/deduplicator.py > group_similar_news` diğer TÜM kaynaklardan
        # gelen datetime'ların timezone-aware olduğunu varsayar (bkz. o
        # modülün fetcher'ları) - dışarıdan naive bir tarih gelirse burada
        # UTC varsayılır, aksi halde gruplama sırasında naive/aware
        # karşılaştırma hatası (TypeError) alınabilirdi.
        published_at = published_at.replace(tzinfo=timezone.utc)

    return NewsItem(
        title=title,
        link=disclosure.link.strip(),
        source=(disclosure.source or DEFAULT_SOURCE_NAME).strip(),
        published_at=published_at,
        raw_text=disclosure.text.strip(),
    )


def process_incoming_disclosure(disclosure: IncomingDisclosure, config: dict[str, Any]) -> None:
    """Tekil bir dış bildirimi ANINDA mevcut özetleme/önem skorlama/kayıt/
    bildirim pipeline'ından geçirir (bkz. src/main.py >
    summarize_and_persist_groups - worker'ın periyodik taramasının kullandığı
    AYNI fonksiyon, burada tek elemanlı bir grup listesiyle çağrılır).

    Aynı `title`'a sahip bir bildirim tekrar gelirse (ör. webhook çağıranın
    ağ hatası sonrası retry'ı, ya da dinleyicinin aynı mesajı iki kez
    yakalaması) `compute_group_key` + veritabanı kontrolü (bkz.
    _reuse_or_mark_for_summarization) sayesinde otomatik olarak idempotenttir
    - zaten skorlanmış bir kayıt varsa yeniden özetlenmez/tekrar bildirilmez.

    SENKRON/BLOKLAYAN bir fonksiyondur (LLM çağrısı + rate-limit bekleme +
    Telegram/Web Push gönderimi içerir, saniyeler sürebilir) - HTTP katmanı
    (bkz. src/web/app.py) bunu bir arka plan görevinde (FastAPI
    BackgroundTasks) çalıştırmalı ki istek hemen dönebilsin. Hiçbir durumda
    exception fırlatmaz (alttaki pipeline zaten kendi hatalarını izole eder,
    bkz. summarize_and_persist_groups) - bir bildirimin işlenememesi arka
    plan görevini/süreci etkilemez."""
    if not disclosure.title.strip():
        logger.warning("Boş başlıklı dış bildirim reddedildi, işlenmedi.")
        return

    item = _build_news_item(disclosure)
    group = NewsGroup(items=[item])
    logger.info(
        "Dış webhook bildirimi alındı, pipeline'a sokuluyor (kaynak=%s): %s",
        item.source,
        item.title,
    )
    try:
        summarize_and_persist_groups([group], config)
    except Exception:  # noqa: BLE001 - bir dış bildirimin işlenememesi arka plan görevini/sunucuyu etkilemesin
        logger.exception("Dış webhook bildirimi işlenirken beklenmeyen hata: %s", item.title)
