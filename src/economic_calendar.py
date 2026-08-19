"""Ekonomik takvim (2026-08-18, kullanıcı isteği): "Yaklaşan Ekonomik Takvim"
kutusu için tradingeconomics.com/calendar üzerinden veri çeker.

Neden Investing.com DEĞİL: Investing.com robots.txt'in ÖTESİNDE tam bir
Cloudflare bot-koruması (JS challenge) kullanıyor - bunu aşmak bot-tespiti
aşma anlamına gelirdi, KESİNLİKLE yapılmadı (bkz. sohbet). Trading Economics
ARAŞTIRILDI: robots.txt'te hiçbir Disallow kuralı yok, Cloudflare/JS
challenge YOK (düz httpx GET ile veri geliyor), tablo satırları
`data-country`/`data-event`/`data-id` gibi temiz `data-*` attribute'lerle
zaten yapılandırılmış - Playwright'e GEREK YOK.

Önem seviyesi (yıldız) TESPİTİ: sayfa satır başına doğrudan bir yıldız
sayısı VERMİYOR - bunun yerine `?importance=N` query param'ı "N VE ÜZERİ
yıldız" filtrelemesi yapıyor (GERÇEK istekle doğrulandı: importance=1/2/3
sırasıyla 462/154/26 satır döndürdü, monoton azalan). Bu yüzden TAM yıldız
seviyesini öğrenmek için 3 AYRI istek atılıp (importance=1, 2, 3) küme
farkıyla (bkz. _fetch_all_tiers) her olayın GERÇEK yıldız sayısı hesaplanır.

Zaman dilimi: sayfanın varsayılan (çerezsiz/anonim istek) saat dilimi UTC
(bkz. `<option selected value="0">UTC</option>` - GERÇEK sayfa kaynağıyla
doğrulandı) - bu modül tarih+saati DOĞRUDAN UTC olarak parse eder, ek bir
dönüşüm YAPMAZ.

Forecast alanı (2026-08-19): TE tablosunda İKİ ayrı sütun var - "Consensus"
(piyasa/analist anketi beklentisi) ve "Forecast" (TE'nin KENDİ ekonometrik
model tahmini, resmi API dokümantasyonunda "TEForecast"). Bu modül
"Consensus" değerini `forecast_value` olarak alır - Investing.com/
ForexFactory gibi diğer takvimlerde "Forecast" denen GERÇEK piyasa-beklenti
rakamı bu (GERÇEK sayfa kaynağıyla doğrulandı: `id="consensus"` vs
`id="forecast"`, sütun başlıkları "Consensus"/"Forecast").
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

from src.fetchers.base import is_allowed, rate_limit

logger = logging.getLogger(__name__)

_CALENDAR_URL = "https://tradingeconomics.com/calendar"
_USER_AGENT = "FinansHaberToplayiciBot/1.0 (+ekonomik takvim)"
_REQUEST_TIMEOUT = 15.0
_MIN_REQUEST_INTERVAL_SECONDS = 3.0  # aynı taramadaki 3 sıralı istek arası nezaket bekleme

# Kullanıcı kararı (2026-08-19, ÖNCEKİ "Türkiye=tüm seviyeler, diğerleri=
# 2-3 yıldız" ayrımını KALDIRDI): TÜM 7 ülke/bölge için 1-2-3 yıldızın
# TAMAMI DB'ye yazılır - kullanıcıya varsayılan gösterimde SADECE 2-3
# yıldız gösterme filtresi SORGU seviyesinde uygulanır (bkz. src/db.py >
# get_upcoming_calendar_events, get_calendar_events_filtered). Böylece bir
# istemci API üzerinden AÇIKÇA 1 yıldız isterse (importance=1) veri zaten
# DB'de mevcut olur. Ülke kodları Trading Economics'in `calendar-iso`
# hücresinde döndürdüğü GERÇEK kodlarla eşleşir (bkz. DB sorgusuyla
# doğrulanan "EA" = Euro Area, "DE"den TAMAMEN AYRI).
_ALLOWED_COUNTRIES = {"TR", "JP", "CN", "US", "DE", "GB", "EA"}

_DATE_CLASS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _fetch_importance_tier(min_importance: int) -> str | None:
    """Tek bir `?importance=N` isteği - başarısız olursa None döner
    (exception FIRLATMAZ, çağıran taraf bu turu atlar)."""
    url = f"{_CALENDAR_URL}?importance={min_importance}"
    if not is_allowed(url, _USER_AGENT, timeout=_REQUEST_TIMEOUT):
        logger.warning("Ekonomik takvim: robots.txt bu adrese erişime izin vermiyor, atlanıyor (%s)", url)
        return None
    rate_limit(url, _MIN_REQUEST_INTERVAL_SECONDS)
    try:
        response = httpx.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_REQUEST_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        return response.text
    except Exception:  # noqa: BLE001 - bir tur başarısız olursa tüm taramayı durdurmasın
        logger.warning("Ekonomik takvim isteği başarısız oldu: %s", url, exc_info=True)
        return None


def _parse_calendar_html(html: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Verilen HTML'i ayrıştırıp `(data-id, tarih)` anahtarlı bir sözlük
    döner - bu anahtar SADECE tek bir taramadaki 3 farklı importance
    isteğini birbiriyle eşleştirmek için kullanılır (bkz. modül docstring'i,
    _fetch_all_tiers). DB'ye yazarken kullanılan GERÇEK upsert anahtarı
    FARKLI (bkz. src/db.py > EconomicCalendarEvent)."""
    soup = BeautifulSoup(html, "html.parser")
    events: dict[tuple[str, str], dict[str, Any]] = {}

    for row in soup.select("tr[data-id]"):
        data_id = row.get("data-id")
        if not data_id:
            continue

        first_td = row.find("td")
        if first_td is None:
            continue
        date_str = next((c for c in (first_td.get("class") or []) if _DATE_CLASS_RE.match(c)), None)
        if not date_str:
            continue

        time_span = first_td.find("span")
        time_text = time_span.get_text(strip=True) if time_span else ""
        event_time = _parse_event_datetime(date_str, time_text)
        if event_time is None:
            continue

        iso_td = row.find("td", class_="calendar-iso")
        country_code = (iso_td.get_text(strip=True) if iso_td else "").upper()
        country_name = (row.get("data-country") or "").strip().title()

        event_link = row.find("a", class_="calendar-event")
        event_name = (event_link.get_text(strip=True) if event_link else (row.get("data-event") or "")).strip()
        if not event_name or not country_code:
            continue

        reference_span = row.find("span", class_="calendar-reference")
        reference_period = reference_span.get_text(strip=True) if reference_span else None

        actual_el = row.find(id="actual")
        previous_el = row.find(id="previous")
        consensus_el = row.find(id="consensus")  # bkz. modül docstring'i: "Forecast" = piyasa beklentisi
        actual_value = actual_el.get_text(strip=True) if actual_el else None
        previous_value = previous_el.get_text(strip=True) if previous_el else None
        forecast_value = consensus_el.get_text(strip=True) if consensus_el else None
        # previous_el içinde iç içe bir 'revised' span'i de var (bkz. GERÇEK
        # sayfa kaynağı) - get_text bunu da yakalayabilir, sadece İLK metin
        # düğümünü (revize öncesi ana değer) almak için ayrıştırılır.
        if previous_el is not None:
            first_text = next((t for t in previous_el.stripped_strings), None)
            previous_value = first_text

        key = (str(data_id), date_str)
        events[key] = {
            "event_time": event_time,
            "country_code": country_code,
            "country_name": country_name or country_code,
            "event_name": event_name,
            "previous_value": previous_value or None,
            "actual_value": actual_value or None,
            "forecast_value": forecast_value or None,
            "reference_period": reference_period,
        }

    return events


