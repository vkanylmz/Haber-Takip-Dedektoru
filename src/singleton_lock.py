"""`python main.py`'nin AYNI ANDA yalnızca TEK bir kopyasının çalışmasını
garanti eden, gerçek bir OS-seviyesi dosya kilidine dayanan kilit.

NEDEN GEREKLİ (2026-07-29'da eklendi): Projenin hibrit mimarisinde (bkz.
README > Render'a Deploy) worker/Gemini özetleme SADECE yerel bilgisayarda
`python main.py` ile çalışır ve TEK bir paylaşımlı Neon Postgres + TEK bir
Gemini API anahtarı kullanır. Gerçek bir olayda, kazayla 4 ayrı `python
main.py` süreci aynı anda çalışır bulundu - her biri kendi zamanlayıcısını
başlattığından, Gemini'nin ücretsiz GÜNLÜK istek kotası (500/gün) normalden
kat kat hızlı tüketildi.

REVİZYON (2026-07-31): İlk sürüm "dosya var mı kontrol et, sonra PID yaz"
şeklindeydi (check-then-write) - bu ATOMIK DEĞİLDİ, ve gerçekte AYNI hatanın
2. kez yaşanmasına (yine 4 kopya birden, bu sefer Neon veri transfer kotasını
tüketerek) yol açtığı tespit edildi: birden fazla süreç neredeyse aynı anda
başlatıldığında hepsi "kilit dosyası yok" durumunu görüp ilerleyebiliyordu.
Şimdi bunun yerine işletim sisteminin GERÇEK dosya kilidi kullanılıyor
(Windows'ta `msvcrt.locking`, Unix'te `fcntl.flock`, ikisi de LOCK_NB/
LK_NBLCK ile non-blocking) - kilit dosyası process ömrü boyunca AÇIK
tutuluyor, kilidin kendisi atomik olarak işletim sistemi tarafından
veriliyor (check + acquire tek bir sistem çağrısı). Ekstra bonus: süreç
BEKLENMEDİK şekilde çökerse (ör. Ctrl+C, kill -9, OOM) işletim sistemi
dosya tanıtıcısını (file handle) otomatik kapatır ve kilidi de otomatik
serbest bırakır - bu yüzden artık "stale PID" tespiti (tasklist/os.kill(0)
ile canlılık kontrolü) YAPMAYA GEREK YOK, önceki sürümün bu tespitte
barındırdığı ek karmaşıklık/edge-case riski tamamen ortadan kalkıyor.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_LOCK_FILENAME = "main.lock"

# Kilit dosyasının dosya tanıtıcısı (file handle) burada, modül düzeyinde
# tutulur - kilit, bu tanıtıcı AÇIK kaldığı sürece geçerlidir. Süreç
# `release_singleton_lock` ile TEMİZ kapanırsa elle serbest bırakılır;
# beklenmedik bir çökmede işletim sistemi süreç sonlanırken tüm açık dosya
# tanıtıcılarını (ve onlara bağlı kilitleri) otomatik kapatır/serbest bırakır.
_lock_file_handle = None


def acquire_singleton_lock(output_dir: str = "data") -> Path:
    """Zaten çalışan başka bir `python main.py` kopyası varsa AÇIKÇA bir
    hatayla süreci sonlandırır (sys.exit(1)); yoksa kilit dosyasını AÇIK
    tutarak (işletim sistemi seviyesinde kilitli) döndürür - kapanışta
    `release_singleton_lock` ile serbest bırakılması için."""
    global _lock_file_handle

    lock_path = Path(output_dir) / "state" / _LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # "a+b": dosya yoksa oluşturur, varsa İÇERİĞİNİ SİLMEDEN açar - kilit
    # denemesi başarısız olursa aşağıda mevcut PID'i (varsa) okuyup hata
    # mesajına ekleyebilmek için içerik korunuyor.
    fh = open(lock_path, "a+b")

    # msvcrt.locking, kilitlenecek bayt aralığının dosyada GERÇEKTEN var
    # olmasını bekler - yeni oluşturulan/boş bir dosyada 0. bayt henüz
    # mevcut değildir, bu yüzden kilitlemeden ÖNCE en az 1 baytlık bir
    # yer tutucu yazılması gerekir (içerik asıl PID ile aşağıda zaten
    # üzerine yazılacak).
    fh.seek(0, os.SEEK_END)
    if fh.tell() == 0:
        fh.write(b"0")
        fh.flush()
    fh.seek(0)

    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Kilit alınamadı -> başka bir CANLI süreç zaten tutuyor (işletim
        # sistemi kilidi mandatory olduğundan, kilit tutan süreç ölmüşse
        # bu satıra hiç düşülmez - OS kilidi otomatik serbest kalmış olurdu).
        try:
            fh.seek(0)
            existing_pid = fh.read().decode("utf-8", errors="replace").strip()
        except OSError:
            existing_pid = "?"
        fh.close()
        print(
            "\n"
            "HATA: Bu projenin worker'ı (python main.py) zaten çalışıyor "
            f"(PID {existing_pid or '?'}). Aynı anda birden fazla kopya, AYNI "
            "paylaşımlı Gemini API kotasını ve veritabanını gereksiz yere "
            "tüketir (bkz. src/singleton_lock.py). Önce mevcut kopyayı "
            "durdurun, sonra tekrar deneyin.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()).encode("utf-8"))
    fh.flush()

    _lock_file_handle = fh
    return lock_path


def release_singleton_lock(lock_path: Path) -> None:
    """Kilidi ve dosya tanıtıcısını serbest bırakır, kilit dosyasını siler."""
    global _lock_file_handle
    if _lock_file_handle is not None:
        try:
            _lock_file_handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(_lock_file_handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(_lock_file_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        _lock_file_handle.close()
        _lock_file_handle = None

    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass
