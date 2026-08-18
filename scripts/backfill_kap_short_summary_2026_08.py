"""TEK SEFERLİK KULLANIM İÇİN YAZILMIŞTIR - kalıcı bir özellik/otomasyon
DEĞİLDİR, tekrar tekrar çalıştırılmak üzere tasarlanmamıştır.

⚠️ ÇALIŞTIRMADAN ÖNCE UYARI (bkz. scripts/rescale_kap_importance_2026_08.py /
backfill_kap_category_2026_08.py'de AYNI uyarı - önceki bir migrasyonda canlı
watchdog açıkken çalıştırıldığında `importance_score` güncellemesi
`notified=False` ile birleşip GERÇEK bir Telegram bildirimi tetiklemişti):
watchdog/worker sürecini (bkz. restart_main.ps1) GEÇİCİ OLARAK DURDURUN. Bu
script `notified`/`importance_score`'a dokunmaz ama yine de canlı süreç
açıkken çalıştırmak GENEL bir prensip olarak riskli - script bitince
watchdog'u yeniden başlatmayı unutmayın.

Bağlam (2026-08-18): src/summarizer.py'ye KAP kartlarının ana/vurgulu satırı
için "[TICKER]: [somut eylem/sonuç]" formatında kısa bir `short_summary`
alanı eklendi (bkz. _KAP_SHORT_SUMMARY_INSTRUCTION, src/models.py >
NewsGroup.short_summary). Yeni gelen KAP kayıtları için bu otomatik çalışıyor.
Bu script DB'de ÖNCEDEN biriken KAP kayıtlarını geriye dönük doldurur.

Ne YAPAR:
  - `sources` alanında "KAP" geçen, `short_summary` alanı HÂLÂ BOŞ olan TÜM
    news_records kayıtlarını çeker (idempotent - ikinci çalıştırmada zaten
    dolu olanları tekrar SORMAZ).
  - Ticker'ı LLM'e TAHMİN ETTİRMEZ - kaydın ZATEN sakladığı `company_ticker`
    alanından ("BIST: NETAS" gibi) bare kodu (borsa öneki olmadan) çıkarıp
    (bkz. _bare_ticker_from_company_ticker) prompt'a gömer - _build_user_prompt
    ile AYNI mekanizma (bkz. summarizer.py).
  - Girdi metni olarak kaydın ZATEN saklanmış `summary`'sini kullanır (ham KAP
    metni DB'de hiç saklanmamıştı) - bu, boş/ham metinden daha iyi bir short_summary
    üretir çünkü summary zaten somut sayısal detayları içeriyor.
  - Her biri için minimal bir LLM çağrısı yapar (KAP_SYSTEM_PROMPT) ve SADECE
    `short_summary` alanını okur.
  - SADECE `short_summary` kolonunu UPDATE eder.
  - `company_ticker` boş olan (ticker çözülemeyen) kayıtları ATLAR - short_summary
    zaten ticker'sız üretilemez (bkz. _KAP_SHORT_SUMMARY_INSTRUCTION).

Ne YAPMAZ (bilinçli olarak):
  - `notified`, `importance_score`, `importance_reason`, `summary`, `title`,
    `company_ticker`, `kap_category` vb. HİÇBİR BAŞKA ALANA DOKUNMAZ.

Kullanım: proje kökünden
    .venv\\Scripts\\python.exe scripts\\backfill_kap_short_summary_2026_08.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config, get_gemini_api_key
from src.db import init_db, get_session, NewsRecord
from src.models import NewsGroup, NewsItem
from src.summarizer import Summarizer, KAP_SYSTEM_PROMPT, _extract_json


def _bare_ticker_from_company_ticker(company_ticker: str) -> str | None:
    """"BIST: NETAS, BIST: NETAS2" -> "NETAS, NETAS2" - _build_user_prompt'un
    beklediği `kap_stock_codes` (bare kod) formatına çevirir (bkz.
    _kap_company_ticker_from_stock_codes'un TERSİ)."""
    if not company_ticker:
        return None
    parts = [p.split(":", 1)[-1].strip() for p in company_ticker.split(",") if p.strip()]
    parts = [p for p in parts if p]
    return ", ".join(parts) if parts else None


def main() -> None:
    config = load_config()
    init_db(config["database"]["path"])

    with get_session() as session:
        records = (
            session.query(NewsRecord)
            .filter(NewsRecord.sources.like("%KAP%"))
            .filter(NewsRecord.short_summary.is_(None))
            .order_by(NewsRecord.id.asc())
            .all()
        )
        before = [
            {"id": r.id, "title": r.title, "summary": r.summary, "company_ticker": r.company_ticker}
            for r in records
        ]

    print(f"short_summary BOŞ olan {len(before)} KAP kaydı bulundu.")
    skipped = [b for b in before if not _bare_ticker_from_company_ticker(b["company_ticker"])]
    targets = [b for b in before if _bare_ticker_from_company_ticker(b["company_ticker"])]
    if skipped:
        print(f"  -> {len(skipped)} kayıt company_ticker boş olduğu için ATLANACAK (short_summary üretilemez).")
    if not targets:
        print("Yapılacak bir şey yok, çıkılıyor.")
        return

    summarizer = Summarizer(
        config["summarizer"], get_gemini_api_key(), provider="gemini", output_dir=config["app"].get("output_dir", "data")
    )

    results = []
    for i, item in enumerate(targets, start=1):
        bare_ticker = _bare_ticker_from_company_ticker(item["company_ticker"])
        group = NewsGroup(
            items=[
                NewsItem(
                    title=item["title"],
                    link="",
                    source="KAP",
                    published_at=None,
                    raw_text=item["summary"] or "",
                    kap_stock_codes=bare_ticker or "",
                )
            ]
        )
        user_prompt = summarizer._build_user_prompt(group)

        print(f"[{i}/{len(targets)}] id={item['id']} ticker={bare_ticker}: {item['title'][:60]}")
        try:
            raw = summarizer._call_model_with_retry(user_prompt, system_prompt=KAP_SYSTEM_PROMPT)
            parsed = _extract_json(raw) or {}
            short_summary = str(parsed.get("short_summary", "")).strip() or None
        except Exception as exc:  # noqa: BLE001 - bir kaydın başarısız olması diğerlerini durdurmasın
            print(f"    HATA: {exc} - atlanıyor.")
            short_summary = None

        if short_summary:
            results.append({**item, "short_summary": short_summary})
            with get_session() as session:
                record = session.get(NewsRecord, item["id"])
                if record is not None:
                    record.short_summary = short_summary

    print("\n" + "=" * 100)
    for r in results:
        print(f"{r['id']:>6} | {r['short_summary']}")
    print("=" * 100)
    print(f"TOPLAM: {len(results)}/{len(targets)} kayıt short_summary ile güncellendi.")


if __name__ == "__main__":
    main()
