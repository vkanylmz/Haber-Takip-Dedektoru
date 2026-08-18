"""Belirli aralıklarla (config.yaml -> worker.interval_minutes, varsayılan 30
dk) otomatik tarama yapan zamanlayıcı: haberleri çeker, gruplar, özetler,
önem skorlar, veritabanına kaydeder ve eşiği geçen haberler için Telegram
bildirimi gönderir (gereksinim #5).

Bu dosya, projenin eski `scheduler.py`'sinin yerini alır ve onu genişletir —
artık sadece `src.main.run_once()`'u çağırmakla kalmıyor, run_once() da
kendi içinde DB kaydı + bildirim adımlarını yürütüyor (bkz. src/main.py).

KAP (bkz. src/fetchers/kap_fetcher.py), yukarıdaki genel taramaya EK olarak
çok daha sık (varsayılan 120sn, bkz. config.yaml > kap_fast_poll) AYRI bir
job ile de yoklanır - bkz. _add_kap_fast_poll_job.

Bağımsız (worker'ı TEK BAŞINA, web dashboard olmadan) çalıştırma:
    python worker.py

Web dashboard ile birlikte tek komutla başlatmak için:
    python main.py
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src.backup import run_backup
from src.commodity_report import send_weekly_commodity_report
from src.config import load_config
from src.daily_digest import send_daily_digest
from src.db import add_subscriber, get_app_state, get_pending_calendar_events_count, init_db, set_app_state
from src.deduplicator import group_similar_news
from src.economic_calendar import refresh_economic_calendar
from src.logging_setup import setup_logging
from src.main import fetch_source, run_once, summarize_and_persist_groups
from src.telegram_bot import start_bot_listener_thread, stop_bot_listener_thread
from src.trend_report import send_monthly_trend_report, send_weekly_trend_report
from src.web.market_data import (
    MARKET_SYMBOLS as _MARKET_DATA_SYMBOLS,
    fetch_market_snapshot_from_yahoo,
)

logger = logging.getLogger(__name__)

# Günlük özet raporu ne zaman gönderilsin (bkz. README > Günlük Özet Raporu).
# Hafta içi (Pazartesi-Cuma), İstanbul saatiyle sabah 09:00. Test amaçlı
# geçici olarak değiştirilebilir - kalıcı kullanımda BU DEĞERLERİ değiştirin.
DAILY_DIGEST_DAY_OF_WEEK = "mon-fri"
DAILY_DIGEST_HOUR = 9
DAILY_DIGEST_MINUTE = 0
DAILY_DIGEST_TIMEZONE = "Europe/Istanbul"

# Haftalık trend raporu: her Pazartesi, günlük özetten (09:00) sonra 09:30'da.
WEEKLY_TREND_HOUR = 9
WEEKLY_TREND_MINUTE = 30

# Aylık trend raporu: ayın İLK Pazartesi'si, haftalık rapordan (09:30) sonra
# 09:45'te - aynı anda iki ayrı Telegram mesaj patlaması göndermemek için
# kasıtlı olarak farklı bir dakika seçildi. "day='1-7', day_of_week='mon'"
# kombinasyonu APScheduler'da "ayın 1-7. günleri ARASINDAKİ Pazartesi" anlamına
# gelir - bu da her ayda TAM OLARAK bir kez (ilk Pazartesi) tetiklenmesini
# sağlar.
MONTHLY_TREND_DAY = "1-7"
MONTHLY_TREND_HOUR = 9
MONTHLY_TREND_MINUTE = 45

# Haftalık Emtia Raporu (bkz. src/commodity_report.py > Faz 2): her
# Pazartesi, diğer haftalık/aylık raporlardan (09:30/09:45) sonra 10:00'da -
# aynı gerekçeyle (art arda Telegram mesaj patlaması olmasın) farklı bir
# saat seçildi. Ayrıca bu rapor 9 ayrı LLM çağrısı içerdiğinden (rate-limit
# koruması nedeniyle ~2 dk sürebilir) diğer görevlerle ÇAKIŞMAMASI için de
# yeterli boşluk bırakıldı.
WEEKLY_COMMODITY_HOUR = 10
WEEKLY_COMMODITY_MINUTE = 0

# Haftalık veritabanı yedeği: Pazar 03:00 - diğer haftalık/aylık görevlerden
# (hepsi Pazartesi sabahı) farklı bir gün/düşük trafikli bir saat, çakışma
# olmasın diye.
DB_BACKUP_DAY_OF_WEEK = "sun"
DB_BACKUP_HOUR = 3
DB_BACKUP_MINUTE = 0


def _seed_env_subscriber(default_threshold: int) -> None:
    """.env'deki (geriye dönük uyumluluk amaçlı) TELEGRAM_CHAT_ID'yi
    subscribers tablosuna ilk kayıt olarak ekler (idempotent — chat_id
    UNIQUE olduğundan tekrar tekrar çağrılması güvenlidir). Bundan sonraki
    yeni abonelikler için .env düzenlemesi gerekmez; herkes botla /start
    yazarak kendi kendine abone olabilir (bkz. src/telegram_bot.py)."""
    chat_id_str = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    for chat_id in (cid.strip() for cid in chat_id_str.split(",")):
        if chat_id:
            add_subscriber(chat_id, default_threshold=default_threshold)


def _init_persistence_and_bot(config: dict) -> None:
    """Veritabanını hazırlar, .env'deki sahibin chat_id'sini abone olarak
    tohumlar ve (bot token varsa) Telegram bot dinleyicisini ayrı bir arka
    plan thread'inde başlatır. RSS tarama zamanlayıcısını bloklamaz."""
    db_path = config.get("database", {}).get("path", "data/finans_haber.db")
    try:
        init_db(db_path)
    except Exception:  # noqa: BLE001
        logger.exception("Veritabanı başlatılamadı (%s), abone kaydı ve bot dinleyicisi atlanıyor.", db_path)
        return

    # `init_db()` (yukarıda) motoru sadece HAZIRLAR - bağlantı havuzu tembel
    # (lazy) olduğundan gerçek bağlantı hatası (ör. Neon kota aşımı) ancak
    # İLK sorguda ortaya çıkar. Bu yüzden `init_db()`'nin BAŞARILI olması,
    # aşağıdaki `add_subscriber` çağrısının da başarılı olacağını GARANTİ
    # ETMEZ - kendi try/except'i olmadan bu satır patlarsa (gerçek bir olayda
    # doğrulandı: Neon kota aşımı sırasında `python main.py` hiç açılmadan
    # çöktü) TÜM uygulama (worker+bot+dashboard) hiç başlamadan çöker.
    try:
        _seed_env_subscriber(config.get("importance", {}).get("threshold", 4))
    except Exception:  # noqa: BLE001
        logger.exception("Sahibin chat_id'si abone olarak kaydedilemedi (DB erişilemiyor olabilir) - devam ediliyor.")

    if not config.get("telegram", {}).get("enabled", True):
        logger.info("config.yaml > telegram.enabled=false, Telegram bot dinleyicisi başlatılmadı.")
        return

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    start_bot_listener_thread(bot_token)


