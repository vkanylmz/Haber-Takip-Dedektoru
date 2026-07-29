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
import re
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
# Sonuç (İLK karar, 2026-07): 12 sn seçildi - kaynağın gerçek tik hızına en
# yakın, güvenlik payı hâlâ çok yüksek (test edilen en agresif aralığın
# [2 sn] 6 katı kadar gevşek) bir değer.
#
# GERÇEK PRODUCTION SONUCU (2026-07-29, birkaç GÜNLÜK sürekli çalıştırmadan
# sonra): yukarıdaki test yalnızca ~5 dk'lık KISA bir burst'ü ölçmüştü - bu,
# Yahoo'nun SÜRDÜRÜLEBİLİR/uzun-vadeli hacim sınırını YANSITMIYORDU. 12 sn'de
# bir 12 sembol = günde ~86.400 istek, GÜNLERCE sürekli aynı (Render'ın sabit)
# IP'den - Render'ın Render loglarında GERÇEKTEN gözlemlendi: TÜM sembollerde
# "429 Too Many Requests" (bkz. sohbet). Bu yüzden değer 90 sn'ye çıkarıldı
# (~7.5 kat azaltma, günde ~11.520 isteğe iner) VE aşağıdaki 429-özel
# "cooldown" mekanizması (bkz. _enter_yahoo_cooldown) eklendi - bir 429
# alındığında, engel devam ederken YİNE DE her turda tekrar denemek (önceki
# davranış) engeli UZATIYOR/pekiştiriyor olabilir; artık 429 görülünce bir
# süre TÜM Yahoo isteği (arka plan VE anlık ticker sorguları) tamamen durur.
#
# MİMARİ NOT (2026-07, çoklu kullanıcı performans düzeltmesi): bu değer
# ARTIK bir "TTL" (kullanıcı isteği geldiğinde ne kadar eski veri kabul
# edilir) DEĞİL - bir arka plan görevinin (bkz. _background_refresh_loop)
# önbelleği ne sıklıkla PROAKTİF olarak tazelediğini belirler. Öncesinde
# (reaktif model) bir kullanıcı isteği önbellek "bayatladığında" Yahoo'ya
# GERÇEK ZAMANLI olarak gidip _refresh_lock'u BEKLEMEK zorunda kalıyordu -
# gerçek testte bu, 2-3 eşzamanlı kullanıcıda 900-1200ms'lik gerçek kilit
# bekleme süresine yol açtığı ÖLÇÜLDÜ. Şimdi arka plan görevi önbelleği
# kullanıcıdan TAMAMEN BAĞIMSIZ olarak sürekli taze tutuyor -
# `get_market_snapshot()` artık hiçbir zaman kilit BEKLEMİYOR, sadece
# hazır veriyi okuyor (soğuk başlangıç anındaki TEK seferlik istisna
# hariç, bkz. get_market_snapshot).
_BACKGROUND_REFRESH_INTERVAL_SECONDS = 90.0
_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}

# --- Yahoo 429 ("Too Many Requests") circuit breaker (2026-07-29) ---
# Gerçek production'da (Render, birkaç günlük sürekli çalıştırma sonrası)
# Yahoo'nun bu anonim/ücretsiz endpoint'i TÜM sembollerde 429 döndürmeye
# başladığı gözlemlendi. Bir 429 görüldüğünde, engel süresince YİNE DE
# yeni istek göndermeye devam etmek (eski davranış: her arka plan turunda
# veya her kullanıcı tıklamasında tekrar dene) muhtemelen engeli
# UZATIYOR/pekiştiriyor - bu yüzden 429 görülür görülmez TÜM Yahoo
# isteği (hem arka plan şeridi HEM anlık ticker/emtia geçmişi sorguları)
# bir süreliğine TAMAMEN durdurulur; önbellekteki ESKİ veri (varsa) o süre
# boyunca gösterilmeye devam eder, hiçbir yeni ağ isteği YAPILMAZ.
_YAHOO_COOLDOWN_SECONDS = 300.0
_yahoo_cooldown_until = 0.0


def _yahoo_in_cooldown() -> bool:
    return time.monotonic() < _yahoo_cooldown_until


def _enter_yahoo_cooldown() -> None:
    global _yahoo_cooldown_until
    _yahoo_cooldown_until = time.monotonic() + _YAHOO_COOLDOWN_SECONDS
    logger.warning(
        "Yahoo Finance 429 (Too Many Requests) döndürdü - %.0f sn boyunca HİÇBİR yeni Yahoo isteği gönderilmeyecek "
        "(bkz. src/web/market_data.py > _enter_yahoo_cooldown).",
        _YAHOO_COOLDOWN_SECONDS,
    )

