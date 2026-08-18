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

import html
import logging
import re
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


# --------------------------------------------------------------------------
# Bildirim detay sayfası zenginleştirmesi (2026-08-18, bkz. kullanıcı isteği
# ve önceki oturumdaki araştırma - PUSULA PORTFÖY/BALSU GIDA örneği)
#
# KAP'ın liste API'sindeki `summary` alanı (bkz. yukarıdaki `content`)
# ÇOĞU ZAMAN kısa/genel bir tebliğ-madde referansından ibaret ("Özel
# Durumlar Tebliği'nin 12-(4). maddesi gereğince yapılan açıklama") -
# ASIL somut içerik (işlem tutarı, önce/sonra oranları, tarih vb.) SADECE
# bildirim DETAY sayfasında (kap.org.tr/tr/Bildirim/{index}) var, iki
# FARKLI şekilde:
#   1) Serbest metin anlatım (HTML'de `text-block-value` class'lı bir div
#      içinde, sunucu tarafında render ediliyor) - DÜZ bir httpx GET ile
#      bile yakalanabiliyor (canlı testte doğrulandı, BALSU örneği).
#   2) Yapılandırılmış "Özet Bilgi" tablosu (etiket-değer çiftleri) - bu
#      veri sayfaya CLIENT-SIDE JS ile SONRADAN yükleniyor, düz httpx GET
#      bunu KAÇIRIYOR (canlı testte doğrulandı, KARSU örneği - httpx boş
#      döndü ama Playwright/gerçek tarayıcı render'ında tam tablo geldi).
#
# Bu yüzden İKİ AŞAMALI bir strateji: ÖNCE ucuz/hızlı httpx denenir (çoğu
# durumda yeterli olabilir), SADECE o boş dönerse PAHALI Playwright
# yedeğine düşülür. PDF eki İNDİRİLMEZ - araştırmada iki örnekte de
# (serbest metin VE tablo) PDF'e hiç gerek kalmadan aynı veri HTML/DOM'da
# zaten mevcuttu.
# --------------------------------------------------------------------------

# Liste API'sinin kendi `summary` alanı bu uzunluktan KISAYSA (ör. boş ya
# da sadece bir tebliğ-madde referansı gibi genel bir cümle) detay sayfası
# zenginleştirmesi denenir - zaten yeterince zengin bir `summary`'si olan
# kayıtlar için (ör. uzun, sayı dolu bir açıklama) gereksiz yere ekstra
# ağ isteği/gecikme YARATILMAZ. Eşik, canlı örneklerin (Balsu: ~0 karakter
# çünkü content boştu; KARSU: subject başlığa düştü) gözlemiyle seçildi.
_ENRICHMENT_CONTENT_LEN_THRESHOLD = 200

# Aynı disclosure_index bir sonraki 120sn'lik hızlı yoklamada (30dk'lık
# lookback penceresi içinde kaldığı sürece) TEKRAR TEKRAR dönebilir (bkz.
# modül docstring'i, "aynı disclosure'ın iki kez görülmesi zararsız" notu)
# - ama bu zenginleştirme AĞIR bir işlem (özellikle Playwright yedeği),
# aynı kayıt için tekrar tekrar çalıştırmak İSRAF olur. Süreç ömrü boyunca
# geçerli basit bir bellek-içi önbellek (DB/disk DEĞİL - restart'ta
# sıfırlanması ZARARSIZ, sadece bir sonraki taramada bir kez daha
# hesaplanır). None değeri de (denendi ama hiçbir şey bulunamadı) ayrı
# tutulur - aksi halde her seferinde tekrar denenip sonuçsuz kalırdı.
_detail_text_cache: dict[Any, str | None] = {}

# Playwright'ın `wait_until="networkidle"` beklemesi bazı KAP sayfalarında
# HİÇ tetiklenmiyor (canlı testte doğrulandı, GEZİNOMİ örneği - 30sn
# timeout'a takıldı, muhtemelen sayfa açık bir bağlantı/poll tutuyor). Bu
# yüzden "domcontentloaded" + sabit bir bekleme kullanılır - client-side
# taxonomy tablosunun dolması için yeterli (canlı testte ~1-2sn'de
# doluyordu, güvenlik payıyla 3sn).
_BROWSER_NAV_TIMEOUT_MS = 15000
_BROWSER_SETTLE_MS = 3000

_TEXT_BLOCK_MARKER = 'class=\\"text-block-value\\"'
_TEXT_BLOCK_MARKER_PLAIN = 'class="text-block-value"'
_TEXT_BLOCK_END_MARKERS = (
    "\\u003c/p\\u003e\\u003c/p\\u003e\\u003c/div\\u003e",
    "</p></p></div>",
    "</div></div></td>",
)


