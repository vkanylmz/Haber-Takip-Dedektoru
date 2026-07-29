"""Haftalık Emtia Raporu (Faz 2, bkz. src/commodities.py > Faz 1 "veri
kaynağı" üzerine kurulur): her izlenen emtia için geçen haftaki fiyat
değişimini hesaplar, LLM'e (Summarizer.analyze_commodity_weekly) o
değişimin YALNIZCA ABD borsası şirket/sektörlerini nasıl etkileyebileceğini
sorar, ve sonucu hem Telegram mesajı hem dashboard paneli için
kullanılabilir tek bir veri yapısına dönüştürür.

`src/trend_report.py` (haftalık trend raporu) ile AYNI mimari desen:
tek bir "veri hesapla" fonksiyonu (`build_weekly_commodity_report_data`)
HEM Telegram gönderimi HEM dashboard API'si tarafından kullanılır - ikisi
arasında tutarsızlık olmaz.

BIST bu raporda YER ALMAZ (kullanıcı gereksinimi: haftalık rapor SADECE ABD
borsası bağlamında yorumlanır; BIST etkisi ayrı, ani-hareket bildiriminde
ele alınacak - bkz. Faz 3).
"""

from __future__ import annotations

import asyncio
import html
import logging
import threading
import time
from typing import Any

from datetime import datetime, timezone

from src.commodities import COMMODITY_SYMBOLS, compute_period_change, get_commodity_history
from src.config import get_summarizer_api_key
from src.db import get_all_subscriber_chat_ids, get_app_state, set_app_state
from src.notifier import send_telegram_message_to_chat_ids
from src.summarizer import Summarizer
from src.telegram_format import chunk_messages
from src.web.market_data import get_quotes_for_company_tickers

logger = logging.getLogger(__name__)

# Dashboard panelinin okuduğu, son hesaplanan (LLM analizi DAHİL) haftalık
# emtia raporunun önbellek anahtarı (bkz. AppState modeli, src/db.py).
_DASHBOARD_CACHE_KEY = "commodity_weekly_report"

# Emtia türüne göre mesaj/panelde kullanılacak emoji - salt görsel, işlevsel
# bir etkisi yok.
_COMMODITY_EMOJI: dict[str, str] = {
    "HG=F": "🔶",
    "ZW=F": "🌾",
    "CL=F": "🛢️",
    "BZ=F": "🛢️",
    "GC=F": "🥇",
    "SI=F": "🥈",
    "NG=F": "🔥",
    "ALI=F": "⚙️",
    "ZC=F": "🌽",
}

# Geçen haftaki değişimi hesaplamak için: 1 aylık günlük geçmiş çekilip son
# ~6 işlem gününe (yaklaşık 1 hafta - hafta sonu/tatil boşluklarına karşı
# güvenlik payı) sınırlanır. "1mo" aralığı, "5d" gibi dar bir aralığın
# olası bir tatil/kapalı gün yüzünden yetersiz nokta döndürme riskini
# azaltır (bkz. src/commodities.py > get_commodity_history).
_HISTORY_RANGE = "1mo"
_HISTORY_INTERVAL = "1d"
_WEEKLY_WINDOW_POINTS = 6


async def _fetch_all_histories() -> dict[str, list[dict[str, Any]]]:
    """Tüm izlenen emtiaların 1 aylık günlük geçmişini PARALEL olarak çeker
    (bkz. src/commodities.py > get_commodity_history - her çağrı kendi
    httpx.AsyncClient'ını açtığından burada asyncio.gather ile paralelleştirmek
    9 ayrı sıralı isteği tek tek beklemekten çok daha hızlıdır)."""
    symbols = [sym for sym, _label, _unit in COMMODITY_SYMBOLS]
    histories = await asyncio.gather(
        *(get_commodity_history(sym, interval=_HISTORY_INTERVAL, range_=_HISTORY_RANGE) for sym in symbols)
    )
    return dict(zip(symbols, histories))