def _job() -> None:
    try:
        run_once()
    except Exception:  # noqa: BLE001
        # run_once() ve alt bileşenleri (fetcher'lar, özetleyici, db,
        # notifier) kendi hatalarını zaten izole ediyor; burada sadece en
        # dış katmanı da koruyarak zamanlayıcının bir sonraki çalışmayı
        # atlamasını önlüyoruz.
        logger.exception("Zamanlanmış tarama sırasında beklenmeyen bir hata oluştu.")


def _find_kap_source_cfg(config: dict) -> dict | None:
    """`config.yaml > sources` içinde `type: kap` olan, etkin girdiyi bulur
    (bkz. src/fetchers/kap_fetcher.py). Yoksa/kapalıysa None döner - çağıran
    taraf (_kap_fast_poll_job) bu durumda sessizce hiçbir şey yapmaz."""
    for source_cfg in config.get("sources", []):
        if source_cfg.get("type") == "kap" and source_cfg.get("enabled", True):
            return source_cfg
    return None


def _kap_fast_poll_job() -> None:
    """KAP'ı (bkz. src/fetchers/kap_fetcher.py) genel `_job` taramasından
    (15 dk, bkz. periyodik_tarama) BAĞIMSIZ, çok daha sık (varsayılan 120sn,
    bkz. config.yaml > kap_fast_poll) yoklar - özel durum açıklamaları
    dakikalar içinde piyasayı etkileyebileceğinden genel tarama aralığı
    "anlık" hissi için yetersiz kalır.

    `fetch_source` (src/main.py) üzerinden çağrılır - böylece diğer
    kaynaklarla AYNI hata izolasyonu ve kaynak-sağlığı kaydı (bkz.
    /kaynak-sagligi) otomatik olarak devreye girer. Aynı disclosure'ın hem
    burada hem genel taramada tekrar görülmesi ZARARSIZDIR (bkz.
    src/fetchers/kap_fetcher.py modül docstring'i - group_key/notified
    idempotency'si)."""
    try:
        config = load_config()
        kap_fast_poll_cfg = config.get("kap_fast_poll", {})
        if not kap_fast_poll_cfg.get("enabled", True):
            return

        source_cfg = _find_kap_source_cfg(config)
        if source_cfg is None:
            return

        items = fetch_source(source_cfg, config["app"])
        if not items:
            return

        dedup_cfg = config["app"]
        groups = group_similar_news(
            items,
            similarity_threshold=dedup_cfg.get("dedup_similarity_threshold", 0.55),
            window_hours=dedup_cfg.get("dedup_window_hours", 12),
        )
        summarize_and_persist_groups(groups, config)
    except Exception:  # noqa: BLE001 - hızlı KAP yoklaması diğer görevleri/zamanlayıcıyı etkilemesin
        logger.exception("KAP hızlı yoklaması sırasında beklenmeyen bir hata oluştu.")


