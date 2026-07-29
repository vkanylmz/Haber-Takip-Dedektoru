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

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from src.config import load_config
from src.db import init_db
from src.web.app import (
    commodity_weekly_report_endpoint as _commodity_weekly_report_view,
    company_detail_endpoint as _company_detail_view,
    dashboard as _dashboard_view,
    detayli_inceleme_page as _detayli_inceleme_view,
    health as _health_view,
    market_data as _market_data_view,
    push_subscribe as _push_subscribe_view,
    push_unsubscribe as _push_unsubscribe_view,
    push_vapid_public_key as _push_vapid_public_key_view,
    sector_heatmap as _sector_heatmap_view,
    service_worker_file as _service_worker_view,
    source_health_page as _source_health_view,
    start_sector_heatmap_background_refresh,
    ticker_quotes_endpoint as _ticker_quotes_view,
    trend_summary as _trend_summary_view,
)
from src.web.market_data import start_background_refresh

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Piyasa verisi önbelleğini kullanıcı isteklerinden TAMAMEN BAĞIMSIZ
    # olarak arka planda proaktif tutan görevi başlatır (bkz.
    # src/web/market_data.py > start_background_refresh) - çoklu kullanıcı
    # performans düzeltmesi (2026-07): önceden bir kullanıcı isteği önbellek
    # bayatladığında Yahoo'nun yanıt vermesini/kilidi BEKLEMEK zorundaydı
    # (gerçek testte 2-3 eşzamanlı kullanıcıda 900-1200ms ölçüldü); artık
    # istekler HER ZAMAN hazır bir önbellekten okuyor. `init_db()` (yukarıda)
    # BİLEREK import-time'da kalıyor (Vercel'in serverless ortamında
    # lifespan'ın güvenilir tetiklenmemesi riskine karşı) - ama Render GERÇEK
    # bir persistent process olduğundan (serverless DEĞİL), background
    # task'ı burada başlatmak güvenli ve doğru yer.
    start_background_refresh()
    # Sektör ısı haritası için de AYNI mimari (bkz. src/web/app.py >
    # _SECTOR_HEATMAP_BACKGROUND_INTERVAL_SECONDS notu) - GERÇEK teşhisle
    # bulundu: asıl maliyet Render (Oregon) <-> Neon (Frankfurt) ağ
    # gecikmesi, DB sorgusunun kendisi ~1.8ms (EXPLAIN ANALYZE ile
    # doğrulandı).
    start_sector_heatmap_background_refresh()
    yield


app = FastAPI(title="Finansal Haber Dashboard (Salt-Okunur / Vercel)", lifespan=lifespan)

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
    # POST, Web Push abonelik uç noktaları için eklendi (bkz. aşağıdaki
    # /api/push/subscribe, /api/push/unsubscribe) - bunlar da /sirket-profili
    # gibi LLM/ücretli bir çağrı TETİKLEMEZ, sadece bir veritabanı satırı
    # yazar/siler, bu yüzden aynı "herkese açık güvenli" mantık geçerli.
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.get("/", response_class=HTMLResponse)(_dashboard_view)
app.get("/detayli-inceleme", response_class=HTMLResponse)(_detayli_inceleme_view)
app.get("/kaynak-sagligi", response_class=HTMLResponse)(_source_health_view)
app.get("/api/market-data")(_market_data_view)
app.get("/api/ticker-quotes")(_ticker_quotes_view)
app.get("/api/sector-heatmap")(_sector_heatmap_view)
app.get("/api/trend-summary")(_trend_summary_view)
app.get("/api/commodity-weekly-report")(_commodity_weekly_report_view)
# /sirket-profili'nin AKSİNE (yukarıdaki modül docstring'i) hiçbir LLM
# çağrısı tetiklemez (bkz. src/web/app.py > company_detail_endpoint) -
# yalnızca ücretsiz/kimlik doğrulamasız Yahoo Finance + veritabanı okuması
# yapar, tıpkı zaten herkese açık olan /api/ticker-quotes gibi - bu yüzden
# güvenle Render'a da yansıtılabilir.
app.get("/api/company-detail")(_company_detail_view)
app.get("/health")(_health_view)

# Web Push (bkz. src/web_push.py) - kullanıcı Render'daki bu PUBLIC
# dashboard'u ziyaret ettiğinde abone olur (bkz. dashboard.html), ama
# GERÇEK gönderim (bkz. src/main.py > _persist_and_notify_single) SADECE
# yerel `python main.py` worker'ında çalışır - Telegram bot'uyla AYNI
# hibrit mimari (ikisi de AYNI paylaşımlı DATABASE_URL'i okur/yazar).
app.get("/sw.js")(_service_worker_view)
app.get("/api/push/vapid-public-key")(_push_vapid_public_key_view)
app.post("/api/push/subscribe")(_push_subscribe_view)
app.post("/api/push/unsubscribe")(_push_unsubscribe_view)
