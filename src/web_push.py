"""Web Push bildirimleri (tarayıcı sekmesi/uygulaması KAPALIYKEN bile
çalışan bildirimler) - Telegram bildirim sistemine (bkz. src/notifier.py)
PARALEL, TAMAMEN AYRI bir kanal. Telegram koduna hiç dokunulmaz.

Mimari özet:
  - VAPID anahtar çifti (bkz. _get_or_create_vapid_keys) uygulama İLK
    çalıştığında OTOMATİK üretilir ve `AppState` tablosunda (aynı paylaşımlı
    Postgres/SQLite - bkz. src/db.py) saklanır. Bu KASITLI bir tercih: Render
    gibi bir ortamda dosya sistemi deploy'lar arası kalıcı DEĞİLDİR ve
    kullanıcıdan manuel bir ortam değişkeni girmesini istemek yerine (ör.
    DATABASE_URL - o KAÇINILMAZ çünkü DB'ye ulaşmak için gereken kimlik
    bilgisinin kendisi), zaten paylaşılan DB'de KENDİ KENDİNİ KURAN bir
    mekanizma tercih edildi - hem yerel worker hem Render dashboard'u AYNI
    anahtarları otomatik olarak görür.
  - `pywebpush` kütüphanesi (RFC 8291/8292 uyumlu) gerçek push gönderimini
    yapar - VAPID imzalama + mesaj şifreleme (aes128gcm) dahil.
  - Bir abonelik artık geçersizse (kullanıcı bildirim iznini geri çektiyse
    veya tarayıcıyı kaldırdıysa) push servisi 404/410 döner - bu durumda
    abonelik veritabanından OTOMATİK silinir (bkz. send_push_notification).
"""

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING, Any

from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from src.db import (
    get_all_push_subscriptions,
    get_app_state,
    mark_push_notified,
    remove_push_subscription,
    set_app_state,
    was_push_notified,
)

if TYPE_CHECKING:
    from src.db import NewsRecord

logger = logging.getLogger(__name__)

_APP_STATE_KEY = "vapid_keys"

# VAPID JWT'sindeki zorunlu "sub" claim - push servisine (FCM/Mozilla
# autopush vb.) bu bildirimlerin kimden geldiğine dair bir iletişim kanalı
# sağlar (RFC 8292). Gerçek bir e-posta kutusu İZLENMEZ, spec'in istediği
# formatı sağlamak için var - bu tamamen kişisel/tek kullanıcılı bir proje
# olduğundan ayrıca yapılandırılabilir hale getirmeye gerek görülmedi.
_VAPID_SUB_CLAIM = "mailto:finans-haber-toplayici@example.com"

# Modül düzeyinde önbellek: VAPID anahtarları süreç boyunca DEĞİŞMEZ, her
# push gönderiminde/her `/api/push/vapid-public-key` isteğinde tekrar tekrar
# DB'ye gitmeye gerek yok.
_vapid_keys_cache: dict[str, str] | None = None


def _generate_vapid_keys() -> dict[str, str]:
    """Yeni bir ECDSA P-256 VAPID anahtar çifti üretir, ikisini de base64url
    (padding'siz) string olarak döner - `private_key` pywebpush'un
    `Vapid.from_string` ile doğrudan kabul ettiği "raw" format, `public_key`
    ise tarayıcının `PushManager.subscribe({applicationServerKey: ...})`
    çağrısının beklediği 65 baytlık (0x04 + X + Y) sıkıştırılmamış nokta
    formatıdır."""
    vapid = Vapid()
    vapid.generate_keys()

    private_numbers = vapid.private_key.private_numbers()
    private_raw = private_numbers.private_value.to_bytes(32, "big")
    private_b64 = base64.urlsafe_b64encode(private_raw).rstrip(b"=").decode("ascii")

    public_numbers = vapid.private_key.public_key().public_numbers()
    public_raw = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
    public_b64 = base64.urlsafe_b64encode(public_raw).rstrip(b"=").decode("ascii")

    return {"public_key": public_b64, "private_key": private_b64}


