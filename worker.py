"""Belirli aralıklarla (config.yaml -> worker.interval_minutes, varsayılan 30
dk) otomatik tarama yapan zamanlayıcı: haberleri çeker, gruplar, özetler,
önem skorlar, veritabanına kaydeder ve eşiği geçen haberler için Telegram
bildirimi gönderir (gereksinim #5).

Bu dosya, projenin eski `scheduler.py`'sinin yerini alır ve onu genişletir —
artık sadece `src.main.run_once()`'u çağırmakla kalmıyor, run_once() da
kendi içinde DB kaydı + bildirim adımlarını yürütüyor (bkz. src/main.py).

Bağımsız (worker'ı TEK BAŞINA, web dashboard olmadan) çalıştırma:
    python worker.py

Web dashboard ile birlikte tek komutla başlatmak için:
    python main.py
"""

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src.backup import run_backup
from src.config import load_config
from src.daily_digest import send_daily_digest
from src.db import add_subscriber, init_db
from src.logging_setup import setup_logging
from src.main import run_once
from src.telegram_bot import start_bot_listener_thread, stop_bot_listener_thread
from src.trend_report import send_monthly_trend_report, send_weekly_trend_report

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

    _seed_env_subscriber(config.get("importance", {}).get("threshold", 4))

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