def _extract_narrative_from_html(page_html: str) -> str | None:
    """Next.js RSC payload'ı içine ÇİFT-escape edilmiş olarak gömülü serbest
    metin açıklamayı (varsa) çıkarır - bkz. yukarıdaki modül notu. Bulamazsa
    None döner (çağıran taraf Playwright yedeğine düşer)."""
    idx = page_html.find(_TEXT_BLOCK_MARKER)
    if idx != -1:
        chunk = page_html[idx : idx + 4000]
        try:
            chunk = chunk.encode().decode("unicode_escape").encode("latin1").decode("utf-8", errors="replace")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return None
    else:
        idx = page_html.find(_TEXT_BLOCK_MARKER_PLAIN)
        if idx == -1:
            return None
        chunk = page_html[idx : idx + 4000]

    for end_marker in _TEXT_BLOCK_END_MARKERS:
        pos = chunk.find(end_marker)
        if pos != -1:
            chunk = chunk[: pos + len(end_marker)]
            break
    else:
        chunk = chunk[:2000]

    # `chunk` bir `<div class="text-block-value">` etiketinin TAM BAŞINDAN
    # DEĞİL, `class="..."` ÖZNİTELİĞİNİN kendisinden başlıyor (bkz. yukarıdaki
    # `.find(_TEXT_BLOCK_MARKER...)`) - bu yüzden açılış `<div ` parçası
    # regex'e hiç girmiyor, kapanış `>` öncesindeki kalıntı ("...value">")
    # düz metne SIZAR. Asıl etiket temizliğinden ÖNCE bu kalıntı ayrıca kesilir.
    text_only = re.sub(r'^[^>]*>', "", chunk, count=1)
    text_only = re.sub(r"<[^>]+>", " ", text_only)
    text_only = html.unescape(text_only)
    text_only = re.sub(r"\s+", " ", text_only).strip()
    return text_only or None


def _fetch_detail_narrative_via_http(disclosure_index: Any, user_agent: str, timeout: float) -> str | None:
    """Ucuz/hızlı ilk deneme - sadece serbest metin anlatımı yakalayabilir
    (bkz. modül notu). Herhangi bir hata durumunda (diğer TÜM fetcher
    fonksiyonlarıyla AYNI izolasyon deseniyle) None döner, exception
    fırlatmaz."""
    url = _DETAIL_URL_TEMPLATE.format(disclosure_index=disclosure_index)
    try:
        response = httpx.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
        response.raise_for_status()
    except Exception:  # noqa: BLE001
        return None
    return _extract_narrative_from_html(response.text)


def _extract_table_text_from_page(page: Any) -> str | None:
    """Zaten navigate edilmiş/beklenmiş bir Playwright `Page`'den "Özet
    Bilgi" taksonomi tablosunun metnini çıkarır - bkz. `_fetch_detail_table_via_browser`
    (tekli/bağımsız kullanım) ve `_maybe_enrich_with_detail_text` (PAYLAŞILAN
    tek bir tarayıcı ÖRNEĞİYLE, birden fazla kayıt için - bkz. o fonksiyondaki
    performans notu) tarafından ORTAK kullanılır."""
    body_text = page.inner_text("body")
    start = body_text.find("Özet Bilgi")
    if start == -1:
        return None
    end = body_text.find("Bildirim Ekleri", start)
    section = body_text[start : end if end != -1 else start + 3000]
    section = re.sub(r"[ \t]+", " ", section)
    section = re.sub(r"\n{2,}", "\n", section).strip()
    return section or None