def _get_or_create_vapid_keys() -> dict[str, str]:
    """VAPID anahtarlarını döner - önce süreç-içi önbellekten, sonra
    `AppState`'ten (bkz. modül docstring'i), hiçbiri yoksa YENİ üretip HEM
    önbelleğe HEM `AppState`'e yazar. İdempotent: birden fazla süreç (ör.
    yerel worker + Render) aynı anda ilk kez çalışsa bile, `AppState`'e
    ikinci yazan basitçe ilkinin üzerine yazar - anahtarlar HER İKİ tarafta
    da yine tutarlı kalır çünkü her ikisi de EN SON yazılan değeri okur
    (bu senaryo pratikte nadirdir - yerel worker genelde Render'dan önce
    ilk kez çalıştırılır)."""
    global _vapid_keys_cache
    if _vapid_keys_cache is not None:
        return _vapid_keys_cache

    cached = get_app_state(_APP_STATE_KEY)
    if cached and cached.get("public_key") and cached.get("private_key"):
        _vapid_keys_cache = cached
        return cached

    keys = _generate_vapid_keys()
    set_app_state(_APP_STATE_KEY, keys)
    logger.info("Yeni VAPID anahtar çifti üretildi ve AppState'e kaydedildi (Web Push bildirimleri için).")
    _vapid_keys_cache = keys
    return keys


def get_vapid_public_key() -> str:
    """Frontend'in `PushManager.subscribe({applicationServerKey: ...})`
    çağrısı için gereken, base64url-encoded VAPID genel anahtarını döner
    (bkz. /api/push/vapid-public-key, src/web/app.py)."""
    return _get_or_create_vapid_keys()["public_key"]


