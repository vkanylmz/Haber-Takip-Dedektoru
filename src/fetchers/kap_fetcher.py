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

DÜZELTME (2026-08-17, "sessiz başarısızlık" teşhisi): İLK sürüm
`/tr/api/disclosure/list/main` endpoint'ini (GitHub'daki `enciyo/kap-tr-sdk`
projesinden alınmıştı) kullanıyordu - bu endpoint HTTP 200 dönmeye devam
ediyordu ama İÇERİĞİ DONMUŞTU: istenen tarih aralığı ne olursa olsun (son
30dk, son 48 saat, son 3 gün) hep AYNI, en yenisi 14 Ağustos 18:06'da kalan
birkaç kaydı döndürüyordu - hata/timeout YOKTU, bu yüzden fark edilmesi zordu.
Gerçek kap.org.tr sitesi (`/tr/bildirim-sorgu` sayfası, tarayıcı ile Network
sekmesi izlenerek) GERÇEKTE `/tr/api/disclosure/members/byCriteria`
endpoint'ini, TAMAMEN FARKLI bir payload şekliyle (`fromDate`/`toDate`
"YYYY-MM-DD" formatında - "DD.MM.YYYY" DEĞİL, `memberType` TEKİL string -
liste DEĞİL, ~15 ek zorunlu boş alan) kullandığı tespit edildi - bu doğru
endpoint canlı testte AYNI gün için 73 kayıt döndürdü (eskisi 0). Yanıt şekli
de FARKLI: eski `{"disclosureBasic": {...}}` iç içe yapısı yerine düz bir
obje (`kapTitle`, `summary`, `subject`, `disclosureIndex` üst seviyede).
Cursor/state TEMİZLENMESİ GEREKMEDİ - bu proje zaten cursor kullanmıyor
(group_key idempotency'sine güveniyor) ve kırık dönemde DB'ye hiç KAP kaydı
YAZILMAMIŞTI (0 kayıt) - temiz bir geçiş.
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

# kap.org.tr'nin "bildirim-sorgu" sayfasının GERÇEKTE kullandığı endpoint
# (bkz. yukarıdaki "DÜZELTME" notu - eski `/tr/api/disclosure/list/main`
# donmuş/bayat veri döndürüyordu, BUNUNLA değiştirildi).
_DISCLOSURE_LIST_URL = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
_DETAIL_URL_TEMPLATE = "https://www.kap.org.tr/tr/Bildirim/{disclosure_index}"

# Varsayılan disclosureClass filtresi: ODA=Özel Durum Açıklaması,
# FR=Finansal Rapor, CA=Kurumsal İşlem. DUY (düzenleyici/idari duyuru) ve
# DG (diğer - ör. "Takasbank Günlük Bülten" gibi bültenler) BİLEREK
# dışarıda bırakılır - canlı testle (2026-08-17) bu ikisinin neredeyse
# tamamen gürültü olduğu doğrulandı. `disclosureClass` filtresi endpoint'in
# KENDİSİNE (request body'sinde) tek bir string olarak verilebiliyor ama
# BİRDEN FAZLA sınıf istediğimizden (ODA+FR+CA) request'te boş bırakılıp
# (`""` = tümü) SONRADAN client-side filtrelenir.
_DEFAULT_DISCLOSURE_CLASSES = ["ODA", "FR", "CA"]
_DEFAULT_MEMBER_TYPE = "IGS"
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
    member_type = source_cfg.get("member_type", _DEFAULT_MEMBER_TYPE)
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
    # KAP'ın GERÇEK sitesinin kullandığı format: Türkiye yerel tarihiyle
    # "YYYY-MM-DD" (bkz. modül başındaki "DÜZELTME" notu - eski endpoint/format
    # "DD.MM.YYYY" bekliyordu, YANLIŞTI/donmuş veri döndürüyordu). Diğer ~15
    # alan, sitenin kendi isteğinde de hep boş/varsayılan gönderilen zorunlu
    # alanlar - eksik bırakılırsa endpoint'in nasıl davrandığı test edilmedi,
    # bu yüzden TAM olarak sitenin gönderdiği şekliyle dahil edildi.
    payload = {
        "fromDate": since.astimezone(TURKEY_TZ).strftime("%Y-%m-%d"),
        "toDate": now.astimezone(TURKEY_TZ).strftime("%Y-%m-%d"),
        "memberType": member_type,
        "mkkMemberOidList": [],
        "inactiveMkkMemberOidList": [],
        "disclosureClass": "",
        "subjectList": [],
        "isLate": "",
        "mainSector": "",
        "sector": "",
        "subSector": "",
        "marketOid": "",
        "index": "",
        "bdkReview": "",
        "bdkMemberOidList": [],
        "year": "",
        "term": "",
        "ruleType": "",
        "period": "",
        "fromSrc": False,
        "srcCategory": "",
        "disclosureIndexList": [],
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
        raw = raw or {}

        if raw.get("disclosureClass") not in disclosure_classes:
            continue

        disclosure_index = raw.get("disclosureIndex")
        # Yeni yanıt şemasında `subject` sabit bir taksonomi kategorisi (ör.
        # "Pay Dışında Sermaye Piyasası Aracı İşlemlerine İlişkin Bildirim"),
        # `summary` ise İLGİLİ KAYDA ÖZGÜ asıl içerik (ör. "TRSGFYHK2614 ISIN
        # Kodlu İhraç Kupon Ödemesi") - bu yüzden `summary` varsa asıl başlık
        # olarak o kullanılır, yoksa (nadiren boş olabilir) `subject`'e
        # düşülür. `raw_text`'e her zaman `subject` konur - LLM'e ek bağlam
        # (bu kaydın hangi genel kategoriye girdiği) sağlar.
        content = (raw.get("summary") or "").strip()
        subject = (raw.get("subject") or "").strip()
        title = content or subject
        if disclosure_index is None or not title:
            continue

        publish_dt = _parse_publish_date(raw.get("publishDate"))
        # Dar pencereden (fromDate/toDate) daha eski bir kayıt döndüyse atla -
        # savunmacı bir son kat (normalde zaten dar pencere bunu önler).
        if publish_dt is not None and publish_dt < since:
            continue

        company = (raw.get("kapTitle") or "").strip()
        display_title = (
            f"{company}: {title}" if company and not title.upper().startswith(company.upper()) else title
        )

        # stockCodes: KAP API'sinin kendi, OTORİTER hisse kodu alanı (bazen
        # virgülle ayrılmış birden fazla kod - ör. "YKB, YKBNK" - eski+yeni
        # kod veya farklı enstrüman sınıfı için, bkz. 2026-08-17 canlı test).
        # kap_subject/kap_stock_codes ayrı, DAİMA dolu alanlar olarak taşınır
        # (raw_text'in aksine - o sadece content varsa subject taşır) - bkz.
        # src/models.py > NewsItem, src/summarizer.py > kap_category/ticker.
        stock_codes = (raw.get("stockCodes") or "").strip()

        items.append(
            NewsItem(
                title=display_title,
                link=_DETAIL_URL_TEMPLATE.format(disclosure_index=disclosure_index),
                source=name,
                published_at=publish_dt,
                raw_text=subject if content else "",
                kap_subject=subject,
                kap_stock_codes=stock_codes,
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