def build_weekly_commodity_report_data(summarizer: Summarizer | None = None) -> list[dict[str, Any]]:
    """Her emtia için {symbol, label, unit, emoji, first_close, last_close,
    abs_change, pct_change, history, analysis, companies} sözlüğü döner.
    `history` (sparkline/trend grafiği için) tüm 1 aylık günlük noktaları
    içerir; değişim yüzdesi ise yalnızca SON `_WEEKLY_WINDOW_POINTS` noktadan
    (yaklaşık 1 hafta) hesaplanır. `companies` (bkz. Summarizer.
    analyze_commodity_weekly), analiz metninde bahsedilen şirketlerin
    yapılandırılmış [{"name","ticker","exchange"}] listesidir - dashboard/
    Telegram bunu kullanarak metni ayrıştırmadan güncel fiyat gösterir.

    `summarizer` verilmezse (ör. sadece veri/grafik lazım, LLM analizi
    gerekmiyorsa - bkz. dashboard API'si) LLM çağrısı YAPILMAZ, `analysis`
    alanı boş string kalır - bu, dashboard panelinin her AJAX isteğinde
    gereksiz yere 9 ayrı LLM çağrısı yapmasını (ve maliyet/gecikmeyi)
    önler. Telegram raporu (bkz. send_weekly_commodity_report) HER ZAMAN
    bir summarizer geçer.

    Bir emtianın verisi hiç çekilemezse (ör. geçici ağ hatası) sonuç
    listesinde YER ALMAZ - tüm raporun durmasına yol açmaz."""
    histories = asyncio.run(_fetch_all_histories())

    results: list[dict[str, Any]] = []
    for symbol, label, unit in COMMODITY_SYMBOLS:
        history = histories.get(symbol) or []
        if len(history) < 2:
            logger.warning("Emtia geçmişi yetersiz, rapordan atlanıyor: %s (%s)", label, symbol)
            continue

        weekly_window = history[-_WEEKLY_WINDOW_POINTS:]
        change = compute_period_change(weekly_window)
        if change is None:
            logger.warning("Emtia haftalık değişimi hesaplanamadı, rapordan atlanıyor: %s (%s)", label, symbol)
            continue

        analysis = ""
        companies: list[dict[str, str]] = []
        if summarizer is not None:
            analysis, companies = summarizer.analyze_commodity_weekly(
                label, change["pct_change"], change["abs_change"], unit
            )

        results.append(
            {
                "symbol": symbol,
                "label": label,
                "unit": unit,
                "emoji": _COMMODITY_EMOJI.get(symbol, "📦"),
                "first_close": change["first_close"],
                "last_close": change["last_close"],
                "abs_change": change["abs_change"],
                "pct_change": change["pct_change"],
                "history": history,
                "analysis": analysis,
                "companies": companies,
            }
        )

    # Ticker doğrulama (2026-07-30, GERÇEK bir hatayla bulundu): LLM
    # "GERÇEK/var olan şirket" talimatına rağmen bazen ESKİ/artık geçersiz
    # bir ticker üretebiliyor - GERÇEK bir örnekte "Chesapeake Energy"
    # için "CHK" üretti, ama bu şirket 2024'te Southwestern Energy ile
    # birleşip "Expand Energy" (ticker EXE) oldu; Yahoo'da "CHK" artık HİÇ
    # veri döndürmüyor (hem yerelde hem Render'da doğrulandı - rate-limit
    # DEĞİL, sembolün kendisi geçersiz). Bu YALNIZCA `summarizer is not
    # None` iken (haftada bir, gerçek LLM raporu üretilirken) çalışır -
    # sık çalışan arka plan fiyat tazelemesinde (summarizer=None) şirket
    # listesi zaten ESKİ önbellekten KORUNUYOR, tekrar doğrulanmaz.
    if summarizer is not None:
        _validate_companies_exist(results)

    return results