def _fetch_detail_table_via_browser(disclosure_index: Any) -> str | None:
    """Pahalı yedek - TEK BİR kayıt için, kendi tarayıcı örneğini açıp kapatır
    (bkz. `_maybe_enrich_with_detail_text`'in TOPLU/paylaşımlı sürümü - ana
    `fetch_kap` akışı O'nu kullanır, bu fonksiyon bağımsız testler/tekli
    kullanım içindir). `playwright` kurulu değilse (opsiyonel bağımlılık,
    bkz. requirements.txt) veya herhangi bir hata/timeout olursa None döner."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.debug("playwright kurulu değil, KAP detay sayfası tablo zenginleştirmesi atlanıyor.")
        return None

    url = _DETAIL_URL_TEMPLATE.format(disclosure_index=disclosure_index)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=_BROWSER_NAV_TIMEOUT_MS)
                page.wait_for_timeout(_BROWSER_SETTLE_MS)
                return _extract_table_text_from_page(page)
            finally:
                browser.close()
    except Exception:  # noqa: BLE001
        return None


# Bir `fetch_kap()` çağrısında en fazla bu kadar YENİ (önbellekte olmayan)
# kayıt zenginleştirilir - canlı testte (2026-08-18) 15 kayıt için 63 saniye
# sürdüğü ölçüldü, bu 120sn'lik KAP hızlı-yoklama döngüsünün YARISINDAN
# FAZLASI - ardışık döngülerin üst üste binmesi (APScheduler "maximum
# number of running instances" uyarısı, bkz. worker.py > interval_minutes
# 1->5 geçmişi, AYNI risk) riskini doğurur. Önbellek sayesinde bu sınır
# pratikte SADECE ilk çalıştırmada/ani patlamalarda devreye girer - normal
# seyirde 120sn'de birkaç YENİ bildirim gelir, sınırın altında kalır.
_MAX_ENRICH_PER_CALL = 6


def _maybe_enrich_with_detail_text(
    items: list[NewsItem],
    contents_by_disclosure_index: dict[Any, str],
    user_agent: str,
    timeout: float,
    min_interval: float,
) -> None:
    """`items` listesindeki (EN FAZLA `_MAX_ENRICH_PER_CALL` tanesi - bkz.
    yukarıdaki not), liste API'sinin kendi `summary`'si YETERSİZ kalan
    kayıtları bildirim detay sayfasından zenginleştirir. `items[i].raw_text`'i
    YERİNDE (in-place) günceller. Herhangi bir kayıt için zenginleştirme
    başarısız/gereksiz olursa o kayıt eski (subject-bazlı) `raw_text`'iyle
    aynen kalır - bu fonksiyon ASLA exception fırlatmaz, ana `fetch_kap`
    akışını ETKİLEMEZ.

    Performans: TEK bir Playwright tarayıcı ÖRNEĞİ bu ÇAĞRI boyunca
    PAYLAŞILIR (her kayıt için ayrı ayrı başlatıp kapatmak YERİNE) - tarayıcı
    başlatma maliyeti (~1-2sn) sadece BİR KEZ ödenir, kayıt başına değil."""
    candidates = []
    for item in items:
        disclosure_index = item.link.rsplit("/", 1)[-1]
        content = contents_by_disclosure_index.get(disclosure_index, "")
        if len(content) < _ENRICHMENT_CONTENT_LEN_THRESHOLD and disclosure_index not in _detail_text_cache:
            candidates.append((item, disclosure_index))
    candidates = candidates[:_MAX_ENRICH_PER_CALL]

    needs_browser: list[tuple[NewsItem, Any]] = []
    for item, disclosure_index in candidates:
        rate_limit(_DETAIL_URL_TEMPLATE, min_interval)
        enriched = _fetch_detail_narrative_via_http(disclosure_index, user_agent, timeout)
        if enriched:
            _detail_text_cache[disclosure_index] = enriched
        else:
            needs_browser.append((item, disclosure_index))

    if needs_browser:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    for item, disclosure_index in needs_browser:
                        rate_limit(_DETAIL_URL_TEMPLATE, min_interval)
                        url = _DETAIL_URL_TEMPLATE.format(disclosure_index=disclosure_index)
                        try:
                            page = browser.new_page()
                            try:
                                page.goto(url, wait_until="domcontentloaded", timeout=_BROWSER_NAV_TIMEOUT_MS)
                                page.wait_for_timeout(_BROWSER_SETTLE_MS)
                                _detail_text_cache[disclosure_index] = _extract_table_text_from_page(page)
                            finally:
                                page.close()
                        except Exception:  # noqa: BLE001
                            _detail_text_cache[disclosure_index] = None
                finally:
                    browser.close()
        except ImportError:
            logger.debug("playwright kurulu değil, KAP detay sayfası tablo zenginleştirmesi atlanıyor.")
            for _item, disclosure_index in needs_browser:
                _detail_text_cache[disclosure_index] = None

    # Tüm adaylar için (HTTP yoluyla dolmuş VEYA yukarıda None ile
    # işaretlenmiş) sonucu items'a uygula ve logla.
    for item, disclosure_index in candidates:
        enriched = _detail_text_cache.get(disclosure_index)
        if enriched:
            item.raw_text = enriched
            logger.info(
                "KAP: bildirim detay sayfasından zenginleştirme yapıldı (disclosure_index=%s, %d karakter).",
                disclosure_index,
                len(enriched),
            )


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
    # `content`, `disclosure_index`'e (item'in linkinden de geri çıkarılabilen,
    # KARARLI/benzersiz bir anahtar) anahtarlanmış ayrı bir sözlükte tutulur -
    # bkz. _maybe_enrich_with_detail_text çağrısı, sort+truncate'den SONRA
    # yapılıyor, o yüzden liste index'ine güvenmek KIRILGAN olurdu.
    contents_by_disclosure_index: dict[Any, str] = {}
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

        contents_by_disclosure_index[disclosure_index] = content
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

    # Sort+truncate SONRASI (gereksiz kaydı zenginleştirmemek için) - bkz.
    # modül başındaki strateji notu. Bu adım BAŞARISIZ olsa BİLE (ağ hatası,
    # playwright kurulu değil vb.) exception fırlatmaz, items aynen döner.
    _maybe_enrich_with_detail_text(items, contents_by_disclosure_index, user_agent, timeout, min_interval)

    logger.info(
        "%s: %d özel durum açıklaması çekildi (disclosure_classes=%s, pencere=%d dk)",
        name,
        len(items),
        sorted(disclosure_classes),
        lookback_minutes,
    )
    return items
