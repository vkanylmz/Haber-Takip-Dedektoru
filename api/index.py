"""Vercel'e deploy edilen SALT-OKUNU (read-only) dashboard giriş noktası.

Bu, `src/web/app.py`'deki TAM uygulamanın (worker + Telegram bot + dashboard
BİRLİKTE, `python main.py` ile yerel bilgisayarda çalışan) BİLEREK
KISITLANMIŞ bir alt kümesidir - bkz. README > "Vercel'e Deploy - Hibrit
Mimari":

  - Worker (RSS taraması) ve Telegram bot dinleyicisi BURADA YOK - onlar
    hâlâ yerel bilgisayarda `python main.py` ile çalışmaya devam ediyor.
  - Bu Vercel fonksiyonu SADECE okuma/görüntüleme rotalarını sunar: ana
    dashboard (filtreler + arama + piyasa şeridi + sektör ısı haritası +
    trend özeti), kaynak sağlık paneli ve "Detaylı İnceleme" (kategori
    sekmeleri) sayfası - hiçbiri LLM çağrısı TETİKLEMEZ, bu yüzden
    kimlik doğrulaması olmadan herkese açık olmaları güvenlidir.
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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from src.config import load_config
from src.db import init_db
from src.web.app import (
    dashboard as _dashboard_view,
    detayli_inceleme_page as _detayli_inceleme_view,
    health as _health_view,
    market_data as _market_data_view,
    sector_heatmap as _sector_heatmap_view,
    source_health_page as _source_health_view,
    ticker_quotes_endpoint as _ticker_quotes_view,
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

# Render free tier'da servis 15 dk inaktivite sonrası spin-down oluyor;
# kullanıcıya Render'ın kendi varsayılan "spinning up" ekranı yerine kendi
# marka/logolu bir splash sayfası göstermek için (bkz. splash-page/index.html,
# GitHub Pages/Cloudflare Pages gibi AYRI bir origin'de barındırılır) o
# sayfanın JS'i buradaki /health'i periyodik olarak fetch() ile yoklar.
# Farklı bir origin'den (GitHub Pages/Cloudflare Pages) yapılan bu fetch()
# çağrısının GERÇEK bir Response (status/ok) okuyabilmesi için CORS
# gerekiyor - GERÇEK bir test (curl -H "Origin: ...") bunun eksik olduğunu
# doğruladı, `mode: "no-cors"` ile de bu sorun ÇÖZÜLEMEZ (opak yanıt,
# status okunamaz). Bu uygulama zaten kimlik doğrulaması olmadan herkese
# açık, SADECE GET rotaları sunuyor (bkz. modül docstring'i) ve hiçbir
# hassas/kişisel veri döndürmüyor - bu yüzden tüm origin'lere izin vermek
# güvenlik riski taşımıyor (aynı veriye zaten doğrudan tarayıcıdan da
# erişilebiliyor).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.get("/", response_class=HTMLResponse)(_dashboard_view)
app.get("/detayli-inceleme", response_class=HTMLResponse)(_detayli_inceleme_view)
app.get("/kaynak-sagligi", response_class=HTMLResponse)(_source_health_view)
app.get("/api/market-data")(_market_data_view)
app.get("/api/ticker-quotes")(_ticker_quotes_view)
app.get("/api/sector-heatmap")(_sector_heatmap_view)
app.get("/api/trend-summary")(_trend_summary_view)
app.get("/health")(_health_view)