# "Thundering herd" koruması: HEM arka plan görevinin kendi periyodik
# tazelemesi HEM DE soğuk başlangıç anında (süreç yeni başladığında, arka
# plan görevi henüz ilk tazelemesini yapmamışken) birden fazla kullanıcının
# isteği çakışırsa, bu kilit ikisinin AYNI ANDA Yahoo'ya gitmesini engeller
# (bkz. get_market_snapshot'taki soğuk başlangıç fallback'i). Normal
# (soğuk olmayan) durumda kullanıcı istekleri bu kilide HİÇ dokunmaz.
_refresh_lock = asyncio.Lock()

# Arka plan tazeleme görevinin kendi referansı - start_background_refresh()
# idempotent olsun diye (ör. yeniden import/reload durumunda ikinci bir
# döngü başlatılmasın) tutulur.
_background_task: "asyncio.Task[None] | None" = None


async def fetch_symbol_history(
    client: httpx.AsyncClient, symbol: str, interval: str, range_: str
) -> list[dict[str, Any]]:
    """Verilen sembol için Yahoo chart endpoint'inden GÜNLÜK/HAFTALIK fiyat
    GEÇMİŞİ çeker (bkz. src/commodities.py > Faz 1: haftalık emtia raporu
    trend grafiği + geçen haftaki değişim hesabı bunu kullanır). `_fetch_one`
    ile AYNI endpoint'i (`_CHART_URL`) kullanır ama farklı bir yanıt şeklini
    (`meta` yerine `timestamp[]`/`indicators.quote[0].close[]` dizileri)
    ayrıştırır - o yüzden ayrı bir fonksiyon.

    `interval`: "1d" (günlük) veya "1wk" (haftalık) gibi Yahoo'nun kabul
    ettiği değerler. `range_`: "1mo", "6mo" gibi. Hata durumunda (sembol
    geçersiz, ağ hatası, veri yok) boş liste döner - exception fırlatmaz,
    çağıran taraf tek bir emtianın geçmişinin çekilememesi yüzünden tüm
    raporun durmasını istemez.

    Yahoo zaten 429 ile engellemişse (bkz. _yahoo_in_cooldown) hiç istek
    ATILMAZ - hem gereksiz bir isteği önler hem de aktif engeli
    uzatmamış olur."""
    if _yahoo_in_cooldown():
        return []
    try:
        response = await client.get(
            _CHART_URL.format(symbol=symbol),
            params={"interval": interval, "range": range_},
            headers=_HEADERS,
        )
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        closes = result["indicators"]["quote"][0].get("close") or []
        return [
            {"timestamp": ts, "close": close}
            for ts, close in zip(timestamps, closes)
            if close is not None
        ]
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            _enter_yahoo_cooldown()
        logger.warning("Fiyat geçmişi alınamadı: %s (interval=%s, range=%s)", symbol, interval, range_, exc_info=True)
        return []
    except Exception:  # noqa: BLE001 - tek bir sembolün geçmiş verisi başarısız olursa çağıran tarafı durdurmaz
        logger.warning("Fiyat geçmişi alınamadı: %s (interval=%s, range=%s)", symbol, interval, range_, exc_info=True)
        return []


async def _fetch_one(client: httpx.AsyncClient, symbol: str, label: str) -> dict[str, Any] | None:
    if _yahoo_in_cooldown():
        return None
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
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            _enter_yahoo_cooldown()
        logger.warning("Piyasa verisi alınamadı: %s", symbol, exc_info=True)
        return None
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
#
# GERÇEK RENDER TESTİYLE bulunan sorun (2026-07): sayfa render'ı SIRASINDA
# (asyncio.run ile, senkron route içinde) 9 farklı sembolü TAMAMEN eşzamanlı
# (asyncio.gather, sınırsız) çekmeye çalışmak, Render'ın kısıtlı 0.1 vCPU'su
# altında ~9/9'dan sadece ~1'inin 8 sn'lik timeout içinde tamamlanmasına yol
# açıyordu (CPU zamanlaması için 9 eşzamanlı isteğin birbiriyle yarışması).
# Çözüm İKİ parçalı:
#   1. Fiyat çekme işlemi artık sayfa render'ını HİÇ bloklamıyor - ayrı bir
#      `/api/ticker-quotes` endpoint'i üzerinden, sayfa tamamen yüklendikten
#      SONRA tarayıcıda arka planda (bkz. detayli_inceleme.html <script>)
#      ve yalnızca İLK birkaç (8) benzersiz ticker için çağrılıyor.
#   2. Bu modül içinde de bir eşzamanlılık sınırı (_TICKER_QUOTE_SEMAPHORE)
#      eklendi - Render'ın kısıtlı CPU'suna aynı anda en fazla 4 istek
#      gönderilir, geri kalanı sırayla işlenir. Bu, HER isteğin kendi
#      payına daha fazla gerçek CPU zamanı bulmasını sağlar.
# --------------------------------------------------------------------------

