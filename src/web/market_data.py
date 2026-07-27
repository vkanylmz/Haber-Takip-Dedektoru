"""Canlı piyasa verisi (döviz, emtia, endeksler) için ücretsiz, API-key
gerektirmeyen bir kaynak: Yahoo Finance'in "chart" endpoint'i
(query1.finance.yahoo.com/v8/finance/chart/<sembol>).

Neden bu kaynak seçildi: Yahoo'nun eski toplu sorgu endpoint'i olan
"v7/finance/quote" artık anonim isteklerde kimlik doğrulama (crumb/cookie)
istiyor ve "Unauthorized" hatası döndürüyor (gerçek bir istekle doğrulandı,
2026-07). Buna karşın sembol-bazlı "v8/finance/chart" endpoint'i hâlâ
API-key/kimlik doğrulama gerektirmeden herkese açık ve ihtiyaç duyulan TÜM
sembol tiplerini (döviz çifti, emtia vadeli işlemi, borsa endeksi)
destekliyor - her sembol gerçek bir istekle tek tek test edilerek
doğrulandı (bkz. README).

BIST100 (XU100.IS) için "gerçek zamanlı" araştırması (2026-07): Yahoo'nun
BIST100 verisini ücretsiz erişimde ~15 dakika geciktirdiği ölçümle
doğrulandıktan sonra, alternatif ücretsiz kaynaklar araştırıldı:
  - TradingView'ın ücretsiz widget/embed'i: BIST verisi için VARSAYILAN
    olarak YİNE ~15 dk gecikmeli (TradingView'ın kendi belgelenen politikası
    - gerçek zamanlı BIST verisi yalnızca kimlik doğrulamalı bir broker
    entegrasyonuyla açılıyor, anonim/herkese açık bir widget'ta yok).
  - "borsapy" gibi BIST'e özel Python kütüphaneleri de veriyi TradingView'ın
    WebSocket API'sinden çekiyor ve resmi belgelerinde "varsayılan olarak
    ~15 dk gecikmeli" olduğunu açıkça belirtiyor.
  - Sonuç: bu 15 dakikalık gecikme, tek bir kaynağın (Yahoo) kısıtı değil,
    Borsa İstanbul'un gerçek zamanlı veri dağıtımını ücretli lisansa
    bağlamasından kaynaklanan YAPISAL bir kısıt - herhangi bir ücretsiz/
    anonim kaynak (Yahoo, TradingView, vb.) aynı gecikmeye tabi. Gerçek
    real-time erişim ancak (a) resmi BIST veri lisansı satın alarak ya da
    (b) kullanıcının kendi aracı kurum hesabına kimlik doğrulamasıyla
    mümkün - ikisi de bu projenin kapsamı dışında (kimlik bilgisi/ödeme
    gerektiriyor). Bu yüzden kaynak DEĞİŞTİRİLMEDİ; bunun yerine
    `is_delayed`/`delay_minutes` alanlarıyla durum şeffaf şekilde
    etiketleniyor (bkz. _fetch_one, dashboard.html > ticker-closed etiketi).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (FinansHaberToplayiciBot market ticker)"}

# (Yahoo sembolü, ekranda gösterilecek ad) - sıra, dashboard'daki gösterim sırasıdır.
MARKET_SYMBOLS: list[tuple[str, str]] = [
    ("TRY=X", "USD/TRY"),
    ("EURTRY=X", "EUR/TRY"),
    ("GC=F", "Ons Altın"),
    ("BZ=F", "Brent Petrol"),
    ("^VIX", "VIX"),
    ("XU100.IS", "BIST 100"),
    ("^GSPC", "S&P 500"),
    ("^IXIC", "NASDAQ"),
    ("^DJI", "Dow Jones"),
    ("^GDAXI", "DAX"),
    ("^FTSE", "FTSE 100"),
    ("^N225", "Nikkei 225"),
]

# Bu değer, GERÇEK ölçümle belirlendi (bkz. README > Piyasa Verisi Tazelik
# Testi) - "1 saniye" gibi keyfi/iyimser bir sayı değil:
#   1) Rate-limit testi: XU100.IS'e 20 istek 0.4 sn aralıklarla, sonra 30
#      istek 2 sn aralıklarla, sonra ~5 dk boyunca 12 sn aralıklarla - TOPLAM
#      ~98 gerçek istekte TEK BİR 429/hata alınmadı. Yani rate-limit, kısa
#      aralıkları (ör. saniyede bir) engelleyen bağlayıcı bir kısıt DEĞİL.
#   2) Asıl bağlayıcı kısıt kaynağın kendisi: XU100.IS (BIST100) verisi bu
#      ücretsiz/anonim endpoint'te HER ZAMAN ~900 saniye (15 DAKİKA) gecikmeli
#      geliyor (regularMarketTime ile gerçek an arasındaki fark, ~5 dakikalık
#      test boyunca sürekli 900-909 sn olarak ölçüldü - Borsa İstanbul'un
#      gerçek zamanlı veri lisansı gerektirmesi nedeniyle Yahoo'nun bilinçli
#      uyguladığı standart bir gecikme). Bu yüzden BIST100'ü saniyede bir
#      sorgulamanın "daha güncel" bir değer getirmesi mümkün değil - kaynak
#      zaten hep 15 dk geride. USD/TRY gibi döviz çiftleri ise ~0 sn gecikmeyle
#      (gerçek zamana yakın) geliyor.
#   3) Ancak o 15 dk geride olan akışın KENDİSİ donuk değil - gerçek testte
#      regularMarketTime'ın (ve fiyatın) ~3-13 sn'de bir (ortalama ~6-7 sn)
#      ilerlediği gözlendi. Yani 10 sn'lik bir aralık, kaynağın kendi tik
#      hızına yakın durarak neredeyse her gerçek güncellemeyi yakalar - daha
#      sık sormanın (rate-limit güvenli olsa da) somut bir faydası olmaz.
# Sonuç: 10 sn seçildi - kaynağın gerçek tik hızına en yakın, güvenlik payı
# hâlâ çok yüksek (test edilen en agresif aralığın [2 sn] 5 katı kadar
# gevşek) bir değer.
_CACHE_TTL_SECONDS = 10.0
_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}

# "Thundering herd" koruması: dashboard'daki her açık sekme/kullanıcı, ticker'ı
# TAM OLARAK aynı `_CACHE_TTL_SECONDS` (10 sn) aralığıyla yeniliyor (bkz.
# dashboard.html > setInterval(refreshMarketTicker, 10000)) - önbellek TAM
# tükendiği anda birden fazla kullanıcının isteği çakışırsa, kilit OLMADAN
# HEPSİ bağımsız olarak Yahoo Finance'e 12 sembol için istek atardı (2
# kullanıcı = 24, 10 kullanıcı = 120 EŞZAMANLI istek). Bu, GERÇEK bir yükte
# doğrulandı (2026-07): 10 eşzamanlı istek Render'ın kısıtlı (0.1 vCPU)
# ücretsiz katmanında ~8 saniyeye kadar sürdü - httpx client timeout'una
# (8.0 sn) neredeyse çarpıyordu, biraz daha yük/ağ gecikmesiyle gerçek
# zaman aşımlarına (ve dolayısıyla "veri çekilemedi" hatasına) yol açması
# an meselesiydi. Bu kilit, önbellek tazelemesini TEK BİR eşzamanlı
# çağrıyla sınırlar - diğerleri kilidi bekleyip TAZE önbellekten okur,
# gerçek Yahoo Finance istek sayısı kullanıcı sayısından BAĞIMSIZ olarak
# her zaman en fazla 12'de kalır.
_refresh_lock = asyncio.Lock()


async def _fetch_one(client: httpx.AsyncClient, symbol: str, label: str) -> dict[str, Any] | None:
    try:
        response = await client.get(
            _CHART_URL.format(symbol=symbol),
            params={"interval": "1d", "range": "1d"},
            headers=_HEADERS,
        )
        response.raise_for_status()
        meta = response.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev_close = meta.get("regularMarketPreviousClose") or meta.get("chartPreviousClose")
        if price is None or not prev_close:
            return None
        change_pct = (price - prev_close) / prev_close * 100.0

        # Yahoo'nun chart meta'sında ayrı bir "marketState" alanı yok (bu,
        # kimlik doğrulama isteyen v7/quote endpoint'inde var ama biz onu
        # kullanmıyoruz - bkz. modül docstring'i). Bunun yerine HER sembol
        # için sağlanan `currentTradingPeriod.regular` (bugünün normal seans
        # başlangıç/bitiş UNIX epoch zamanı) ile şu anki zamanı kıyaslıyoruz:
        # piyasa kapalıyken Yahoo zaten son işlem fiyatını döndürmeye devam
        # ediyor (fiyat güncellenmiyor) - biz sadece bunu "kapalı, son kapanış"
        # olarak etiketliyoruz, ayrı bir "son kapanış" isteği yapmaya gerek yok.
        regular_period = meta.get("currentTradingPeriod", {}).get("regular", {})
        period_start = regular_period.get("start")
        period_end = regular_period.get("end")
        is_open = (
            period_start is not None
            and period_end is not None
            and period_start <= time.time() <= period_end
        )

        # Bazı borsalar (ör. Borsa İstanbul) gerçek zamanlı veri dağıtımı için
        # ücretli bir lisans şartı koyuyor; ücretsiz/anonim erişimde Yahoo
        # (ve TradingView dahil hemen hemen tüm ücretsiz kaynaklar - bkz.
        # README > Piyasa Verisi Araştırması) bu sembolü sabit ~15 dk geriden
        # veriyor. Bunu HARDCODE ETMEK yerine (ör. "XU100.IS ise gecikmelidir"
        # gibi kırılgan bir varsayım) `regularMarketTime` (verinin GERÇEKTEN
        # üretildiği an) ile şu anki zaman arasındaki farkı ölçüyoruz - hangi
        # sembol gecikmeliyse otomatik olarak öyle etiketlenir, kaynak
        # davranışı değişirse kod kendiliğinden buna uyar. 120 sn eşiği,
        # gerçek zamanlı sembollerde gözlenen ~0-2 sn'lik doğal gecikmeyi
        # (ağ/işlem gecikmesi) yanlışlıkla "gecikmeli" saymayacak kadar
        # yüksek, ama 15 dk'lık gerçek gecikmeyi kaçırmayacak kadar düşük.
        #
        # Yalnızca piyasa AÇIKKEN "gecikmeli" say: piyasa kapalıyken
        # regularMarketTime zaten kapanış anına sabitlenir ve "şu ana göre
        # yaşı" kapanıştan bu yana geçen (saatler sürebilen, tamamen normal)
        # süreyi yansıtır - bu bir "vendor gecikmesi" değil, zaten ayrı
        # "🔒 kapalı" etiketiyle karşılanıyor (bkz. is_open).
        market_time = meta.get("regularMarketTime")
        age_seconds = (time.time() - market_time) if market_time else 0
        is_delayed = is_open and age_seconds > 120
        delay_minutes = round(age_seconds / 60) if is_delayed else 0

        return {
            "symbol": symbol,
            "label": label,
            "price": round(price, 4),
            "change_pct": round(change_pct, 2),
            "currency": meta.get("currency", ""),
            "is_open": is_open,
            "is_delayed": is_delayed,
            "delay_minutes": delay_minutes,
        }
    except Exception:  # noqa: BLE001 - tek bir sembolün başarısız olması diğerlerini etkilemesin
        logger.warning("Piyasa verisi alınamadı: %s", symbol, exc_info=True)
        return None


# --------------------------------------------------------------------------
# "Detaylı İnceleme" > Şirket sütunu: haberdeki "BORSA: SEMBOL" (ör.
# "NASDAQ: TSLA", "BIST: THYAO" - bkz. src/summarizer.py > company_ticker)
# etiketi için, MÜMKÜNSE gerçek anlık fiyat/günlük değişim de gösterilir.
# Bu, yukarıdaki sabit MARKET_SYMBOLS listesinden TAMAMEN AYRI, keyfi/
# dinamik semboller içeren bir mekanizmadır - kendi (daha uzun ömürlü,
# 30 sn) önbelleğine sahiptir; ana piyasa şeridinin 10 sn'lik önbelleğiyle
# KARIŞTIRILMAZ. Opsiyonel/bonus bir özelliktir: bir sembol çözülemez veya
# Yahoo'dan veri alınamazsa o haber için sessizce fiyat gösterilmez (sayfa
# hata vermez).
# --------------------------------------------------------------------------

# Borsa kodundan Yahoo Finance sembol sonekine kaba bir eşleme. Kapsamlı
# değildir (özellikle "EURONEXT" gibi çok şehirli borsalar için Paris
# varsayıldı) - amaç, modelin ürettiği en yaygın borsa kodları için makul
# bir tahmin sağlamak; eşleşmeyen bir kod soneksiz (ör. ABD borsaları gibi)
# denenir.
_EXCHANGE_YAHOO_SUFFIX: dict[str, str] = {
    "NASDAQ": "",
    "NYSE": "",
    "NYSEAMERICAN": "",
    "AMEX": "",
    "BIST": ".IS",
    "LSE": ".L",
    "TSE": ".T",
    "TYO": ".T",
    "HKEX": ".HK",
    "HKSE": ".HK",
    "SGX": ".SI",
    "SSE": ".SS",
    "SZSE": ".SZ",
    "ASX": ".AX",
    "TWSE": ".TW",
    "KRX": ".KS",
    "KOSPI": ".KS",
    "EURONEXT": ".PA",
}

_TICKER_QUOTE_CACHE_TTL_SECONDS = 30.0
_ticker_quote_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _resolve_yahoo_symbol(company_ticker: str) -> str | None:
    """"NASDAQ: TSLA" -> "TSLA", "BIST: THYAO" -> "THYAO.IS" gibi. Beklenen
    "BORSA: SEMBOL" formatında değilse (ör. ":" yoksa) None döner."""
    if not company_ticker or ":" not in company_ticker:
        return None
    exchange, _, symbol = company_ticker.partition(":")
    exchange = exchange.strip().upper()
    symbol = symbol.strip().upper()
    if not symbol:
        return None
    suffix = _EXCHANGE_YAHOO_SUFFIX.get(exchange, "")
    return f"{symbol}{suffix}"


async def get_quotes_for_company_tickers(company_tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Verilen "BORSA: SEMBOL" string listesi için, çözülebilen ve Yahoo'dan
    gerçek veri alınabilenler için {orijinal_company_ticker: quote_dict} döner.
    Çözülemeyen/başarısız olanlar sonuçta YER ALMAZ (exception fırlatmaz)."""
    now = time.monotonic()
    to_fetch: dict[str, str] = {}  # yahoo_symbol -> orijinal company_ticker
    results: dict[str, dict[str, Any]] = {}

    for ct in set(company_tickers):
        yahoo_symbol = _resolve_yahoo_symbol(ct)
        if not yahoo_symbol:
            continue
        cached = _ticker_quote_cache.get(yahoo_symbol)
        if cached and (now - cached[0]) < _TICKER_QUOTE_CACHE_TTL_SECONDS:
            if cached[1] is not None:
                results[ct] = cached[1]
        else:
            to_fetch[yahoo_symbol] = ct

    if to_fetch:
        async with httpx.AsyncClient(timeout=8.0) as client:
            fetched = await asyncio.gather(*(_fetch_one(client, sym, sym) for sym in to_fetch))
        for sym, quote in zip(to_fetch.keys(), fetched):
            _ticker_quote_cache[sym] = (now, quote)
            if quote is not None:
                results[to_fetch[sym]] = quote

    return results