def _parse_event_datetime(date_str: str, time_text: str) -> datetime | None:
    """"2026-08-18" + "06:00 AM" -> UTC datetime. Saat kısmı boş/ayrıştırılamaz
    olabilir (bazı olaylar "All Day" gibi saatsiz gösterilir) - bu durumda
    o günün başlangıcı (00:00 UTC) kullanılır, olay YOK SAYILMAZ (sadece
    hassas-zamanlama izlemesi o olay için anlamsız kalır)."""
    try:
        base_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None
    time_text = (time_text or "").strip()
    try:
        time_part = datetime.strptime(time_text, "%I:%M %p")
        combined = base_date.replace(hour=time_part.hour, minute=time_part.minute)
    except ValueError:
        combined = base_date
    return combined.replace(tzinfo=timezone.utc)


def _fetch_all_tiers() -> list[dict[str, Any]]:
    """3 sıralı istek (importance=1/2/3) atar, küme farkıyla her olayın
    GERÇEK yıldız sayısını hesaplar, sonra 7 izinli ülke/bölge dışındakileri
    eler (bkz. _ALLOWED_COUNTRIES - importance filtresi YOK, 1-2-3'ün TAMAMI
    döner, kullanıcıya gösterimde SADECE 2-3 filtresi SORGU seviyesinde
    uygulanır). Herhangi bir tur başarısız olursa (bkz. _fetch_importance_tier)
    o katman BOŞ kabul edilir - tüm tarama durmaz, sadece o katmanın
    olayları eksik kalır."""
    tier_html = {n: _fetch_importance_tier(n) for n in (1, 2, 3)}
    tier_events = {n: (_parse_calendar_html(html) if html else {}) for n, html in tier_html.items()}

    keys_3 = set(tier_events[3].keys())
    keys_2 = set(tier_events[2].keys())
    keys_1 = set(tier_events[1].keys())

    result: list[dict[str, Any]] = []
    for key, ev in tier_events[1].items():
        if key in keys_3:
            importance = 3
        elif key in keys_2:
            importance = 2
        else:
            importance = 1

        if ev["country_code"] not in _ALLOWED_COUNTRIES:
            continue

        result.append({**ev, "importance": importance})

    return result


