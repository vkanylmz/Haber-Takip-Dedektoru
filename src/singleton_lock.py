"""`python main.py`'nin AYNI ANDA yalnızca TEK bir kopyasının çalışmasını
garanti eden basit bir PID-dosyası kilidi.

NEDEN GEREKLİ (2026-07-29'da eklendi): Projenin hibrit mimarisinde (bkz.
README > Render'a Deploy) worker/Gemini özetleme SADECE yerel bilgisayarda
`python main.py` ile çalışır ve TEK bir paylaşımlı Neon Postgres + TEK bir
Gemini API anahtarı kullanır. Gerçek bir olayda, kazayla 4 ayrı `python
main.py` süreci aynı anda çalışır bulundu - her biri kendi zamanlayıcısını
başlattığından, Gemini'nin ücretsiz GÜNLÜK istek kotası (500/gün) normalden
kat kat hızlı tüketildi (aynı yeni haberi birden fazla süreç aynı anda
"henüz özetlenmemiş" görüp paralel olarak API'ye gönderebiliyordu). Bu modül
bunun bir daha sessizce tekrarlanmasını önler: ikinci bir kopya başlatılmaya
çalışıldığında AÇIKÇA bir hata verip çıkar.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_LOCK_FILENAME = "main.lock"


def _pid_is_alive(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            # tasklist çalıştırılamadıysa emin olamayız - güvenli tarafta
            # kalıp "canlı" varsayarak yanlışlıkla ikinci bir kopyanın
            # başlamasına izin vermemek daha iyi.
            return True
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # süreç var ama bize ait değil - yine de canlı sayılır
    return True


def acquire_singleton_lock(output_dir: str = "data") -> Path:
    """Zaten çalışan başka bir `python main.py` kopyası varsa AÇIKÇA bir
    hatayla süreci sonlandırır (sys.exit(1)); yoksa kendi PID'ini kilit
    dosyasına yazıp döndürür (kapanışta `release_singleton_lock` ile
    silinmesi için)."""
    lock_path = Path(output_dir) / "state" / _LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            existing_pid = None

        if existing_pid is not None and existing_pid != os.getpid() and _pid_is_alive(existing_pid):
            print(
                "\n"
                "HATA: Bu projenin worker'ı (python main.py) zaten çalışıyor "
                f"(PID {existing_pid}). Aynı anda birden fazla kopya, AYNI paylaşımlı "
                "Gemini API kotasını ve veritabanını gereksiz yere tüketir (bkz. "
                "src/singleton_lock.py). Önce mevcut kopyayı durdurun, sonra tekrar "
                "deneyin. Eğer bu bir hataysa (süreç aslında ölmüş ama kilit dosyası "
                f"kalmış), '{lock_path}' dosyasını elle silip tekrar deneyebilirsiniz.\n",
                file=sys.stderr,
            )
            sys.exit(1)
        # Kilit dosyası var ama sahibi artık canlı değil (stale) - devral.

    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    return lock_path


def release_singleton_lock(lock_path: Path) -> None:
    try:
        if lock_path.exists() and lock_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock_path.unlink()
    except OSError:
        pass