def _add_kap_fast_poll_job(scheduler, config: dict) -> None:
    kap_fast_poll_cfg = config.get("kap_fast_poll", {})
    if not kap_fast_poll_cfg.get("enabled", True):
        logger.info("config.yaml > kap_fast_poll.enabled=false, KAP hızlı yoklama job'ı eklenmedi.")
        return
    interval_seconds = kap_fast_poll_cfg.get("interval_seconds", 120)
    scheduler.add_job(_kap_fast_poll_job, "interval", seconds=interval_seconds, id="kap_hizli_yoklama")
    logger.info("KAP hızlı yoklama job'ı eklendi: her %s saniyede bir.", interval_seconds)


# Piyasa şeridi (top ticker) anlık görüntüsü - GERÇEK production'da (Render,
# paylaşılan IP) Yahoo Finance'in bu IP'yi ağır rate-limit'e uğrattığı
# doğrulandı (bkz. sohbet, 2026-08-18); web katmanı artık Yahoo'ya HİÇ
# gitmiyor (bkz. src/web/market_data.py > _refresh_market_data_cache) -
# SADECE burada, yerel/engellenmemiş worker IP'sinden çekilip app_state'e
# yazılıyor. Web katmanının ESKİ arka plan tazeleme aralığıyla (bkz.
# src/web/market_data.py > _BACKGROUND_REFRESH_INTERVAL_SECONDS) AYNI (90sn)
# tutuldu - daha sık çekmenin (kaynağın kendi tik hızına göre, bkz. o
# dosyadaki ölçüm notu) somut bir faydası yok.
_MARKET_SNAPSHOT_INTERVAL_SECONDS = 90


