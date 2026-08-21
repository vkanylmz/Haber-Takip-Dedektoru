"""Kripto para fiyat verisi çekim + önbellek katmanı (2026-08-21, kullanıcı
isteği: "Kripto Paralar" sekmesi) - src/commodities.py + src/commodity_report.py
İLE BİREBİR AYNI mimari desen, sadece LLM analizi/şirket eşlemesi YOK (kullanıcı
isteği: sadece fiyat kartı - ikon+isim+değişim+mini grafik).

`get_commodity_history`/`compute_period_change` (src/commodities.py) TAMAMEN
JENERIK - Yahoo sembolünün emtia mi kripto mu olduğuna bakmaz, aynı
`fetch_symbol_history` (src/web/market_data.py) altyapısını kullanır. Bu
yüzden burada AYRI bir HTTP/parsing mekanizması İCAT EDİLMEDİ, sadece sembol
listesi + TradingView eşlemesi tanımlanıp mevcut fonksiyonlar çağrılıyor.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from src.commodities import compute_period_change, get_commodity_history
from src.db import get_app_state, set_app_state

logger = logging.getLogger(__name__)

# (Yahoo sembolü, gösterim adı) - piyasa değerine göre top 10 (2026-08-21,
# kullanıcı kararı: stablecoin'ler - Tether/USDC - BİLİNÇLİ OLARAK hariç
# tutuldu, çünkü sabit $1 fiyatları/değişimsiz grafikleri dashboard kartı
# olarak bilgi değeri taşımıyor; onun yerine köklü/uzun süredir top-10'da
# kalan majör coin'ler kullanıldı - kullanıcının kendi önerdiği set).
CRYPTO_SYMBOLS: list[tuple[str, str]] = [
    ("BTC-USD", "Bitcoin"),
    ("ETH-USD", "Ethereum"),
    ("XRP-USD", "XRP"),
    ("BNB-USD", "BNB"),
    ("SOL-USD", "Solana"),
    ("DOGE-USD", "Dogecoin"),
    ("ADA-USD", "Cardano"),
    ("TRX-USD", "TRON"),
    ("LINK-USD", "Chainlink"),
    ("AVAX-USD", "Avalanche"),
]

_CRYPTO_EMOJI: dict[str, str] = {
    "BTC-USD": "₿",
    "ETH-USD": "Ξ",
    "XRP-USD": "✕",
    "BNB-USD": "🔶",
    "SOL-USD": "◎",
    "DOGE-USD": "🐕",
    "ADA-USD": "🔷",
    "TRX-USD": "🔺",
    "LINK-USD": "🔗",
    "AVAX-USD": "🔺",
}

# TradingView entegrasyonu - src/commodities.py > COMMODITY_TRADINGVIEW_SYMBOLS
# İLE AYNI desen (sabit kod-içi eşleme, LLM'e SORULMAZ - sembol sayısı sabit/
# az ve iyi bilindiğinden). Binance'in USDT paritesi kullanıldı (en yüksek
# hacimli/en yaygın TradingView kripto veri kaynağı) - her sembol GERÇEK bir
# tarayıcı testiyle doğrulandı (bkz. 2026-08-21 commit notu).
CRYPTO_TRADINGVIEW_SYMBOLS: dict[str, str] = {
    "BTC-USD": "BINANCE:BTCUSDT",
    "ETH-USD": "BINANCE:ETHUSDT",
    "XRP-USD": "BINANCE:XRPUSDT",
    "BNB-USD": "BINANCE:BNBUSDT",
    "SOL-USD": "BINANCE:SOLUSDT",
    "DOGE-USD": "BINANCE:DOGEUSDT",
    "ADA-USD": "BINANCE:ADAUSDT",
    "TRX-USD": "BINANCE:TRXUSDT",
    "LINK-USD": "BINANCE:LINKUSDT",
    "AVAX-USD": "BINANCE:AVAXUSDT",
}

_HISTORY_RANGE = "1mo"
_HISTORY_INTERVAL = "1d"
_WEEKLY_WINDOW_POINTS = 6

_DASHBOARD_CACHE_KEY = "crypto_snapshot"
# Emtia panosuyla AYNI aralık (bkz. src/commodity_report.py >
# _COMMODITY_REFRESH_INTERVAL_SECONDS'taki Yahoo istek hacmi notu) - tutarlı
# bir "canlılık" hissi + aynı gerekçeyle toplam Yahoo istek hacmini kontrollü
# tutar.
_CRYPTO_REFRESH_INTERVAL_SECONDS = 180.0


async def _fetch_all_histories() -> dict[str, list[dict[str, Any]]]:
    symbols = [sym for sym, _label in CRYPTO_SYMBOLS]
    histories = await asyncio.gather(
        *(get_commodity_history(sym, interval=_HISTORY_INTERVAL, range_=_HISTORY_RANGE) for sym in symbols)
    )
    return dict(zip(symbols, histories))


def build_crypto_dashboard_data() -> list[dict[str, Any]]:
    """Her kripto para için {symbol, label, emoji, last_close, abs_change,
    pct_change, history, trading_view_symbol} sözlüğü döner - LLM analizi
    YOK (bkz. modül docstring'i, kullanıcı isteği sadece fiyat kartı).

    Bir kripto paranın verisi hiç çekilemezse (geçici ağ hatası) sonuç
    listesinde YER ALMAZ - src/commodity_report.py > build_weekly_commodity_report_data
    İLE AYNI hata izolasyonu deseni."""
    histories = asyncio.run(_fetch_all_histories())

    results: list[dict[str, Any]] = []
    for symbol, label in CRYPTO_SYMBOLS:
        history = histories.get(symbol) or []
        if len(history) < 2:
            logger.warning("Kripto geçmişi yetersiz, panodan atlanıyor: %s (%s)", label, symbol)
            continue

        weekly_window = history[-_WEEKLY_WINDOW_POINTS:]
        change = compute_period_change(weekly_window)
        if change is None:
            logger.warning("Kripto haftalık değişimi hesaplanamadı, panodan atlanıyor: %s (%s)", label, symbol)
            continue

        results.append(
            {
                "symbol": symbol,
                "label": label,
                "emoji": _CRYPTO_EMOJI.get(symbol, "🪙"),
                "last_close": change["last_close"],
                "abs_change": change["abs_change"],
                "pct_change": change["pct_change"],
                "history": history,
                "trading_view_symbol": CRYPTO_TRADINGVIEW_SYMBOLS.get(symbol),
            }
        )

    return results


def _refresh_crypto_dashboard_cache() -> None:
    try:
        new_data = build_crypto_dashboard_data()
        if not new_data:
            return
        set_app_state(
            _DASHBOARD_CACHE_KEY,
            {"generated_at": datetime.now(timezone.utc).isoformat(), "cryptos": new_data},
        )
    except Exception:  # noqa: BLE001 - arka plan döngüsü asla ölmesin
        logger.exception("Arka plan kripto verisi tazeleme sırasında hata.")


_crypto_background_thread: threading.Thread | None = None
_crypto_background_stop = threading.Event()


def _crypto_background_loop() -> None:
    while not _crypto_background_stop.is_set():
        _refresh_crypto_dashboard_cache()
        _crypto_background_stop.wait(_CRYPTO_REFRESH_INTERVAL_SECONDS)


def start_crypto_background_refresh() -> None:
    global _crypto_background_thread
    if _crypto_background_thread is not None and _crypto_background_thread.is_alive():
        return
    _crypto_background_thread = threading.Thread(
        target=_crypto_background_loop, daemon=True, name="crypto-refresh"
    )
    _crypto_background_thread.start()


def get_crypto_dashboard_data() -> dict[str, Any]:
    """Dashboard paneli (bkz. src/web/app.py > /api/crypto-data) için veri -
    src/commodity_report.py > get_commodity_dashboard_data İLE AYNI öncelik
    deseni: önbellek varsa doğrudan o döner, yoksa (ör. arka plan döngüsü
    henüz ilk turunu tamamlamadıysa) bir kerelik canlı hesaplama yapılıp
    önbelleğe yazılır."""
    cached = get_app_state(_DASHBOARD_CACHE_KEY)
    if cached is not None and cached.get("cryptos"):
        return cached

    data = build_crypto_dashboard_data()
    result = {"generated_at": datetime.now(timezone.utc).isoformat() if data else None, "cryptos": data}
    set_app_state(_DASHBOARD_CACHE_KEY, result)
    return result