async def get_market_snapshot() -> list[dict[str, Any]]:
    """İzlenen tüm sembollerin güncel fiyat/günlük değişim yüzdesini,
    dashboard'daki gösterim sırasına göre döner. Başarısız olan semboller
    sessizce listeden çıkarılır (bkz. _fetch_one).

    Eşzamanlı çağrılara karşı İKİ katmanlı koruma:
      1. `_refresh_lock` - önbellek tazelemesi aynı anda yalnızca TEK bir
         çağrı tarafından yapılır (bkz. modül seviyesindeki not).
      2. Tazeleme TAMAMEN başarısız olursa (ör. geçici bir ağ sorunu - TÜM
         sembollerin isteği başarısız olur) önbellekteki ESKİ ama GERÇEK
         veri SİLİNMEZ - kullanıcıya aniden boş/hata göstermek yerine biraz
         eski ama gerçek veri gösterilmeye devam edilir, bir sonraki
         tazeleme denemesi (10 sn sonra) başarılı olursa güncellenir.
    """
    now = time.monotonic()
    if _cache["data"] is not None and (now - _cache["fetched_at"]) < _CACHE_TTL_SECONDS:
        return _cache["data"]

    async with _refresh_lock:
        # Kilit ALINANA KADAR başka bir eşzamanlı çağrı önbelleği zaten
        # tazelemiş olabilir - tekrar kontrol et (double-checked locking).
        now = time.monotonic()
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < _CACHE_TTL_SECONDS:
            return _cache["data"]

        async with httpx.AsyncClient(timeout=8.0) as client:
            results = await asyncio.gather(*(_fetch_one(client, sym, label) for sym, label in MARKET_SYMBOLS))

        data = [r for r in results if r is not None]

        if len(data) == len(MARKET_SYMBOLS):
            # Tam başarı - önbelleği doğrudan güncelle.
            _cache["data"] = data
            _cache["fetched_at"] = now
        elif data:
            # KISMİ başarı (ör. 12 semboldan sadece birkaçı) - bu turda
            # BAŞARISIZ olan sembollerin ESKİ (varsa) değerlerini KORUYARAK
            # birleştir, ham `data`'yı doğrudan önbelleğe YAZMA. Aksi halde
            # kısmi bir başarı, önceki TAM (ör. 12/12) önbelleği aniden
            # sparse bir veriyle EZERDİ - bu, GERÇEK production'da (Render'da,
            # 2026-07) gözlemlendi: Yahoo Finance bazı sembolleri geçici
            # olarak reddederken, önbellek her turda 1-2 sembole düşüyor,
            # kullanıcı önceden gördüğü diğer 10-11 sembolü KAYBEDİYORDU.
            previous_by_symbol = {item["symbol"]: item for item in (_cache["data"] or [])}
            new_by_symbol = {item["symbol"]: item for item in data}
            merged = [
                new_by_symbol.get(symbol) or previous_by_symbol.get(symbol)
                for symbol, _label in MARKET_SYMBOLS
            ]
            _cache["data"] = [item for item in merged if item is not None]
            _cache["fetched_at"] = now
            logger.warning(
                "Piyasa verisi tazeleme denemesi KISMİ başarılı (%d/%d sembol) - "
                "eksik semboller için önbellekteki eski değerler korundu.",
                len(data), len(MARKET_SYMBOLS),
            )
        elif _cache["data"] is not None:
            logger.warning(
                "Piyasa verisi tazeleme denemesi TAMAMEN başarısız oldu (0/%d sembol) - "
                "önbellekteki eski veri korunuyor, bir sonraki denemede tekrar denenecek.",
                len(MARKET_SYMBOLS),
            )
        else:
            # İlk çalıştırmadan beri hiç başarılı veri yok - gösterilecek
            # "eski" bir veri da yok, boş liste dönmek zorundayız.
            _cache["data"] = data
            _cache["fetched_at"] = now

        return _cache["data"]
