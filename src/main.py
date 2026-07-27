"""Uygulamanın ana giriş noktası: tüm kaynakları çeker, gruplar, özetler,
önem skorlar, veritabanına kaydeder, önemli haberler için Telegram bildirimi
gönderir ve terminale + Markdown dosyasına yazar.

Çalıştırma (proje kök dizininden):
    python -m src.main

Periyodik (zamanlanmış) çalıştırma ve web dashboard için bkz. worker.py ve
main.py (proje kökünde).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from src.config import get_summarizer_api_key, load_config
from src.db import (
    compute_group_key,
    find_record_by_group_key,
    get_session,
    init_db,
    mark_notified,
    record_source_health,
    upsert_group,
)
from src.deduplicator import group_similar_news, sort_groups_by_recency
from src.fetchers.licensed_aggregator import fetch_licensed_aggregator
from src.fetchers.rss_fetcher import fetch_rss
from src.fetchers.scrape_fetcher import fetch_scrape
from src.keyword_alerts import check_keyword_matches_and_notify
from src.logging_setup import setup_logging
from src.models import NewsGroup, NewsItem
from src.notifier import send_telegram_notification
from src.output.cli_output import print_report
from src.output.markdown_output import write_markdown
from src.summarizer import Summarizer

logger = logging.getLogger(__name__)


def fetch_source(source_cfg: dict[str, Any], app_cfg: dict[str, Any]) -> list[NewsItem]:
    """Kaynağın türüne göre uygun fetcher'a yönlendirir.

    Bilinmeyen kaynak türleri veya beklenmedik hatalar burada yakalanır ve
    boş liste döner; böylece tek bir kaynaktaki sorun tüm çalıştırmayı
    etkilemez (gereksinim #7).

    Her çalıştırma denemesi (başarılı/başarısız, kaç haber, ne zaman) ayrıca
    `src/db.py > record_source_health`'e kaydedilir - kaynak sağlık paneli
    bunu kullanır (bkz. src/web/app.py > /kaynak-sagligi). Bu kayıt asla
    exception fırlatmaz, dolayısıyla asıl çekim akışını etkilemez.

    ÖNEMLİ: alttaki fetcher'lar (rss_fetcher/scrape_fetcher/licensed_aggregator)
    KENDİ hatalarını (ağ hatası, robots.txt engeli, parse hatası) zaten
    yakalayıp loglayıp boş liste döndürüyor - yani BURADAKİ try/except'e asıl
    pratikte neredeyse hiç düşülmez. Bu yüzden "başarı", exception fırlatılıp
    fırlatılmadığından DEĞİL, gerçekten haber dönüp dönmediğinden
    (len(items) > 0) belirlenir - aksi halde sağlık paneli her zaman "%100
    başarı" gösterirdi, altta bir ağ/robots.txt sorunu olsa bile.
    """
    source_type = source_cfg.get("type", "rss")
    name = source_cfg.get("name", source_cfg.get("id", "bilinmeyen-kaynak"))
    try:
        if source_type == "rss":
            items = fetch_rss(source_cfg, app_cfg)
        elif source_type == "scrape":
            items = fetch_scrape(source_cfg, app_cfg)
        elif source_type == "licensed_aggregator":
            items = fetch_licensed_aggregator(source_cfg, app_cfg)
        else:
            logger.error("%s: bilinmeyen kaynak türü '%s'", name, source_type)
            record_source_health(name, success=False, error_message=f"Bilinmeyen kaynak türü: {source_type}")
            return []
        if items:
            record_source_health(name, success=True, item_count=len(items))
        else:
            record_source_health(
                name,
                success=False,
                error_message="Bu çalıştırmada hiç haber alınamadı (ağ hatası, robots.txt engeli, boş feed veya parse hatası olabilir - bkz. loglar).",
            )
        return items
    except Exception as exc:  # noqa: BLE001 - beklenmeyen her hatayı izole et
        logger.exception("%s kaynağı çekilirken beklenmeyen bir hata oluştu", name)
        record_source_health(name, success=False, error_message=str(exc))
        return []


def fetch_all_sources(config: dict[str, Any]) -> list[NewsItem]:
    app_cfg = config["app"]
    sources = [s for s in config.get("sources", []) if s.get("enabled", True)]

    all_items: list[NewsItem] = []
    with ThreadPoolExecutor(max_workers=min(5, max(1, len(sources)))) as executor:
        future_to_source = {
            executor.submit(fetch_source, source_cfg, app_cfg): source_cfg
            for source_cfg in sources
        }
        for future in as_completed(future_to_source):
            source_cfg = future_to_source[future]
            name = source_cfg.get("name", source_cfg.get("id"))
            try:
                items = future.result()
                all_items.extend(items)
            except Exception:  # noqa: BLE001
                logger.exception("%s kaynağından sonuç alınırken hata oluştu", name)

    return all_items


def summarize_groups(groups: list[NewsGroup], group_keys: dict[int, str], config: dict[str, Any], notify: bool = True) -> None:
    """Her grubu seçili LLM sağlayıcısıyla (varsayılan Gemini, bkz.
    config.yaml > summarizer.llm_provider) özetler+skorlar. İlgili API anahtarı
    tanımlı değilse tüm çalıştırmayı durdurmak yerine ham başlık/metni özet
    olarak kullanır ve bir uyarı loglar — böylece .env henüz ayarlanmamışken
    bile kullanıcı çekilen haberleri terminalde/Markdown'da görebilir. Bu
    durumda önem skoru None kalır, yani bu haberler asla Telegram eşiğini
    geçmez.

    Yeni haberler özetlendiği ANDA veritabanına kaydedilir ve bildirimi gönderilir.

    NOT: Her grup için `_persist_and_notify_single` KENDİ kısa ömürlü
    session'ını açar (aşağıda tek bir paylaşılan session kullanılmıyor).
    Bunun nedeni: `summarizer.summarize_group()` çağrısı, rate-limit
    koruması yüzünden istekler arasında ~12.5 sn (veya daha fazla, 429
    durumunda) bekliyor - eğer TÜM döngü tek bir açık SQLite yazma
    transaction'ı içinde olsaydı, bu transaction onlarca saniye kilitli
    kalır ve AYNI ANDA çalışan Telegram bot dinleyicisinin (kullanıcı
    komutlarını işlerken yaptığı veritabanı yazmaları) "database is locked"
    hatası almasına yol açardı - gerçek production'da defalarca gözlemlendi
    (bkz. data/logs/finans_haber.log). Her grubun kendi kısa session'ı
    olması, yazma kilidinin sadece o grubun DB işlemi kadar (milisaniyeler)
    sürmesini sağlıyor.
    """
    summarizer_cfg = config["summarizer"]
    try:
        provider, api_key = get_summarizer_api_key(summarizer_cfg)
    except RuntimeError as exc:
        logger.warning("%s Özetler ham metinle dolduruluyor.", exc)
        for group in groups:
            rep = group.representative
            group.summary = f"(Özetlenmedi - API anahtarı yok) {rep.raw_text[:280] or rep.title}"
        return

    summarizer = Summarizer(summarizer_cfg, api_key=api_key, provider=provider)
    for group in groups:
        try:
            summarizer.summarize_group(group)
            # Özetlenen haberi beklemeden hemen kaydet ve bildir
            group_key = group_keys.get(id(group)) or compute_group_key(group.representative.title)
            _persist_and_notify_single(group, group_key, config, notify)
        except Exception:  # noqa: BLE001
            logger.exception("Özetleme sırasında beklenmeyen hata: %s", group.representative.title)


def _reuse_or_mark_for_summarization(
    groups: list[NewsGroup],
) -> tuple[list[NewsGroup], dict[int, str]]:
    """Her grup için kararlı bir group_key hesaplar. Veritabanında bu
    group_key için zaten (Claude tarafından üretilmiş) bir özet varsa, tekrar
    API çağrısı yapmadan o özeti/skoru gruba kopyalar (maliyet optimizasyonu
    — aynı haber bir sonraki 30 dakikalık taramada hâlâ listedeyse tekrar
    özetlenmez). Yoksa grup, özetlenmesi gereken listeye eklenir.

    Döndürür: (özetlenmesi_gereken_gruplar, {id(group): group_key, ...})
    """
    to_summarize: list[NewsGroup] = []
    group_keys: dict[int, str] = {}

    with get_session() as session:
        for group in groups:
            group_key = compute_group_key(group.representative.title)
            group_keys[id(group)] = group_key

            # Yalnızca gerçekten Claude tarafından skorlanmış (importance_score
            # dolu) kayıtları yeniden kullanıyoruz. API anahtarı eksikken veya
            # JSON ayrıştırma başarısız olduğunda importance_score None kalır
            # (bkz. summarizer.py) — bu durumda yer tutucu metni sonsuza kadar
            # önbelleğe almak yerine, her çalıştırmada tekrar denenir (API
            # anahtarı sonradan eklenirse bir sonraki taramada düzgün
            # özetlenebilsin diye).
            existing = find_record_by_group_key(session, group_key)
            if existing is not None and existing.summary and existing.importance_score is not None:
                group.summary = existing.summary
                group.key_points = existing.key_points_list()
                group.importance_score = existing.importance_score
                group.importance_reason = existing.importance_reason or ""
                group.regions = existing.regions_list()
                group.sectors = existing.sectors_list()
                group.sentiment = existing.sentiment
            else:
                to_summarize.append(group)

    return to_summarize, group_keys


def _persist_and_notify_single(group: NewsGroup, group_key: str, config: dict[str, Any], notify: bool) -> None:
    """Bir grubu kaydeder ve gerekirse bildirir.

    KASITLI OLARAK dışarıdan bir `session` almaz: veritabanı yazma adımı
    (`upsert_group`) KENDİ kısa ömürlü session'ında açılıp hemen commit
    edilir, ondan SONRA (session kapandıktan sonra) yavaş olabilecek ağ
    çağrıları (Telegram gönderimi, anahtar kelime kontrolü - ki o da kendi
    session'larını açar) yapılır. Böylece hiçbir DB yazma kilidi, bir ağ
    çağrısı süresince açık kalmaz (bkz. summarize_groups'taki NOT)."""
    telegram_enabled = config.get("telegram", {}).get("enabled", True)

    with get_session() as session:
        record = upsert_group(session, group, group_key)

    if not notify or not telegram_enabled:
        return

    # Anahtar kelime takibi (src/keyword_alerts.py), önem skoru eşiğinden
    # TAMAMEN BAĞIMSIZ çalışır - eşiği geçmese bile takip edilen kelime
    # geçiyorsa ilgili kullanıcıya bildirim gider (gereksinim).
    try:
        check_keyword_matches_and_notify(record)
    except Exception:  # noqa: BLE001 - anahtar kelime bildirimi asla ana akışı durdurmasın
        logger.exception("Anahtar kelime eşleşme kontrolü sırasında beklenmeyen hata: %s", record.title)

    # Artık tek bir global eşik yok: her abonenin KENDİ eşiği (bkz. Telegram
    # /esik komutu, src/db.py > Subscriber.importance_threshold) esas alınır -
    # kimin gönderim alacağına send_telegram_notification içinde karar verilir.
    if record.importance_score is None:
        return
    if record.notified:
        return

    sent = send_telegram_notification(record)
    if sent:
        mark_notified(group_key)


def persist_and_notify(
    groups: list[NewsGroup],
    group_keys: dict[int, str],
    config: dict[str, Any],
    notify: bool = True,
) -> None:
    """Tüm grupları veritabanına kalıcı hale getirir; önem skoru eşiği
    geçen ve daha önce bildirilmemiş haberler için Telegram bildirimi
    gönderir (gereksinim: sadece eşiği geçenler, aynı haber için tekrar yok).

    Her grup `_persist_and_notify_single` içinde kendi kısa ömürlü
    session'ını kullanır (bkz. summarize_groups'taki NOT) - burada tüm
    döngüyü saran paylaşımlı bir session YOK, aksi halde ~90 grubu (bazıları
    Telegram gönderimi gerektiren) işlerken yazma kilidi uzun süre açık
    kalırdı.
    """
    for group in groups:
        group_key = group_keys.get(id(group)) or compute_group_key(group.representative.title)
        _persist_and_notify_single(group, group_key, config, notify)


def run_once(
    config_path: str | None = None,
    summarize: bool = True,
    notify: bool = True,
) -> list[NewsGroup]:
    config = load_config(config_path)
    setup_logging(config["app"].get("output_dir", "data"))

    db_path = config.get("database", {}).get("path", "data/finans_haber.db")
    try:
        init_db(db_path)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Veritabanı başlatılamadı (%s) - kalıcılık ve Telegram bildirimi bu "
            "çalıştırmada devre dışı, ancak çekim/özetleme/çıktı devam ediyor.",
            db_path,
        )

    logger.info("Haber toplama çalıştırması başladı.")
    all_items = fetch_all_sources(config)
    logger.info("Toplam %d haber çekildi (tüm kaynaklardan).", len(all_items))

    dedup_cfg = config["app"]
    groups = group_similar_news(
        all_items,
        similarity_threshold=dedup_cfg.get("dedup_similarity_threshold", 0.55),
        window_hours=dedup_cfg.get("dedup_window_hours", 12),
    )
    groups = sort_groups_by_recency(groups)
    logger.info("Haberler %d konuya gruplandı.", len(groups))

    group_keys: dict[int, str] = {}
    if groups:
        try:
            groups_to_summarize, group_keys = _reuse_or_mark_for_summarization(groups)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Veritabanından önceki özetler okunamadı, tüm gruplar yeniden özetlenecek."
            )
            groups_to_summarize, group_keys = groups, {}

        if summarize and groups_to_summarize:
            logger.info(
                "%d/%d grup daha önce özetlenmemiş, Claude ile özetleniyor "
                "(%d grup önbellekten/veritabanından yeniden kullanıldı).",
                len(groups_to_summarize),
                len(groups),
                len(groups) - len(groups_to_summarize),
            )
            summarize_groups(groups_to_summarize, group_keys, config, notify=notify)

        try:
            persist_and_notify(groups, group_keys, config, notify=notify)
        except Exception:  # noqa: BLE001
            logger.exception("Veritabanına kaydetme/bildirim adımında beklenmeyen hata oluştu.")

    print_report(groups)
    report_path = write_markdown(groups, config["app"].get("output_dir", "data"))
    logger.info("Markdown raporu yazıldı: %s", report_path)

    return groups


if __name__ == "__main__":
    run_once()
