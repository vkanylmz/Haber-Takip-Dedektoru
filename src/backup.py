"""Veritabanının (data/finans_haber.db) haftalık otomatik yedeklenmesi.

Her Pazar 03:00'te (Europe/Istanbul - düşük trafikli bir saat, diğer haftalık/
aylık zamanlanmış görevlerle - bkz. worker.py > "haftalik_trend"/"aylik_trend",
Pazartesi 09:30/09:45 - ÇAKIŞMASIN diye ayrı bir gün seçildi) çalışan bir
zamanlanmış görev, veritabanının tarihli bir kopyasını data/backups/ altına
alır ve 8 haftadan eski yedekleri otomatik temizler.

ÖNEMLİ: Veritabanı WAL (Write-Ahead Logging) modunda çalıştığından (bkz.
src/db.py > _configure_sqlite_for_concurrency), dosyayı ham (`shutil.copy`)
kopyalamak TUTARSIZ bir görüntü alabilir (henüz -wal dosyasından ana dosyaya
"checkpoint" edilmemiş yazmalar kaybolabilir). Bunun yerine Python'un yerleşik
`sqlite3.Connection.backup()` API'si kullanılır - bu, WAL durumundan bağımsız
olarak TUTARLI, TEK dosyalık bir anlık görüntü üretir (SQLite'ın resmi önerilen
yedekleme yöntemi).

Yalnızca YEREL bir yedekleme - Drive/bulut gibi bir yere otomatik yükleme
KASITLI OLARAK yapılmaz (bkz. gereksinim); kullanıcı isterse data/backups/
içindeki dosyaları elle taşıyabilir.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 8 haftadan eski yedekler otomatik silinir - disk şişmesin diye.
RETENTION_WEEKS = 8

_BACKUP_FILENAME_RE = re.compile(r"^finans_haber_(\d{4})-(\d{2})-(\d{2})\.db$")


def _backup_sqlite_db(source_path: Path, dest_path: Path) -> None:
    """SQLite'ın yerleşik backup API'siyle tutarlı, tek dosyalık bir kopya
    üretir (bkz. modül docstring'i - WAL modunda ham dosya kopyalamaktan
    KESİNLİKLE daha güvenilirdir)."""
    src_conn = sqlite3.connect(str(source_path))
    try:
        dest_conn = sqlite3.connect(str(dest_path))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def _cleanup_old_backups(backup_dir: Path) -> None:
    """`RETENTION_WEEKS`'ten eski yedek dosyalarını siler. Yalnızca BU
    modülün ürettiği isimlendirme desenine (finans_haber_YYYY-MM-DD.db) uyan
    dosyalara dokunur - kullanıcının data/backups/ altına elle koyduğu başka
    dosyalar (ör. Drive'a taşımadan önce burada biriktirdiği bir şey) asla
    silinmez."""
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=RETENTION_WEEKS)
    for path in backup_dir.iterdir():
        if not path.is_file():
            continue
        match = _BACKUP_FILENAME_RE.match(path.name)
        if not match:
            continue
        try:
            file_date = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                path.unlink()
                logger.info("Eski veritabanı yedeği silindi (%d haftadan eski): %s", RETENTION_WEEKS, path.name)
            except OSError:
                logger.exception("Eski veritabanı yedeği silinemedi: %s", path.name)


def run_backup(config: dict[str, Any]) -> None:
    """Haftalık veritabanı yedeğini alır ve eski yedekleri temizler. Herhangi
    bir hata durumunda exception fırlatmaz (çağıran taraf, ör. worker.py'deki
    cron job, sadece loglar - bir sonraki haftanın yedeği etkilenmez)."""
    try:
        db_path = Path(config.get("database", {}).get("path", "data/finans_haber.db"))
        if not db_path.exists():
            logger.warning("Yedeklenecek veritabanı dosyası bulunamadı: %s", db_path)
            return

        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dest_path = backup_dir / f"finans_haber_{today_str}.db"

        _backup_sqlite_db(db_path, dest_path)
        logger.info("Veritabanı yedeği alındı: %s", dest_path)

        _cleanup_old_backups(backup_dir)
    except Exception:  # noqa: BLE001 - yedekleme asla ana uygulamayı etkilemesin
        logger.exception("Veritabanı yedeklenirken beklenmeyen bir hata oluştu.")
