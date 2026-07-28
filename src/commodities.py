"""Emtia (bakır, buğday, petrol, altın, gümüş, doğalgaz, alüminyum, mısır)
fiyat verisi çekim katmanı - src/web/market_data.py'deki AYNI Yahoo Finance
chart endpoint altyapısını (aynı URL, aynı header, aynı `_fetch_one`/
`fetch_symbol_history` fonksiyonları) kullanır, ayrı bir HTTP/parsing
mekanizması İCAT ETMEZ.

Bu modül YALNIZCA veri çekme sorumluluğunu taşır (Faz 1). Haftalık emtia
raporu (Faz 2, LLM sektör/şirket etki analizi + Telegram/dashboard) ve ani
fiyat hareketi bildirimi (Faz 3) kullanıcı onayıyla AYRI modüllerde,
sırayla eklenecek - bu dosya o iki fazın da üzerine kuracağı ortak temel.

Tüm 9 sembol GERÇEK bir istekle tek tek doğrulandı (2026-07): anlık
snapshot, 1 aylık günlük geçmiş ve 6 aylık haftalık geçmiş - hepsi 9/9
başarılı. Buğday ve Mısır Yahoo'da "USX" (ABD senti, dolar DEĞİL) cinsinden
geliyor - `unit` alanı bunu doğru göstermek için var, fiyat DÖNÜŞTÜRÜLMEDİ
(Yahoo'nun ham değeri aynen kullanılıyor, sadece etiketleniyor).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from src.web.market_data import _fetch_one, fetch_symbol_history

logger = logging.getLogger(__name__)

# (Yahoo sembolü, gösterim adı, birim) - sıra, rapor/dashboard'daki gösterim
# sırasıdır. WTI ve Brent AYRI izlenir (kullanıcı gereksinimi "Ham Petrol
# (WTI/Brent)" - ikisi de takip edilsin diye yorumlandı). Altın/Brent zaten
# src/web/market_data.py > MARKET_SYMBOLS'ta (üst şeritte) ayrıca var - bu,
# KASITLI bir tekrar: o listenin amacı "anlık şerit", buradaki liste ise
# "haftalık geçmiş + emtia raporu" - ikisi farklı önbellek/yaşam döngüsüne
# sahip, birbirine bağımlı değil.
COMMODITY_SYMBOLS: list[tuple[str, str, str]] = [
    ("HG=F", "Bakır", "USD/lb"),
    ("ZW=F", "Buğday", "USX/bu"),
    ("CL=F", "Ham Petrol (WTI)", "USD/varil"),
    ("BZ=F", "Brent Petrol", "USD/varil"),
    ("GC=F", "Altın", "USD/ons"),
    ("SI=F", "Gümüş", "USD/ons"),
    ("NG=F", "Doğalgaz", "USD/MMBtu"),
    ("ALI=F", "Alüminyum", "USD/ton"),
    ("ZC=F", "Mısır", "USX/bu"),
]

_COMMODITY_UNIT_BY_SYMBOL: dict[str, str] = {sym: unit for sym, _label, unit in COMMODITY_SYMBOLS}


async def get_commodity_snapshot() -> list[dict[str, Any]]:
    """Tüm izlenen emtiaların GÜNCEL anlık fiyatını/günlük değişim yüzdesini
    döner (bkz. src/web/market_data.py > `_fetch_one` - AYNI mantık).
    Kasıtlı olarak KENDİ önbelleği YOK - bu fonksiyon ana piyasa şeridi
    kadar sık çağrılmayacak (haftalık rapor haftada bir, ani hareket
    kontrolü 15-30 dk'da bir - bkz. Faz 2/3), bu yüzden ayrı bir
    background-refresh döngüsü gereksiz karmaşıklık olurdu. Başarısız olan
    semboller sessizce sonuç listesinden çıkarılır (exception fırlatmaz)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        results = await asyncio.gather(
            *(_fetch_one(client, sym, label) for sym, label, _unit in COMMODITY_SYMBOLS)
        )
    snapshot: list[dict[str, Any]] = []
    for item in results:
        if item is None:
            continue
        item["unit"] = _COMMODITY_UNIT_BY_SYMBOL.get(item["symbol"], "")
        snapshot.append(item)
    return snapshot


async def get_commodity_history(symbol: str, interval: str = "1d", range_: str = "1mo") -> list[dict[str, Any]]:
    """Tek bir emtia için fiyat geçmişini döner (bkz.
    src/web/market_data.py > fetch_symbol_history). Haftalık rapordaki
    trend grafiği VE "geçen haftaki değişim" hesabı bunu kullanır."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await fetch_symbol_history(client, symbol, interval=interval, range_=range_)


def compute_period_change(history: list[dict[str, Any]]) -> dict[str, float] | None:
    """Bir geçmiş fiyat listesinin İLK ve SON noktası arasındaki mutlak ve
    yüzdesel değişimi hesaplar (ör. "geçen haftaki değişim" için `range_`
    zaten o döneme sınırlandırılmış bir `history` verilir - bu fonksiyon
    dönem SEÇİMİ yapmaz, sadece verilen listenin uç noktalarını kıyaslar).
    En az 2 nokta yoksa (veri çekilememişse) None döner."""
    if len(history) < 2:
        return None
    first_close = history[0]["close"]
    last_close = history[-1]["close"]
    if not first_close:
        return None
    abs_change = last_close - first_close
    pct_change = (abs_change / first_close) * 100.0
    return {
        "first_close": round(first_close, 4),
        "last_close": round(last_close, 4),
        "abs_change": round(abs_change, 4),
        "pct_change": round(pct_change, 2),
    }
