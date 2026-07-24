"""Basit bir FastAPI web dashboard'u: veritabanındaki (özetlenmiş, önem
skorlanmış) haberleri listeler, kaynağa göre filtreleme sağlar, sayfa
birkaç dakikada bir otomatik yenilenir (meta refresh — ekstra JS/websocket
gerektirmez).

Çalıştırma (proje kök dizininden, geliştirme amaçlı doğrudan):
    uvicorn src.web.app:app --reload

Normal kullanımda bu, proje kökündeki main.py tarafından worker ile birlikte
başlatılır (bkz. README > Tek Komutla Başlatma).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from src.config import load_config
from src.db import NewsRecord, get_distinct_sources, get_recent_records, get_session, init_db

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    db_path = config.get("database", {}).get("path", "data/finans_haber.db")
    init_db(db_path)
    yield


app = FastAPI(title="Finansal Haber Dashboard", lifespan=lifespan)


def _record_to_view(record: NewsRecord, threshold: int) -> dict[str, Any]:
    score = record.importance_score
    if score is None:
        badge_class = "badge-unknown"
        badge_text = "?"
    elif score >= 5:
        badge_class = "badge-critical"
        badge_text = str(score)
    elif score >= threshold:
        badge_class = "badge-important"
        badge_text = str(score)
    elif score >= 3:
        badge_class = "badge-medium"
        badge_text = str(score)
    else:
        badge_class = "badge-low"
        badge_text = str(score)

    sentiment = (record.sentiment or "").strip().lower()
    sentiment_class, sentiment_label = {
        "pozitif": ("sentiment-positive", "📈 Pozitif"),
        "negatif": ("sentiment-negative", "📉 Negatif"),
        "notr": ("sentiment-neutral", "➖ Nötr"),
    }.get(sentiment, ("sentiment-unknown", None))

    return {
        "title": record.title,
        "sources": record.sources,
        "published_at": record.published_at.strftime("%Y-%m-%d %H:%M UTC") if record.published_at else "tarih bilinmiyor",
        "summary": record.summary or "(özet yok)",
        "key_points": record.key_points_list(),
        "importance_score": score,
        "importance_reason": record.importance_reason or "",
        "notified": record.notified,
        "links": record.links_list(),
        "badge_class": badge_class,
        "badge_text": badge_text,
        "is_highlighted": score is not None and score >= threshold,
        "sentiment_class": sentiment_class,
        "sentiment_label": sentiment_label,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, source: str | None = None) -> HTMLResponse:
    config = load_config()
    threshold = config.get("importance", {}).get("threshold", 4)
    refresh_seconds = config.get("web", {}).get("refresh_seconds", 180)
    max_items = config.get("web", {}).get("max_items", 100)

    with get_session() as session:
        records = get_recent_records(session, limit=max_items, source_filter=source or None)
        sources = get_distinct_sources(session)

    views = [_record_to_view(r, threshold) for r in records]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "records": views,
            "sources": sources,
            "selected_source": source or "",
            "threshold": threshold,
            "refresh_seconds": refresh_seconds,
            "total_count": len(views),
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