def _market_snapshot_job() -> None:
    """Piyasa şeridi verisini (bkz. MARKET_SYMBOLS) çekip app_state'e yazar.

    KISMİ başarı (ör. 12 semboldan birkaçı) durumunda, ÖNCEKİ app_state
    yazımındaki eski değerlerle birleştirilir - src/web/market_data.py'nin
    ESKİ (Yahoo'ya doğrudan giderken kullandığı) "kısmi başarıyı eskiyle
    birleştir" mantığıyla AYNI gerekçe: bir turun başarısız sembolleri,
    kullanıcının önceden gördüğü diğer sembolleri KAYBETMESİN. TAMAMEN
    başarısız olursa (0 sembol) app_state'e HİÇ dokunulmaz - eski (varsa)
    veri korunur, `fetched_at` de GÜNCELLENMEZ (bkz. dashboard() >
    is_data_stale - bu sayede yaşı, gerçekten ne kadar süredir taze veri
    alınamadığını doğru yansıtır)."""
    try:
        data = asyncio.run(fetch_market_snapshot_from_yahoo())
    except Exception:  # noqa: BLE001 - bir turun başarısız olması zamanlayıcıyı durdurmasın
        logger.exception("Piyasa şeridi verisi çekilirken beklenmeyen bir hata oluştu.")
        return

    if not data:
        logger.warning(
            "Piyasa şeridi tazeleme denemesi TAMAMEN başarısız oldu (0/%d sembol) - "
            "app_state'teki eski veri korunuyor, bir sonraki denemede tekrar denenecek.",
            len(_MARKET_DATA_SYMBOLS),
        )
        return

    if len(data) < len(_MARKET_DATA_SYMBOLS):
        try:
            previous = get_app_state("market_snapshot") or {}
        except Exception:  # noqa: BLE001
            previous = {}
        previous_by_symbol = {item["symbol"]: item for item in (previous.get("data") or [])}
        new_by_symbol = {item["symbol"]: item for item in data}
        data = [
            new_by_symbol.get(symbol) or previous_by_symbol.get(symbol)
            for symbol, _label in _MARKET_DATA_SYMBOLS
        ]
        data = [item for item in data if item is not None]
        logger.warning(
            "Piyasa şeridi tazeleme denemesi KISMİ başarılı (%d/%d sembol) - "
            "eksik semboller için önceki app_state değerleri korundu.",
            len(new_by_symbol), len(_MARKET_DATA_SYMBOLS),
        )

    try:
        set_app_state(
            "market_snapshot",
            {"data": data, "fetched_at": datetime.now(timezone.utc).isoformat()},
        )
    except Exception:  # noqa: BLE001 - DB geçici erişilemez olabilir, bir sonraki turda tekrar denenecek
        logger.exception("Piyasa şeridi verisi app_state'e yazılamadı.")


def _add_market_snapshot_job(scheduler) -> None:
    scheduler.add_job(
        _market_snapshot_job, "interval", seconds=_MARKET_SNAPSHOT_INTERVAL_SECONDS, id="piyasa_seridi_yenileme"
    )
    scheduler.add_job(_market_snapshot_job, id="piyasa_seridi_ilk_yenileme")  # hemen (arka planda) çalışır


# --- Ekonomik Takvim (2026-08-18, kullanıcı isteği) ---
# İKİ AYRI görev, TEK ortak fonksiyonu (refresh_economic_calendar) çağırır:
#   1) Genel tarama: 5 saatte bir (kullanıcının istediği "4-6 saatte bir"
#      aralığının ortası) - Türkiye TÜMÜ + diğer ülkeler 2-3 yıldız, 7 günlük
#      pencere (bkz. src/economic_calendar.py).
#   2) "Hassas izleme": 30sn'lik BASİT interval-polling (kullanıcı kararı,
#      2026-08-18 - başlangıçta önerilen "her olay için ayrı APScheduler
#      job'ı" tasarımı YERİNE seçildi: watchdog sık restart olduğundan
#      bellek-içi tek-seferlik job'ların kaybolma riski gerçekti, bu basit
#      interval job restart'a doğal olarak dayanıklı, JOB KAYBI YOK). SADECE
#      açıklanma saati GEÇMİŞ AMA actual_value HÂLÂ boş olan bir olay varsa
#      (bkz. get_pending_calendar_events_count) gerçek bir istek atar -
#      nezaket kuralı, gereksiz taramayı önler.
_ECONOMIC_CALENDAR_REFRESH_HOURS = 5
_ECONOMIC_CALENDAR_WATCH_INTERVAL_SECONDS = 30
_ECONOMIC_CALENDAR_WATCH_WINDOW_MINUTES = 3


def _economic_calendar_refresh_job() -> None:
    try:
        refresh_economic_calendar()
    except Exception:  # noqa: BLE001 - bir tur başarısız olursa zamanlayıcıyı durdurmasın
        logger.exception("Ekonomik takvim genel taraması sırasında beklenmeyen bir hata oluştu.")


def _economic_calendar_watch_job() -> None:
    try:
        pending = get_pending_calendar_events_count(_ECONOMIC_CALENDAR_WATCH_WINDOW_MINUTES)
        if pending == 0:
            return
        logger.info(
            "Ekonomik takvim: açıklanma saati geçmiş %d bekleyen olay var, hassas izleme taraması tetikleniyor.",
            pending,
        )
        refresh_economic_calendar()
    except Exception:  # noqa: BLE001 - bir tur başarısız olursa zamanlayıcıyı durdurmasın
        logger.exception("Ekonomik takvim hassas izleme taraması sırasında beklenmeyen bir hata oluştu.")