def _validate_companies_exist(results: list[dict[str, Any]]) -> None:
    """`results` içindeki her emtianın `companies` listesini YERİNDE
    (in-place) günceller: Yahoo Finance'te GERÇEKTEN veri dönmeyen (ör.
    şirket birleşmiş/adı değişmiş, LLM eski bir ticker üretmiş) şirketleri
    listeden ÇIKARIR - kullanıcı tıkladığında "veri alınamadı" ile
    karşılaşmasın diye baştan filtrelenir.

    GÜVENLİ TARAF: doğrulama çağrısının KENDİSİ boş dönerse (ör. Yahoo o an
    TÜMÜYLE geçici bir rate-limit yaşıyorsa - bkz. get_ticker_detail'deki
    GERÇEK Render testinde gözlemlenen geçici engelleme) HİÇBİR şirket
    silinmez - yanlışlıkla TÜM şirketleri "geçersiz" sayıp gizlemek, tek
    bir gerçekten geçersiz ticker'ı gözden kaçırmaktan çok daha kötü bir
    kullanıcı deneyimi olurdu."""
    all_tickers: list[str] = []
    for item in results:
        for c in item.get("companies", []):
            ticker_str = _company_ticker_string(c)
            if ticker_str not in all_tickers:
                all_tickers.append(ticker_str)

    if not all_tickers:
        return

    # Herkese açık /api/ticker-quotes ile AYNI sunucu-tarafı sınırı
    # (_TICKER_QUOTE_MAX_SYMBOLS_PER_CALL=10) burada da geçerli olduğundan,
    # TEK bir çağrıda 10'dan fazla sembol göndermek fazlasını sessizce
    # (doğrulanmamış -> yanlışlıkla "geçersiz" sayılmış gibi) keserdi. Bu
    # yüzden 10'luk parçalar hâlinde AYRI çağrılar yapılır.
    chunk_size = 10
    valid_tickers: set[str] = set()
    any_chunk_succeeded = False
    for i in range(0, len(all_tickers), chunk_size):
        chunk = all_tickers[i : i + chunk_size]
        try:
            quotes = asyncio.run(get_quotes_for_company_tickers(chunk))
        except Exception:  # noqa: BLE001 - doğrulama başarısız olursa bu parçayı atla, şirketleri SİLME
            logger.exception("Şirket ticker doğrulaması sırasında hata (parça): %s", chunk)
            continue
        if quotes:
            any_chunk_succeeded = True
            valid_tickers.update(quotes.keys())

    if not any_chunk_succeeded:
        # Doğrulama servisi bu turda TÜMÜYLE yanıt vermedi (ör. geçici
        # rate-limit) - hiçbir şirketi silme, bir sonraki haftaya (veya
        # kullanıcı denemesine) bırak.
        logger.warning("Şirket ticker doğrulaması TÜMÜYLE başarısız oldu - bu turda hiçbir şirket filtrelenmiyor.")
        return

    for item in results:
        companies = item.get("companies", [])
        if not companies:
            continue
        kept = [c for c in companies if _company_ticker_string(c) in valid_tickers]
        removed = [c for c in companies if _company_ticker_string(c) not in valid_tickers]
        if removed:
            logger.warning(
                "Geçersiz/artık var olmayan ticker(lar) rapordan çıkarıldı (%s): %s",
                item.get("label", "?"),
                [f"{c['name']} ({_company_ticker_string(c)})" for c in removed],
            )
        item["companies"] = kept


def _company_ticker_string(company: dict[str, str]) -> str:
    """{"ticker": "FCX", "exchange": "NYSE", ...} -> "NYSE: FCX" (bkz.
    src/web/market_data.py > _resolve_yahoo_symbol'ün beklediği "BORSA:
    SEMBOL" formatı - src/summarizer.py > company_ticker ile AYNI)."""
    return f"{company['exchange']}: {company['ticker']}"