def send_push_notification(subscription_info: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Tek bir tarayıcı aboneliğine push bildirimi gönderir.

    `subscription_info`: {"endpoint": "...", "keys": {"p256dh": "...", "auth": "..."}}
    `payload`: JSON'a çevrilip şifrelenecek veri - Service Worker'ın (bkz.
    sw.js > 'push' event listener) `event.data.json()` ile okuyacağı şekil.

    Döner: True (başarıyla gönderildi), False (gönderim başarısız - ör.
    geçici ağ hatası; abonelik SİLİNMEZ, bir dahaki denemede tekrar
    denenir). Abonelik KALICI OLARAK geçersizse (404/410 - kullanıcı
    izni geri çekti/tarayıcı kaldırıldı) veritabanından OTOMATİK silinir
    ve False döner - exception hiçbir zaman çağıran tarafa SIZMAZ."""
    private_key = _get_or_create_vapid_keys()["private_key"]
    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=private_key,
            vapid_claims={"sub": _VAPID_SUB_CLAIM},
        )
        return True
    except WebPushException as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in (404, 410):
            logger.info(
                "Web Push aboneliği artık geçerli değil (HTTP %s) - siliniyor: ...%s",
                status_code,
                subscription_info.get("endpoint", "")[-24:],
            )
            remove_push_subscription(subscription_info.get("endpoint", ""))
        else:
            logger.warning(
                "Web Push gönderimi başarısız (HTTP %s): %s", status_code, exc, exc_info=True
            )
        return False
    except Exception:  # noqa: BLE001 - tek bir aboneliğe gönderim hatası diğerlerini/ana akışı etkilemesin
        logger.exception("Web Push gönderimi sırasında beklenmeyen hata.")
        return False


def _record_matches_filters(record: "NewsRecord", filters: dict[str, Any]) -> bool:
    """Bir haberin, bir aboneliğin filtresiyle eşleşip eşleşmediğini
    kontrol eder. Her boyut BAĞIMSIZ bir AND koşuludur (ör. hem kaynak HEM
    önem skoru filtresi VARSA, haber İKİSİNİ DE karşılamalı); bir boyutun
    KENDİ İÇİNDE birden fazla değer varsa bunlar OR ile eşleşir (ör.
    sources=["A","B"] -> haberin kaynağı A OLABİLİR ya da B). Boş/None bir
    filtre alanı o boyutta HİÇBİR KISITLAMA olmadığı anlamına gelir."""
    sources = filters.get("sources") or []
    if sources:
        record_sources = [s.strip() for s in (record.sources or "").split(",")]
        if not any(s in record_sources for s in sources):
            return False

    sectors = filters.get("sectors") or []
    if sectors:
        record_sectors = record.sectors_list()
        if not any(s in record_sectors for s in sectors):
            return False

    regions = filters.get("regions") or []
    if regions:
        record_regions = record.regions_list()
        if not any(r in record_regions for r in regions):
            return False

    sentiments = filters.get("sentiments") or []
    if sentiments and record.sentiment not in sentiments:
        return False

    min_importance = filters.get("min_importance")
    if min_importance and (record.importance_score is None or record.importance_score < min_importance):
        return False

    return True


def notify_matching_push_subscriptions(record: "NewsRecord") -> None:
    """Yeni özetlenmiş/güncellenmiş bir haberi TÜM kayıtlı tarayıcı push
    aboneliklerinin filtreleriyle karşılaştırır; eşleşen ve daha önce BU
    ABONELİĞE bildirilmemiş her biri için push bildirimi gönderir.

    `src/keyword_alerts.py > check_keyword_matches_and_notify` İLE AYNI
    mimari desen (Telegram önem skoru eşiğinden TAMAMEN BAĞIMSIZ, ayrı bir
    dedup tablosu ile tekrar bildirimi önler) - buradaki tek fark,
    hedefin bir Telegram chat_id değil bir tarayıcı push endpoint'i olması.

    Filtresiz bir abonelik (filters=None/boş - bkz. Faz 1 ham abonelik)
    HER haberde eşleşir (tüm boyutlar boş -> _record_matches_filters
    otomatik olarak True döner).

    Hiçbir durumda exception fırlatmaz - bir aboneliğe gönderim hatası
    diğerlerini/ana özetleme akışını etkilemez."""
    subscriptions = get_all_push_subscriptions()
    if not subscriptions:
        return

    for sub in subscriptions:
        try:
            filters: dict[str, Any] = json.loads(sub.filters) if sub.filters else {}
        except ValueError:
            filters = {}

        try:
            if not _record_matches_filters(record, filters):
                continue
        except Exception:  # noqa: BLE001 - bozuk bir filtre bu aboneliği atlasın, diğerlerini etkilemesin
            logger.exception("Push abonelik filtresi değerlendirilirken hata: endpoint=...%s", sub.endpoint[-24:])
            continue

        if was_push_notified(sub.endpoint, record.group_key):
            continue

        subscription_info = {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}
        payload = {
            "title": record.title,
            "body": (record.summary or "")[:150],
            "url": "/",
            # Aynı habere ait ardışık bir push gelirse (pratikte olmaz, ama
            # güvenlik payı) tarayıcıda ÜST ÜSTE YIĞILMASIN diye group_key
            # `tag` olarak kullanılır (bkz. sw.js > 'push' event listener).
            "tag": record.group_key,
        }
        sent = send_push_notification(subscription_info, payload)
        if not sent:
            continue

        logger.info(
            "Web Push bildirimi gönderildi: endpoint=...%s, haber=%s",
            sub.endpoint[-24:],
            record.title,
        )
        try:
            mark_push_notified(sub.endpoint, record.group_key)
        except Exception:  # noqa: BLE001 - mesaj zaten gitti, işaretleme başarısız olsa da akış devam etsin
            logger.exception(
                "Push bildirimi 'gönderildi' olarak işaretlenemedi (endpoint=...%s, haber=%s).",
                sub.endpoint[-24:],
                record.title,
            )
