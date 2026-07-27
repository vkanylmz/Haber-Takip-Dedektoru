"""Vercel'e deploy edilen SALT-OKUNU (read-only) dashboard giriş noktası.

Bu, `src/web/app.py`'deki TAM uygulamanın (worker + Telegram bot + dashboard
BİRLİKTE, `python main.py` ile yerel bilgisayarda çalışan) BİLEREK
KISITLANMIŞ bir alt kümesidir - bkz. README > "Vercel'e Deploy - Hibrit
Mimari":

  - Worker (RSS taraması) ve Telegram bot dinleyicisi BURADA YOK - onlar
    hâlâ yerel bilgisayarda `python main.py` ile çalışmaya devam ediyor.
  - Bu Vercel fonksiyonu SADECE okuma/görüntüleme rotalarını sunar: ana
    dashboard (filtreler + arama + piyasa şeridi + sektör ısı haritası +
    trend özeti) ve kaynak sağlık paneli.
  - Route HANDLER FONKSİYONLARININ KENDİSİ `src/web/app.py`'den import
    edilip YENİDEN KULLANILIR (kod tekrarı yok, tek bir kaynak-of-truth).
  - BİLEREK route olarak KAYDEDİLMEYEN tek şey `/sirket-profili`'dir: o
    rota her istekte GERÇEK bir LLM API çağrısı (ücretli) tetikliyor - bu
    dashboard artık kimlik doğrulaması olmadan İNTERNETE AÇIK olacağından,
    rastgele ziyaretçilerin bu rotayı tekrar tekrar çağırıp API kotanızı/
    faturanızı tüketmesi mümkün olurdu. Bu risk ortadan kalkana kadar
    (ör. bir parola/kimlik doğrulama katmanı eklenene kadar) bu rota
    Vercel'e YANSITILMAZ - yalnızca yerel `python main.py`'de kalır.

  - Her iki ortam (yerel worker/bot VE bu Vercel fonksiyonu) AYNI
    `DATABASE_URL` (Neon Postgres) üzerinden veri paylaşır - bkz.
    src/db.py > init_db.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from src.config import load_config
from src.db import init_db
from src.web.app import (
    dashboard as _dashboard_view,
    health as _health_view,
    market_data as _market_data_view,
    sector_heatmap as _sector_heatmap_view,
    source_health_page as _source_health_view,
    trend_summary as _trend_summary_view,
)

# Vercel'in serverless ("soğuk başlangıç" olabilen) ortamında FastAPI'nin
# `lifespan` (startup/shutdown) event'lerinin HER platformda güvenilir
# şekilde tetiklendiği garanti değildir - bu yüzden init_db() burada MODÜL
# İÇE AKTARILIRKEN (import time, cold start başına bir kez) doğrudan
# çağrılır, lifespan'a bağımlı kalınmaz. `src/db.py > init_db`,
# `DATABASE_URL` ortam değişkeni tanımlıysa Postgres'e bağlanır - Vercel'de
# bu değişken HER ZAMAN tanımlı olmalı (bkz. README > "Vercel'e Deploy"),
# aksi halde (yerel bir dosya sistemi olmadığından) veritabanı hiç
# çalışmaz.
_config = load_config()
init_db(_config.get("database", {}).get("path", "data/finans_haber.db"))

app = FastAPI(title="Finansal Haber Dashboard (Salt-Okunur / Vercel)")

app.get("/", response_class=HTMLResponse)(_dashboard_view)
app.get("/kaynak-sagligi", response_class=HTMLResponse)(_source_health_view)
app.get("/api/market-data")(_market_data_view)
app.get("/api/sector-heatmap")(_sector_heatmap_view)
app.get("/api/trend-summary")(_trend_summary_view)
app.get("/health")(_health_view)