# Sayfa render'ını asla bloklamayacak şekilde artık ayrı bir arka plan
# çağrısı (bkz. yukarıdaki not) olduğundan, ana piyasa şeridinden (8.0 sn)
# biraz daha cömert bir timeout güvenle kullanılabilir - kullanıcı bu
# çağrının birkaç saniye sürmesini fark etmez (sayfa zaten yüklenmiş
# durumda), ama yine de sonsuza kadar asılı kalmayı önlemek için sınırlı.
_TICKER_QUOTE_HTTP_TIMEOUT = 12.0

# Render'ın kısıtlı CPU'su altında GERÇEK testte ölçülen kök neden: çok
# sayıda (9) eşzamanlı istek birbiriyle CPU zamanlaması için yarışıyordu.
# Bu semaphore, aynı anda en fazla bu kadar isteğin gerçekten "uçuşta"
# olmasını garanti eder - geri kalanı sırada bekler, bir öncekinin CPU
# payını çalmaz.
_TICKER_QUOTE_MAX_CONCURRENT = 4
_ticker_quote_semaphore = asyncio.Semaphore(_TICKER_QUOTE_MAX_CONCURRENT)

# Bu, artık HERKESE AÇIK bir endpoint'ten (bkz. src/web/app.py >
# /api/ticker-quotes) çağrıldığından, tek bir istekte kabul edilecek
# sembol sayısına sunucu tarafında da SERT bir üst sınır konur - frontend
# zaten en fazla 8 gönderiyor (bkz. detayli_inceleme.html), ama bu sunucu
# tarafı sınır kötü niyetli/hatalı bir çağrıya karşı bağımsız bir koruma.
_TICKER_QUOTE_MAX_SYMBOLS_PER_CALL = 10

# Yahoo'nun chart endpoint'ine gönderilecek sembol, path'e doğrudan
# gömüldüğünden (bkz. _CHART_URL.format) beklenmedik karakterler
# (boşluk, "/", vb.) içeren bir "sembol" sessizce reddedilir - geçerli
# semboller (bkz. MARKET_SYMBOLS) yalnızca harf/rakam/".", "^", "=", "-"
# içerir.
_VALID_YAHOO_SYMBOL_RE = re.compile(r"^[A-Z0-9.\^=-]{1,15}$")

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

# 30 sn -> 120 sn (2026-07-30, gerçek Render testiyle bulunan sorun):
# emtia raporundaki şirket kartları/company-detail modalı eklendikten
# sonra, ayNI şirket (ör. ADM hem Buğday hem Mısır'da geçiyor) birden
# fazla panelde/tıklamada tekrar tekrar sorgulanabiliyor - kısa TTL bunu
# her seferinde YENİ bir Yahoo isteği olarak tekrarlıyordu. GERÇEK testte
# doğrulandı: Render'ın IP'sinden art arda birçok FARKLI hisse sorgusu
# (18 şirket, ~1 dk içinde) sonrası Yahoo bu dinamik/anlık sorgu yolunu
# birkaç dakikalığına reddetmeye başladı (429 DEĞİL - sabit 12'lik piyasa
# şeridi o sırada SORUNSUZ çalışmaya devam etti, bu yüzden mevcut
# _yahoo_in_cooldown devre kesicisi bunu YAKALAMADI - farklı bir ret
# şekli); ~3 dk bekleyince kendiliğinden düzeldi. Kök neden LLM'in ürettiği
# ticker'ların GEÇERSİZLİĞİ DEĞİLDİ (en geçerli sembol olan AAPL bile aynı
# şekilde geçici olarak başarısız oldu). TTL'i 120 sn'ye çıkarmak, aynı
# şirketin tekrar tıklanmasında/birden fazla emtiada geçmesinde gereksiz
# tekrar istekleri azaltarak bu riski düşürür.
_TICKER_QUOTE_CACHE_TTL_SECONDS = 120.0
_ticker_quote_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _resolve_yahoo_symbol(company_ticker: str) -> str | None:
    """"NASDAQ: TSLA" -> "TSLA", "BIST: THYAO" -> "THYAO.IS" gibi. Beklenen
    "BORSA: SEMBOL" formatında değilse (ör. ":" yoksa) veya sonuç geçerli
    bir Yahoo sembolü şeklinde değilse (bkz. _VALID_YAHOO_SYMBOL_RE - artık
    HERKESE AÇIK bir endpoint'ten geldiğinden bu doğrulama önemli) None döner."""
    if not company_ticker or ":" not in company_ticker:
        return None
    exchange, _, symbol = company_ticker.partition(":")
    exchange = exchange.strip().upper()
    symbol = symbol.strip().upper()
    if not symbol:
        return None
    suffix = _EXCHANGE_YAHOO_SUFFIX.get(exchange, "")
    yahoo_symbol = f"{symbol}{suffix}"
    if not _VALID_YAHOO_SYMBOL_RE.match(yahoo_symbol):
        return None
    return yahoo_symbol


