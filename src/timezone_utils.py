"""Türkiye saat dilimi (Europe/Istanbul) gösterim yardımcıları.

Veritabanında TÜM zaman damgaları UTC olarak saklanır ve saklanmaya devam
eder (bkz. src/db.py > datetime.now(timezone.utc)) - bu modül SADECE
gösterim/format katmanında (web dashboard, Telegram mesajları) kullanıcıya
Türkiye saatiyle göstermek için bir dönüşüm sağlar.

Neden sabit "UTC+3" offset'i YERİNE `zoneinfo.ZoneInfo("Europe/Istanbul")`:
Türkiye 2016'dan beri yaz/kış saati uygulamıyor (sabit UTC+3), bu yüzden
bugün için ikisi de aynı sonucu verir. Ama IANA saat dilimi veritabanı
(zoneinfo/tzdata) kullanmak, Türkiye'nin saat politikasında GELECEKTE
olası bir değişiklik (teorik de olsa) olursa kod değişikliği GEREKTİRMEDEN
otomatik doğru sonucu vermeye devam eder - hardcoded bir offset bu tür bir
değişiklikte sessizce yanlış sonuç üretirdi. Bu yüzden "en doğru ve
gelecekte sorun çıkarmayacak yöntem" olarak zoneinfo tercih edildi.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TURKEY_TZ = ZoneInfo("Europe/Istanbul")


def to_turkey_time(dt: datetime) -> datetime:
    """Verilen datetime'ı Türkiye saat dilimine çevirir. `dt` tz-aware
    değilse (bu projede pratikte hep UTC olarak üretiliyor, bkz.
    datetime.now(timezone.utc)) savunmacı olarak UTC kabul edilir."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TURKEY_TZ)


def format_turkey_time(dt: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """`to_turkey_time` + `strftime` kısayolu - çağıran taraf `dt is None`
    durumunu (her yerde farklı bir yedek metin istenebildiğinden, ör.
    "tarih bilinmiyor" vs "—") kendisi ele alır."""
    return to_turkey_time(dt).strftime(fmt)
