"""TradingView sembol doğrulaması (2026-08-19, kullanıcı geri bildirimi):
dashboard'daki "Teknik Görünüm" butonları bazı KAP kayıtlarında GERÇEKTEN
var olmayan bir TradingView sembolüne (ör. çok küçük/az işlem gören BIST
şirketleri TradingView'in veritabanında hiç olmayabilir) gidiyordu.

Bu modül, TradingView'in KENDİ web sitesinin sembol sayfasını yüklerken
kullandığı, dokümante edilmemiş ama kimlik doğrulaması gerektirmeyen bir
servisi (`scanner.tradingview.com/symbol`) sorgulayarak bir sembolün
GERÇEKTEN var olup olmadığını doğrular - GERÇEK bir tarayıcı ağ trafiği
yakalamasıyla bulundu (bkz. commit notu), 90 gerçek sembolle test edildi:
61 geçerli, 29 (~%32) TradingView'de YOK çıktı - kullanıcının şüphesini
doğruladı.

ÖNEMLİ: Bu, TradingView'in RESMİ/dokümante bir API'si DEĞİL - kendi
sitesinin kullandığı iç bir servis, yarın değişebilir/kapanabilir riski
var. Proje zaten Yahoo Finance'in BENZER şekilde dokümante olmayan chart
endpoint'ini kullanıyor (bkz. src/web/market_data.py) - aynı risk
toleransı. Kırılırsa etkisi sınırlı: doğrulama None (bilinmiyor) döner,
"Teknik Görünüm" butonu o turda gösterilmez, sitenin geri kalanını
ETKİLEMEZ (bkz. validate_symbol - HİÇBİR exception dışarı sızmaz).
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# Header'sız istekler 403 dönüyor (GERÇEK testte doğrulandı) - gerçek bir
# tarayıcının gönderdiği User-Agent/Referer/Origin taklit edilir.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.tradingview.com/",
    "Origin": "https://www.tradingview.com",
    "Accept": "application/json",
}
_VALIDATE_URL = "https://scanner.tradingview.com/symbol"
_TIMEOUT_SECONDS = 5.0


def validate_symbol(symbol: str) -> bool | None:
    """"BORSA:SEMBOL" (ör. "BIST:EFOR") GERÇEKTEN TradingView'de var mı?

    - Sembol varsa: True (TradingView 200 + sembol bilgisiyle yanıt verir).
    - Sembol GERÇEKTEN yoksa: False (TradingView 404 +
      {"code":"symbol_not_exists"} ile yanıt verir - GERÇEK testte
      doğrulandı).
    - Ağ hatası/zaman aşımı/beklenmedik bir durum kodu gibi SONUCU
      BELİRSİZLEŞTİREN bir durumda: None (bilinmiyor) - çağıran taraf bunu
      "geçersiz" ile KARIŞTIRMAMALI, sadece "şu an doğrulanamadı" anlamına
      gelir (bkz. src/summarizer.py - None durumunda buton o turda
      gösterilmez ama kayıt kalıcı olarak "geçersiz" işaretlenmez, ileride
      backfill ile tekrar denenebilir).
    """
    if not symbol:
        return None
    try:
        resp = httpx.get(
            _VALIDATE_URL,
            params={"symbol": symbol, "fields": "name"},
            headers=_HEADERS,
            timeout=_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - doğrulama asla worker/dashboard akışını KIRMASIN
        logger.warning("TradingView sembol doğrulaması başarısız (ağ hatası): %s", symbol, exc_info=True)
        return None

    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    logger.warning(
        "TradingView sembol doğrulaması beklenmedik durum kodu döndürdü: %s -> %s", symbol, resp.status_code
    )
    return None


def find_valid_bist_symbol(stock_codes: str) -> tuple[str | None, bool | None]:
    """KAP'ın OTORİTER `stockCodes` alanından ("YKB, YKBNK" gibi virgülle
    ayrılmış, bazen birden fazla kod) TÜM kodları TradingView'de SIRAYLA
    dener - İLK GEÇERLİ olanı "BIST:KOD" formatında döner (2026-08-19,
    kullanıcı geri bildirimi ile bulundu: KAP'ın listelediği İLK kod her
    zaman TradingView'in kullandığı asıl kod DEĞİL - ör. "YKB" TradingView'de
    yok ama AYNI kaydın listesindeki "YKBNK" var, GERÇEK testle doğrulandı).

    Dönüş: (sembol, valid)
      - Hiç kod yoksa: (None, None).
      - Kodlardan biri geçerliyse: (o kod, True) - İLK geçerliyi bulduğu
        anda diğerlerini DENEMEZ.
      - Hiçbiri geçerli değilse (hepsi kesin 404): (ilk kod, False) - eski
        davranışla (ilk kodu kullan) GERİYE DÖNÜK UYUMLU, sadece artık
        "geçersiz" olduğu bilgisiyle işaretli.
      - Bazı kodlar doğrulanamadıysa (ağ hatası, bkz. validate_symbol)
        AMA hiçbiri kesin geçerli çıkmadıysa: (ilk kod, None) - "geçersiz"
        diye KESİN işaretlemek yerine belirsiz bırakılır, ileride tekrar
        denenebilir.
    """
    codes = [c.strip() for c in (stock_codes or "").split(",") if c.strip()]
    if not codes:
        return None, None

    any_unknown = False
    for code in codes:
        symbol = f"BIST:{code}"
        result = validate_symbol(symbol)
        if result is True:
            return symbol, True
        if result is None:
            any_unknown = True

    first_symbol = f"BIST:{codes[0]}"
    return first_symbol, (None if any_unknown else False)