async def _fetch_one_limited(client: httpx.AsyncClient, symbol: str, label: str) -> dict[str, Any] | None:
    """`_fetch_one`'ı bir semaphore ile sarar - Render'ın kısıtlı CPU'su
    altında GERÇEK testte doğrulandığı üzere (bkz. modül başındaki not),
    çok sayıda eşzamanlı isteği aynı anda göndermek CPU zamanlaması
    yüzünden çoğunun timeout'a düşmesine yol açıyordu; bu, aynı anda
    en fazla _TICKER_QUOTE_MAX_CONCURRENT isteğin gerçekten "uçuşta"
    olmasını garanti eder."""
    async with _ticker_quote_semaphore:
        return await _fetch_one(client, symbol, label)


async def get_quotes_for_company_tickers(company_tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Verilen "BORSA: SEMBOL" string listesi için, çözülebilen ve Yahoo'dan
    gerçek veri alınabilenler için {orijinal_company_ticker: quote_dict} döner.
    Çözülemeyen/başarısız olanlar sonuçta YER ALMAZ (exception fırlatmaz).

    Sunucu tarafı sert sınır: en fazla _TICKER_QUOTE_MAX_SYMBOLS_PER_CALL
    (10) benzersiz sembol işlenir - fazlası sessizce yok sayılır (bkz.
    modül başındaki not, bu artık herkese açık bir endpoint'ten çağrılıyor)."""
    now = time.monotonic()
    to_fetch: dict[str, str] = {}  # yahoo_symbol -> orijinal company_ticker
    results: dict[str, dict[str, Any]] = {}

    unique_tickers = list(dict.fromkeys(company_tickers))[:_TICKER_QUOTE_MAX_SYMBOLS_PER_CALL]

    for ct in unique_tickers:
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
        async with httpx.AsyncClient(timeout=_TICKER_QUOTE_HTTP_TIMEOUT) as client:
            fetched = await asyncio.gather(*(_fetch_one_limited(client, sym, sym) for sym in to_fetch))
        for sym, quote in zip(to_fetch.keys(), fetched):
            _ticker_quote_cache[sym] = (now, quote)
            if quote is not None:
                results[to_fetch[sym]] = quote

    return results


async def validate_tickers_exist(company_tickers: list[str]) -> dict[str, dict[str, Any]]:
    """`get_quotes_for_company_tickers` İLE AYNI mantık (aynı önbellek/
    semaphore, sonuç şekli), ama `_TICKER_QUOTE_MAX_SYMBOLS_PER_CALL` (10)
    SUNUCU TARAFI sınırı YOK - bu, HERKESE AÇIK `/api/ticker-quotes`
    endpoint'ini kötüye kullanıma karşı koruyan bir önlemdi, ama bu
    fonksiyon dışarıdan HİÇ çağrılmıyor (bkz. src/commodity_report.py >
    _validate_companies_exist - haftada BİR, LLM'in ürettiği TÜM
    şirketleri TEK seferde doğrulamak için).

    ÖNEMLİ (2026-07-30, gerçek bir bug'la bulundu): çağıran taraf BİRDEN
    FAZLA `asyncio.run()` çağrısıyla (ör. 10'luk parçalar hâlinde)
    doğrulama yaparsa, modül düzeyindeki `_ticker_quote_semaphore` HER
    `asyncio.run()`'da YENİ bir event loop'a bağlanmaya çalışıp
    "bound to a different event loop" hatası fırlatıyordu - bu da bazı
    parçaların sessizce başarısız olup GEÇERLİ şirketlerin bile
    yanlışlıkla "geçersiz" sayılmasına yol açtı. Bu fonksiyon TÜM
    ticker'ları TEK bir `asyncio.gather` içinde (dolayısıyla TEK bir
    event loop'ta) işleyerek bu sorunu ortadan kaldırır - çağıran taraf
    kendi `asyncio.run()`'ını yalnızca BİR KEZ çağırmalıdır."""
    now = time.monotonic()
    to_fetch: dict[str, str] = {}
    results: dict[str, dict[str, Any]] = {}

    unique_tickers = list(dict.fromkeys(company_tickers))
    for ct in unique_tickers:
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
        async with httpx.AsyncClient(timeout=_TICKER_QUOTE_HTTP_TIMEOUT) as client:
            fetched = await asyncio.gather(*(_fetch_one_limited(client, sym, sym) for sym in to_fetch))
        for sym, quote in zip(to_fetch.keys(), fetched):
            _ticker_quote_cache[sym] = (now, quote)
            if quote is not None:
                results[to_fetch[sym]] = quote

    return results


# Emtia raporundaki tıklanabilir şirket kartları (bkz. dashboard.html >
# şirket detay modalı, /api/company-detail) için 1 aylık günlük fiyat
# geçmişi (sparkline) önbelleği - `_ticker_quote_cache` (30 sn) ile AYNI
# kaynaktan (Yahoo chart endpoint'i) geldiği için AYNI semaphore'u
# paylaşır, ama TTL'i çok daha uzun: 1 aylık günlük kapanış verisi gün
# içinde pratikte değişmez (yalnızca bugünün henüz kapanmamış barı
# güncellenir), bu yüzden fiyat kadar sık tazelenmesine gerek yok - Yahoo'ya
# gereksiz yük bindirmemek için 5 dakika seçildi.
_TICKER_HISTORY_CACHE_TTL_SECONDS = 300.0
_ticker_history_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_TICKER_HISTORY_RANGE = "1mo"
_TICKER_HISTORY_INTERVAL = "1d"


async def get_ticker_detail(company_ticker: str) -> dict[str, Any] | None:
    """Emtia raporundaki bir şirket kartına tıklanınca (bkz.
    /api/company-detail) gösterilecek TÜM veriyi tek çağrıda toplar: anlık
    fiyat/değişim (bkz. get_quotes_for_company_tickers - kendi 30 sn
    önbelleği/semaphore'u ile) + 1 aylık günlük fiyat geçmişi (sparkline
    için, kendi 5 dk önbelleğiyle, bkz. yukarıdaki not).

    "BORSA: SEMBOL" çözülemezse (geçersiz format/sembol) None döner - bu,
    çağıran tarafın (app.py) 404 dönmesi için kullanılır."""
    yahoo_symbol = _resolve_yahoo_symbol(company_ticker)
    if not yahoo_symbol:
        return None

    quotes = await get_quotes_for_company_tickers([company_ticker])
    quote = quotes.get(company_ticker)

    now = time.monotonic()
    cached = _ticker_history_cache.get(yahoo_symbol)
    if cached and (now - cached[0]) < _TICKER_HISTORY_CACHE_TTL_SECONDS:
        history = cached[1]
    else:
        async with _ticker_quote_semaphore:
            async with httpx.AsyncClient(timeout=_TICKER_QUOTE_HTTP_TIMEOUT) as client:
                history = await fetch_symbol_history(
                    client, yahoo_symbol, _TICKER_HISTORY_INTERVAL, _TICKER_HISTORY_RANGE
                )
        _ticker_history_cache[yahoo_symbol] = (now, history)

    if quote is None and not history:
        # Ne fiyat ne geçmiş alınabildiyse (ör. sembol Yahoo'da yok) - 404.
        return None

    return {"quote": quote, "history": history}


async def _refresh_market_data_cache() -> None:
    """Yahoo'dan GERÇEK veriyi çekip önbelleği yazar - hem arka plan görevi
    (bkz. _background_refresh_loop) hem de soğuk başlangıç fallback'i (bkz.
    get_market_snapshot) TEK bir kaynak-of-truth olarak bu fonksiyonu
    kullanır. Çağıran taraf `_refresh_lock`'u tutmakla YÜKÜMLÜDÜR - bu
    fonksiyonun kendisi kilit almaz (tekrar-giriş/deadlock riski olmasın).

    Tazeleme TAMAMEN başarısız olursa (ör. geçici bir ağ sorunu - TÜM
    sembollerin isteği başarısız olur) önbellekteki ESKİ ama GERÇEK veri
    SİLİNMEZ - kullanıcıya aniden boş/hata göstermek yerine biraz eski ama
    gerçek veri gösterilmeye devam edilir, bir sonraki tazeleme denemesi
    başarılı olursa güncellenir."""
    async with httpx.AsyncClient(timeout=8.0) as client:
        results = await asyncio.gather(*(_fetch_one(client, sym, label) for sym, label in MARKET_SYMBOLS))

    data = [r for r in results if r is not None]
    now = time.monotonic()

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


async def _background_refresh_loop() -> None:
    """Süreç boyunca sürekli çalışır: kullanıcı isteklerinden TAMAMEN
    BAĞIMSIZ olarak her _BACKGROUND_REFRESH_INTERVAL_SECONDS (12 sn) bir
    önbelleği proaktif olarak tazeler. Bu sayede `get_market_snapshot()`
    çağıran normal istekler HİÇBİR ZAMAN Yahoo'nun yanıt vermesini
    BEKLEMEZ - her zaman az önce yazılmış, hazır bir önbellekten okur.

    Tek bir Render worker'ında TEK bir bu döngü çalışır (bkz.
    start_background_refresh - idempotent) - render.yaml'da BİLİNÇLİ
    olarak tek worker kullanıldığından (bkz. render.yaml yorumu), N
    worker'da N kat Yahoo isteği riski burada da YOK."""
    while True:
        try:
            async with _refresh_lock:
                await _refresh_market_data_cache()
        except Exception:  # noqa: BLE001 - bir tur başarısız olursa döngü YİNE DE devam etsin
            logger.exception("Arka plan piyasa verisi tazeleme döngüsünde beklenmeyen hata.")
        await asyncio.sleep(_BACKGROUND_REFRESH_INTERVAL_SECONDS)


def start_background_refresh() -> None:
    """Arka plan tazeleme görevini başlatır - `api/index.py`'nin FastAPI
    `lifespan`'ından (uygulama başlarken TEK sefer) çağrılır. İdempotenttir:
    zaten çalışan bir görev varsa tekrar başlatmaz (ör. testlerde/olası bir
    yeniden çağrıda ikinci bir döngünün sessizce birikmesini önler)."""
    global _background_task
    if _background_task is not None and not _background_task.done():
        return
    _background_task = asyncio.create_task(_background_refresh_loop())


async def get_market_snapshot() -> list[dict[str, Any]]:
    """İzlenen tüm sembollerin güncel fiyat/günlük değişim yüzdesini,
    dashboard'daki gösterim sırasına göre döner. Başarısız olan semboller
    sessizce listeden çıkarılır (bkz. _fetch_one).

    NORMAL DURUMDA bu fonksiyon HİÇBİR AĞ İSTEĞİ YAPMAZ / HİÇBİR KİLİT
    BEKLEMEZ - sadece arka plan görevinin (bkz. _background_refresh_loop)
    az önce yazdığı hazır önbellekten okur. Tek istisna: SÜREÇ YENİ
    BAŞLADIYSA ve arka plan görevi henüz ilk tazelemesini yapmadıysa
    (`_cache["data"] is None`), o TEK seferlik soğuk başlangıç anında
    burada senkron bir fallback tazeleme yapılır - `_refresh_lock` sayesinde
    bu sırada birden fazla kullanıcı gelirse yine TEK bir Yahoo isteği
    grubu atılır (thundering herd koruması bu istisnai durumda da geçerli)."""
    if _cache["data"] is not None:
        return _cache["data"]

    async with _refresh_lock:
        # Kilit ALINANA KADAR başka bir eşzamanlı çağrı (veya arka plan
        # görevinin kendisi) önbelleği zaten doldurmuş olabilir - tekrar
        # kontrol et (double-checked locking).
        if _cache["data"] is not None:
            return _cache["data"]
        await _refresh_market_data_cache()

    return _cache["data"]