def _fetch_company_quotes(data: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Rapordaki TÜM emtiaların TÜM şirketleri için, TEK bir toplu çağrıda
    (bkz. get_quotes_for_company_tickers - kendi 30 sn'lik önbelleği/
    eşzamanlılık sınırı VAR, burada tekrarlanmıyor) anlık fiyat/değişim
    çeker. Ağ hatası/boş liste durumunda boş sözlük döner (Telegram mesajı
    fiyat eki OLMADAN, sadece LLM analiziyle gönderilmeye devam eder -
    bkz. format_commodity_weekly_messages)."""
    tickers = [
        _company_ticker_string(c)
        for item in data
        for c in item.get("companies", [])
    ]
    if not tickers:
        return {}
    try:
        return asyncio.run(get_quotes_for_company_tickers(tickers))
    except Exception:  # noqa: BLE001 - fiyat eki opsiyonel bir bonus, raporun gönderimini durdurmasın
        logger.exception("Haftalık Emtia Raporu için şirket fiyatları çekilirken hata oluştu.")
        return {}


def format_commodity_weekly_messages(
    data: list[dict[str, Any]], quotes: dict[str, dict[str, Any]] | None = None
) -> list[str]:
    """9 emtianın TAMAMI (her biri kendi LLM analiziyle) tek bir Telegram
    mesajına SIĞMAYABİLİR (bkz. src/telegram_format.py > TELEGRAM_MESSAGE_LIMIT
    = 4096 karakter - GERÇEK bir test çalıştırmasında 9/9 emtia + analizle
    mesaj 4765 karaktere ulaştı ve gönderim "Message is too long" hatasıyla
    TAMAMEN BAŞARISIZ oldu, bkz. sohbet). Bu yüzden `daily_digest.py`'nin
    kullandığı AYNI `chunk_messages` yardımcı fonksiyonu kullanılarak, gerekli
    olduğunda birden fazla mesaja bölünür - her mesaj bağımsız olarak
    gönderilir (bkz. send_weekly_commodity_report).

    `quotes` verilirse (bkz. _fetch_company_quotes - "BORSA: SEMBOL" ->
    fiyat sözlüğü), her emtia analizinde bahsedilen şirketler için ayrı bir
    "İlgili şirketler" satırı eklenir (ör. "Freeport-McMoRan (FCX): $42.10
    (+1.2%)"). LLM'in serbest metnindeki isim biçimiyle bire bir eşleşmesi
    GARANTİ olmadığından, fiyat metne "ameliyat" edilerek gömülmez - AYRI,
    kendi başına doğru bir satır olarak eklenir (bkz. kullanıcıyla yapılan
    tasarım tartışması). Bir şirketin fiyatı çözülemezse (sembol geçersiz/
    Yahoo'dan veri alınamadı) o şirket sessizce fiyatsız (sadece isim/ticker)
    gösterilir."""
    header = "<b>🛒 Haftalık Emtia Raporu</b>"

    if not data:
        return [f"{header}\n\nBu hafta hiçbir emtia için veri hesaplanamadı."]

    quotes = quotes or {}
    blocks: list[str] = []
    for item in data:
        arrow = "📈" if item["pct_change"] >= 0 else "📉"
        sign = "+" if item["pct_change"] >= 0 else ""
        block = (
            f"{item['emoji']} <b>{html.escape(item['label'])}</b>: "
            f"{item['last_close']} {html.escape(item['unit'])} "
            f"({arrow} {sign}{item['pct_change']}%, {sign}{item['abs_change']} haftalık)"
        )
        if item["analysis"]:
            block += f"\n{html.escape(item['analysis'])}"

        companies = item.get("companies", [])
        if companies:
            company_bits = []
            for c in companies:
                quote = quotes.get(_company_ticker_string(c))
                label_bit = f"{html.escape(c['name'])} ({html.escape(c['ticker'])})"
                if quote:
                    q_sign = "+" if quote["change_pct"] >= 0 else ""
                    label_bit += f": {quote['price']} {html.escape(quote['currency'])} ({q_sign}{quote['change_pct']}%)"
                company_bits.append(label_bit)
            block += "\n<i>İlgili şirketler:</i> " + ", ".join(company_bits)

        blocks.append(block)

    return chunk_messages(header, blocks)


def send_weekly_commodity_report(config: dict[str, Any], chat_ids: list[str] | None = None) -> None:
    """Haftalık emtia raporunu hazırlar ve verilen `chat_ids`'e (verilmezse
    TÜM abonelere - bkz. get_all_subscriber_chat_ids) gönderir. Herhangi bir
    hata durumunda exception fırlatmaz (bkz. src/trend_report.py ile AYNI
    izolasyon deseni - worker.py'deki cron job sadece loglar).

    `chat_ids` parametresi KASITLI olarak eklendi: gerçek toplu gönderimden
    ÖNCE, yalnızca admin/test chat_id'sine göndererek doğrulama yapılabilsin
    diye (bkz. kullanıcı testi - gerçek abonelere test verisi gitmemeli)."""
    try:
        telegram_cfg = config.get("telegram", {})
        if not telegram_cfg.get("enabled", True):
            logger.info("config.yaml > telegram.enabled=false, Haftalık Emtia Raporu atlanıyor.")
            return

        target_chat_ids = chat_ids if chat_ids is not None else get_all_subscriber_chat_ids()
        if not target_chat_ids:
            logger.info("Hiç Telegram abonesi yok, Haftalık Emtia Raporu atlanıyor.")
            return

        summarizer_cfg = config.get("summarizer", {})
        provider, api_key = get_summarizer_api_key(summarizer_cfg)
        output_dir = config.get("app", {}).get("output_dir", "data")
        summarizer = Summarizer(summarizer_cfg, api_key=api_key, provider=provider, output_dir=output_dir)

        data = build_weekly_commodity_report_data(summarizer)
        quotes = _fetch_company_quotes(data)
        messages = format_commodity_weekly_messages(data, quotes)

        # Dashboard panelinin AYNI veriyi (LLM analizi/şirket isimleri DAHİL)
        # gösterebilmesi için önbelleğe yazılır - panel bu sayede kendi
        # AJAX isteğinde TEKRAR 9 ayrı LLM çağrısı yapmak zorunda kalmaz
        # (bkz. get_commodity_dashboard_data).
        set_app_state(
            _DASHBOARD_CACHE_KEY,
            {"generated_at": datetime.now(timezone.utc).isoformat(), "commodities": data},
        )

        # Rapor birden fazla mesaja bölünmüş olabilir (bkz.
        # format_commodity_weekly_messages) - her biri AYRI bir Telegram
        # API çağrısıyla gönderilir.
        sent_counts = [send_telegram_message_to_chat_ids(target_chat_ids, msg) for msg in messages]
        logger.info(
            "Haftalık Emtia Raporu gönderildi: %d mesaj parçası, alıcı başına gönderim sayıları=%s (toplam abone=%d).",
            len(messages), sent_counts, len(target_chat_ids),
        )
    except Exception:  # noqa: BLE001 - haftalık emtia raporu asla tüm uygulamayı etkilemesin
        logger.exception("Haftalık Emtia Raporu gönderilirken beklenmeyen bir hata oluştu.")


def _refresh_commodity_dashboard_cache() -> None:
    """Her `_COMMODITY_REFRESH_INTERVAL_SECONDS` (90 sn) fiyat/sparkline'ı
    TAZE Yahoo verisiyle günceller - LLM analizi/şirket listesi (pahalı,
    haftada bir üretilir) ESKİ önbellekten KORUNUR, yeniden üretilmez.

    ÖNEMLİ DÜZELTME (2026-07-30, kullanıcı geri bildirimi): `generated_at`
    ÖNCEDEN buradaki her tazelemede ESKİ (Telegram raporunun yazdığı)
    değere sabitleniyordu - bu, dashboard panelindeki "Son güncelleme"
    yazısının GERÇEKTE her 90 sn'de bir tazelenen fiyatı YANSITMAMASINA,
    kullanıcıya panelin donduğu izlenimi vermesine yol açıyordu (gerçek
    testte doğrulandı: fiyatlar sessizce güncelleniyordu ama zaman damgası
    hiç değişmiyordu). Artık HER başarılı tazelemede `datetime.now()` ile
    güncelleniyor - "Son güncelleme" artık GERÇEKTEN o an gösterilen
    fiyat/sparkline verisinin ne zaman çekildiğini yansıtıyor (LLM analizi
    kendi içinde hâlâ haftalık kalıyor, sadece zaman damgasının anlamı artık
    "fiyat tazeliği" - bu, kullanıcının "fiyat/yüzde canlı olsun" isteğiyle
    tutarlı)."""
    try:
        new_data = build_weekly_commodity_report_data(summarizer=None)
        if not new_data:
            return

        old_cache = get_app_state(_DASHBOARD_CACHE_KEY)
        old_commodities = {}
        if old_cache and old_cache.get("commodities"):
            for item in old_cache["commodities"]:
                old_commodities[item["symbol"]] = item

        for item in new_data:
            old_item = old_commodities.get(item["symbol"])
            if old_item:
                item["analysis"] = old_item.get("analysis", "")
                item["companies"] = old_item.get("companies", [])

        set_app_state(
            _DASHBOARD_CACHE_KEY,
            {"generated_at": datetime.now(timezone.utc).isoformat(), "commodities": new_data},
        )
    except Exception:
        logger.exception("Arka plan emtia verisi tazeleme de hata.")

# 90 sn -> 180 sn (2026-07-30): toplam Yahoo Finance istek hacmini azaltmak
# için (bkz. src/web/market_data.py > _TICKER_QUOTE_CACHE_TTL_SECONDS'taki
# AYNI tarihli not - company-detail/ticker-quotes tarafında geçici bir
# rate-limit sorunu gözlemlendi). Bu panel 9 sembolün TAMAMININ 1 aylık
# geçmişini (9 ayrı istek) her turda çekiyor - 90 sn'de bir 9 istek = günde
# ~8.640, 180 sn'de bir ise ~4.320'ye iner. Piyasa şeridiyle (12 sembol,
# 90 sn) BİRLİKTE toplam günlük hacim ~20.160'tan ~15.840'a düşer. Emtia
# fiyatları için 3 dakikalık bir tazelik hâlâ "canlı" hissettirir (haftalık
# pencereli bir analiz için saniyeler önemli değil), bu yüzden gözle
# görülür bir his kaybı olmadan hacmi azaltır.
_COMMODITY_REFRESH_INTERVAL_SECONDS = 180.0
_commodity_background_thread: threading.Thread | None = None
_commodity_background_stop = threading.Event()

def _commodity_background_loop() -> None:
    while not _commodity_background_stop.is_set():
        _refresh_commodity_dashboard_cache()
        _commodity_background_stop.wait(_COMMODITY_REFRESH_INTERVAL_SECONDS)

def start_commodity_background_refresh() -> None:
    global _commodity_background_thread
    if _commodity_background_thread is not None and _commodity_background_thread.is_alive():
        return
    _commodity_background_thread = threading.Thread(
        target=_commodity_background_loop, daemon=True, name="commodity-refresh"
    )
    _commodity_background_thread.start()

def get_commodity_dashboard_data() -> dict[str, Any]:
    """Dashboard paneli (bkz. src/web/app.py > /api/commodity-weekly-report)
    için veri. Öncelik SIRASI:
      1. Önbellekte (AppState, en son Telegram raporuyla BİRLİKTE yazılmış -
         bkz. send_weekly_commodity_report) LLM analizi DAHİL bir sonuç
         varsa, DOĞRUDAN o döner - ek bir LLM çağrısı YAPILMAZ.
      2. Önbellek hiç yoksa (ör. ilk Pazartesi raporu henüz hiç
         çalışmamışsa), fiyat/sparkline'ı boş bırakmamak için LLM
         analizi OLMADAN (summarizer=None) bir kerelik canlı hesaplama
         yapılır - kullanıcı en azından fiyat/grafiği görür, "şirket
         etkisi" metni ise bir sonraki Pazartesi raporuyla eklenir."""
    cached = get_app_state(_DASHBOARD_CACHE_KEY)
    if cached is not None and cached.get("commodities"):
        return cached

    data = build_weekly_commodity_report_data(summarizer=None)
    set_app_state(_DASHBOARD_CACHE_KEY, {"generated_at": None, "commodities": data})
    return {"generated_at": None, "commodities": data}