def _add_economic_calendar_jobs(scheduler) -> None:
    scheduler.add_job(
        _economic_calendar_refresh_job,
        "interval",
        hours=_ECONOMIC_CALENDAR_REFRESH_HOURS,
        id="ekonomik_takvim_genel_tarama",
    )
    scheduler.add_job(_economic_calendar_refresh_job, id="ekonomik_takvim_ilk_tarama")  # hemen (arka planda) çalışır
    scheduler.add_job(
        _economic_calendar_watch_job,
        "interval",
        seconds=_ECONOMIC_CALENDAR_WATCH_INTERVAL_SECONDS,
        id="ekonomik_takvim_hassas_izleme",
    )


def _daily_digest_job() -> None:
    """Günlük özet raporu (bkz. src/daily_digest.py) - mevcut anlık bildirim
    akışından (yukarıdaki `_job`) TAMAMEN bağımsız, ayrı bir zamanlanmış
    görev. Buradaki hata da periyodik taramayı/diğer görevleri etkilemesin
    diye izole ediliyor."""
    try:
        config = load_config()
        send_daily_digest(config)
    except Exception:  # noqa: BLE001
        logger.exception("Günlük özet raporu gönderilirken beklenmeyen bir hata oluştu.")


def _weekly_trend_job() -> None:
    """Haftalık trend raporu (bkz. src/trend_report.py) - günlük özetten VE
    anlık bildirim akışından TAMAMEN bağımsız, ayrı bir zamanlanmış görev."""
    try:
        config = load_config()
        send_weekly_trend_report(config)
    except Exception:  # noqa: BLE001
        logger.exception("Haftalık trend raporu gönderilirken beklenmeyen bir hata oluştu.")


def _monthly_trend_job() -> None:
    """Aylık trend raporu (bkz. src/trend_report.py) - haftalık rapordan da
    bağımsız çalışır, kendi hatası diğer görevleri etkilemesin diye izole
    edilir."""
    try:
        config = load_config()
        send_monthly_trend_report(config)
    except Exception:  # noqa: BLE001
        logger.exception("Aylık trend raporu gönderilirken beklenmeyen bir hata oluştu.")


def _weekly_commodity_report_job() -> None:
    """Haftalık Emtia Raporu (bkz. src/commodity_report.py > Faz 2) -
    haftalık/aylık trend raporlarından ve diğer tüm zamanlanmış görevlerden
    TAMAMEN bağımsız, ayrı bir zamanlanmış görev. Kendi hatası diğer
    görevleri etkilemesin diye izole edilir (send_weekly_commodity_report
    zaten kendi içinde de exception yutuyor - bu try/except ekstra bir
    güvenlik katmanı, worker.py'deki DİĞER TÜM job fonksiyonlarıyla
    tutarlılık için)."""
    try:
        config = load_config()
        send_weekly_commodity_report(config)
    except Exception:  # noqa: BLE001
        logger.exception("Haftalık Emtia Raporu gönderilirken beklenmeyen bir hata oluştu.")


def _db_backup_job() -> None:
    """Haftalık veritabanı yedeği (bkz. src/backup.py) - diğer tüm zamanlanmış
    görevlerden bağımsız, kendi hatası diğerlerini etkilemesin diye izole
    edilir."""
    try:
        config = load_config()
        run_backup(config)
    except Exception:  # noqa: BLE001
        logger.exception("Veritabanı yedeği alınırken beklenmeyen bir hata oluştu.")


def _add_daily_digest_job(scheduler) -> None:
    scheduler.add_job(
        _daily_digest_job,
        CronTrigger(
            day_of_week=DAILY_DIGEST_DAY_OF_WEEK,
            hour=DAILY_DIGEST_HOUR,
            minute=DAILY_DIGEST_MINUTE,
            timezone=DAILY_DIGEST_TIMEZONE,
        ),
        id="gunluk_ozet",
    )


