"""KAP (Kamuyu Aydınlatma Platformu, kap.org.tr) özel durum açıklamalarını
çeken fetcher.

BİLİNÇLİ BİR POLİTİKA İSTİSNASI (2026-08-17, bkz. README > "Eklenen
Kaynaklar" > KAP satırı): bu proje normalde robots.txt'i okunamayan (WAF
tarafından engellenen) kaynakları eklemez (bkz. Investing.com/Handelsblatt/
Telegraph gerekçeleri) - `www.kap.org.tr/robots.txt` bugün hâlâ standart dışı
bir HTTP 666 WAF bloğu dönüyor (her denemede farklı bir "hata numarası").
Kullanıcı, bu belirli kaynak için KENDİ kararıyla bu politikaya bilinçli bir
istisna tanımıştır - bu yüzden diğer TÜM fetcher'ların aksine burada
`src.fetchers.base.is_allowed()` KASITLI OLARAK çağrılmaz (o fonksiyon zaten
666 gibi standart dışı bir kodu ">=400 ama 401/403 değil" dalına düşürüp
"izinli" sayardı - bunu örtük bir kaçak yol olarak kullanmak yerine, bu
istisnanın burada AÇIKÇA belgelenmesi tercih edildi).

Resmi/dokümante edilmiş bir API DEĞİLDİR: kap.org.tr'nin kendi
"bildirim-sorgu" sayfasının kullandığı iç JSON endpoint'i kullanılıyor (bkz.
GitHub'daki benzer açık kaynak yaklaşımlar - cemsinano/pykap,
enciyo/kap-tr-sdk, saidsurucu/borsa-mcp - hiçbiri resmi bir API kullanmıyor).
Bu GEÇİCİ bir çözümdür: MKK API Portal (apiportal.mkk.com.tr) başvurusu
sonuçlanır ve gerçek zamanlı özel durum açıklamalarını kapsadığı doğrulanırsa,
bu fetcher resmi/belgelenmiş endpoint'e geçirilecektir.

Diğer fetcher'lardan (rss_fetcher, scrape_fetcher) FARKLI olarak bu kaynak
`worker.py > _add_kap_fast_poll_job` ile AYRICA çok daha sık (varsayılan
120sn, bkz. config.yaml > kap_fast_poll) yoklanır - genel `worker.interval_minutes`
(15 dk) taraması özel durum açıklamaları için çok yavaş kalır. Aynı
disclosure'ın hem genel taramada hem hızlı yoklamada tekrar görülmesi
ZARARSIZDIR: mevcut `group_key`/`notified` idempotency mekanizması (bkz.
src/db.py, src/main.py > _reuse_or_mark_for_summarization) zaten aynı haberi
iki kez özetlemez/bildirmez - webhook.py'nin push yoluyla paylaştığı AYNI
garanti.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.fetchers.base import rate_limit
from src.models import NewsItem
from src.timezone_utils import TURKEY_TZ

logger = logging.getLogger(__name__)

# notifier.py bu değeri "bu kayıt KAP kaynaklı mı" kontrolünde kullanır (bkz.
# src/notifier.py > KAP_SOURCE_NAME) - config.yaml > sources'taki KAP
# girdisinin `name:` alanıyla AYNI kalmalıdır.
KAP_SOURCE_NAME = "KAP"

_DISCLOSURE_LIST_URL = "https://www.kap.org.tr/tr/api/disclosure/list/main"
_DETAIL_URL_TEMPLATE = "https://www.kap.org.tr/tr/Bildirim/{disclosure_index}"

# Varsayılan disclosureClass filtresi: ODA=Özel Durum Açıklaması,
# FR=Finansal Rapor, CA=Kurumsal İşlem. DUY (düzenleyici/idari duyuru) ve
# DG (diğer - ör. "Takasbank Günlük Bülten" gibi bültenler) BİLEREK
# dışarıda bırakılır - canlı testle (2026-08-17) bu ikisinin neredeyse
# tamamen gürültü olduğu doğrulandı.
_DEFAULT_DISCLOSURE_CLASSES = ["ODA", "FR", "CA"]
_DEFAULT_MEMBER_TYPES = ["IGS"]
_DEFAULT_LOOKBACK_MINUTES = 30


def _parse_publish_date(raw: str | None) -> datetime | None:
    """KAP'ın "DD.MM.YYYY HH:MM:SS" formatındaki (Türkiye yerel saati)
    publishDate alanını tz-aware UTC datetime'a çevirir. Ayrıştırılamazsa
    None döner - çağıran taraf bunu diğer fetcher'lardaki (bkz.
    rss_fetcher.py > _parse_published) AYNI "tarih bilinmiyor ama haberi
    yine de işle" mantığıyla ele alır."""
    if not raw:
        return None
    try:
        naive = datetime.strptime(raw, "%d.%m.%Y %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=TURKEY_TZ).astimezone(timezone.utc)


def fetch_kap(source_cfg: dict[str, Any], app_cfg: dict[str, Any]) -> list[NewsItem]:
    """KAP'ın iç JSON endpoint'inden son `lookback_minutes` içindeki özel
    durum açıklamalarını çeker. Bir hata oluşursa exception fırlatmaz; boş
    liste döner ve hatayı loglar (diğer TÜM fetcher'larla AYNI hata izolasyonu
    deseni, bkz. rss_fetcher.py)."""
    name = source_cfg.get("name", KAP_SOURCE_NAME)
    user_agent = app_cfg.get("user_agent", "FinansHaberToplayiciBot/1.0")
    timeout = app_cfg.get("request_timeout", 15)
    min_interval = app_cfg.get("rate_limit_seconds", 2.0)
    max_items = app_cfg.get("max_articles_per_source", 15)

    disclosure_classes = set(source_cfg.get("disclosure_classes", _DEFAULT_DISCLOSURE_CLASSES))
    member_types = source_cfg.get("member_types", _DEFAULT_MEMBER_TYPES)
    lookback_minutes = source_cfg.get("lookback_minutes", _DEFAULT_LOOKBACK_MINUTES)

    # robots.txt kontrolü BİLEREK yok (bkz. modül docstring'i) - ama alan adı
    # bazlı nazik istek aralığı (rate_limit) diğer TÜM fetcher'larla AYNI
    # şekilde uygulanır; bu, genel tarama (15 dk) ile hızlı KAP yoklamasının
    # (120sn, bkz. worker.py) aynı ana denk gelmesi durumunda bile kap.org.tr'ye
    # `min_interval`den daha sık istek atılmamasını GARANTİ eder (paylaşımlı,
    # kilitli, alan-adı bazlı sayaç - bkz. src/fetchers/base.py).
    rate_limit(_DISCLOSURE_LIST_URL, min_interval)

    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=lookback_minutes)
    # KAP API'si Türkiye yerel saatiyle DD.MM.YYYY bekliyor (canlı testle
    # doğrulandı, 2026-08-17).
    payload = {
        "fromDate": since.astimezone(TURKEY_TZ).strftime("%d.%m.%Y"),
        "toDate": now.astimezone(TURKEY_TZ).strftime("%d.%m.%Y"),
        "disclosureType": None,
        "fundTypes": [],
        "memberTypes": member_types,
        "mkkMemberOid": None,
    }

    try:
        response = httpx.post(
            _DISCLOSURE_LIST_URL,
            json=payload,
            headers={"User-Agent": user_agent, "Content-Type": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        raw_items = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.error("%s: istek başarısız oldu: %s", name, exc)
        return []

    if not isinstance(raw_items, list):
        logger.error("%s: beklenmeyen yanıt formatı (liste bekleniyordu): %r", name, type(raw_items))
        return []

    items: list[NewsItem] = []
    for raw in raw_items:
        basic = (raw or {}).get("disclosureBasic") or {}

        if basic.get("disclosureClass") not in disclosure_classes:
            continue

        disclosure_index = basic.get("disclosureIndex")
        title = (basic.get("title") or "").strip()
        if disclosure_index is None or not title:
            continue

        publish_dt = _parse_publish_date(basic.get("publishDate"))
        # Dar pencereden (fromDate/toDate) daha eski bir kayıt döndüyse atla
        # - KAP API'si geniş aralıklarda ~30 kayıtta kesildiğinden (canlı
        # testle tespit edildi), dar pencere zaten bunu büyük ölçüde önlüyor;
        # bu ek kontrol savunmacı bir son kat.
        if publish_dt is not None and publish_dt < since:
            continue

        company = (basic.get("companyTitle") or "").strip()
        display_title = (
            f"{company}: {title}" if company and not title.upper().startswith(company.upper()) else title
        )

        items.append(
            NewsItem(
                title=display_title,
                link=_DETAIL_URL_TEMPLATE.format(disclosure_index=disclosure_index),
                source=name,
                published_at=publish_dt,
                raw_text=(basic.get("summary") or "").strip(),
            )
        )

    items.sort(key=lambda i: i.published_at or now, reverse=True)
    items = items[:max_items]

    logger.info(
        "%s: %d özel durum açıklaması çekildi (disclosure_classes=%s, pencere=%d dk)",
        name,
        len(items),
        sorted(disclosure_classes),
        lookback_minutes,
    )
    return items