class EconomicCalendarProvider(ABC):
    """Ekonomik takvim veri sağlayıcıları için ortak arayüz (2026-08-19,
    kullanıcı isteği) - ileride Investing.com için izinli/resmi bir erişim
    ortaya çıkarsa `InvestingProvider(EconomicCalendarProvider)` eklenip
    `refresh_economic_calendar(provider=...)` ile DEĞİŞTİRİLEBİLİR, DB/API/
    frontend katmanları HİÇ değişmeden (hepsi `upsert_economic_calendar_events`'in
    beklediği aynı sözlük şeklini görür). Şu an TEK gerçek implementasyon
    `TradingEconomicsScraperProvider`."""

    @abstractmethod
    def fetch_events(self) -> list[dict[str, Any]]:
        """7 izinli ülke/bölge (bkz. _ALLOWED_COUNTRIES) için 1-2-3 yıldızın
        TAMAMINI, `upsert_economic_calendar_events`'in beklediği sözlük
        şeklinde (event_time/country_code/country_name/event_name/
        previous_value/actual_value/forecast_value/reference_period/
        importance) döner."""


class TradingEconomicsScraperProvider(EconomicCalendarProvider):
    """Mevcut/tek aktif sağlayıcı - tradingeconomics.com/calendar sayfasını
    3 katmanlı importance isteğiyle tarar (bkz. modül docstring'i)."""

    def fetch_events(self) -> list[dict[str, Any]]:
        return _fetch_all_tiers()


def refresh_economic_calendar(provider: EconomicCalendarProvider | None = None) -> int:
    """Genel/hassas-izleme taramalarının TEK ortak giriş noktası (bkz.
    worker.py > _economic_calendar_refresh_job, _economic_calendar_watch_job -
    ikisi de bu fonksiyonu çağırır, farklı olan sadece HANGİ SIKLIKTA
    çağrıldıkları). `provider` verilmezse varsayılan `TradingEconomicsScraperProvider`
    (bkz. EconomicCalendarProvider). Döner: DB'ye upsert edilen olay sayısı."""
    from src.db import upsert_economic_calendar_events  # döngüsel import'tan kaçınmak için burada

    provider = provider or TradingEconomicsScraperProvider()
    events = provider.fetch_events()
    if not events:
        logger.warning("Ekonomik takvim taraması hiç olay döndürmedi (ağ hatası veya sayfa yapısı değişmiş olabilir).")
        return 0

    upsert_economic_calendar_events(events)
    logger.info("Ekonomik takvim taraması tamamlandı: %d olay upsert edildi.", len(events))
    return len(events)