def _add_trend_report_jobs(scheduler) -> None:
    scheduler.add_job(
        _weekly_trend_job,
        CronTrigger(
            day_of_week="mon",
            hour=WEEKLY_TREND_HOUR,
            minute=WEEKLY_TREND_MINUTE,
            timezone=DAILY_DIGEST_TIMEZONE,
        ),
        id="haftalik_trend",
    )
    scheduler.add_job(
        _monthly_trend_job,
        CronTrigger(
            day=MONTHLY_TREND_DAY,
            day_of_week="mon",
            hour=MONTHLY_TREND_HOUR,
            minute=MONTHLY_TREND_MINUTE,
            timezone=DAILY_DIGEST_TIMEZONE,
        ),
        id="aylik_trend",
    )
    scheduler.add_job(
        _weekly_commodity_report_job,
        CronTrigger(
            day_of_week="mon",
            hour=WEEKLY_COMMODITY_HOUR,
            minute=WEEKLY_COMMODITY_MINUTE,
            timezone=DAILY_DIGEST_TIMEZONE,
        ),
        id="haftalik_emtia_raporu",
    )


def _add_db_backup_job(scheduler) -> None:
    scheduler.add_job(
        _db_backup_job,
        CronTrigger(
            day_of_week=DB_BACKUP_DAY_OF_WEEK,
            hour=DB_BACKUP_HOUR,
            minute=DB_BACKUP_MINUTE,
            timezone=DAILY_DIGEST_TIMEZONE,
        ),
        id="db_yedekleme",
    )


def start_background_scheduler(config: dict | None = None) -> BackgroundScheduler:
    """Web sunucusuyla BİRLİKTE (main.py) kullanmak üzere, arka planda
    (bloklamadan) çalışan bir zamanlayıcı başlatır ve döndürür.

    İlk tarama hemen (arka plan thread'inde, çağıranı bloklamadan) tetiklenir,
    ardından periyodik tarama devam eder.
    """
    config = config or load_config()
    setup_logging(config["app"].get("output_dir", "data"))
    _init_persistence_and_bot(config)
    interval_minutes = config.get("worker", {}).get("interval_minutes", 30)

    scheduler = BackgroundScheduler()
    scheduler.add_job(_job, "interval", minutes=interval_minutes, id="periyodik_tarama")
    scheduler.add_job(_job, id="ilk_tarama")  # tetikleyici verilmezse hemen (arka planda) çalışır
    _add_kap_fast_poll_job(scheduler, config)
    _add_market_snapshot_job(scheduler)
    _add_economic_calendar_jobs(scheduler)
    _add_daily_digest_job(scheduler)
    _add_trend_report_jobs(scheduler)
    _add_db_backup_job(scheduler)
    scheduler.start()

    logger.info(
        "Worker (arka plan) başlatıldı: her %s dakikada bir taranacak; günlük özet %s %02d:%02d (%s).",
        interval_minutes,
        DAILY_DIGEST_DAY_OF_WEEK,
        DAILY_DIGEST_HOUR,
        DAILY_DIGEST_MINUTE,
        DAILY_DIGEST_TIMEZONE,
    )
    return scheduler


def main() -> None:
    """Bağımsız çalıştırma: worker'ı TEK BAŞINA, bloklayan bir zamanlayıcıyla
    başlatır. Ctrl+C ile durdurulabilir."""
    config = load_config()
    setup_logging(config["app"].get("output_dir", "data"))
    _init_persistence_and_bot(config)
    interval_minutes = config.get("worker", {}).get("interval_minutes", 30)

    scheduler = BlockingScheduler()
    scheduler.add_job(_job, "interval", minutes=interval_minutes, id="periyodik_tarama")
    _add_kap_fast_poll_job(scheduler, config)
    _add_market_snapshot_job(scheduler)
    _add_economic_calendar_jobs(scheduler)
    _add_daily_digest_job(scheduler)
    _add_trend_report_jobs(scheduler)
    _add_db_backup_job(scheduler)

    logger.info(
        "Worker başlatıldı: her %s dakikada bir çalışacak. Çıkmak için Ctrl+C.",
        interval_minutes,
    )

    _job()  # ilk taramayı hemen yap

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Worker durduruldu.")
    finally:
        stop_bot_listener_thread()


if __name__ == "__main__":
    main()
