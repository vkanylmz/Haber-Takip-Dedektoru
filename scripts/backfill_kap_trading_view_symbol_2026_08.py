"""TEK SEFERLİK KULLANIM İÇİN YAZILMIŞTIR - kalıcı bir özellik/otomasyon
DEĞİLDİR, tekrar tekrar çalıştırılmak üzere tasarlanmamıştır.

⚠️ ÇALIŞTIRMADAN ÖNCE UYARI (bkz. scripts/backfill_kap_short_summary_2026_08.py/
rescale_kap_importance_2026_08.py/backfill_kap_category_2026_08.py'de AYNI
uyarı): watchdog/worker sürecini (bkz. restart_main.ps1) GEÇİCİ OLARAK
DURDURUN. Bu script `notified`/`importance_score`'a dokunmaz ama yine de canlı
süreç açıkken çalıştırmak GENEL bir prensip olarak riskli - script bitince
watchdog'u yeniden başlatmayı unutmayın.

Bağlam (2026-08-19): src/summarizer.py'ye dashboard kartlarında bir "Teknik
Görünüm" (TradingView) butonu göstermek için `trading_view_symbol` alanı
eklendi (bkz. src/models.py > NewsGroup.trading_view_symbol). KAP kayıtlarında
bu alan LLM'e SORULMAZ (bkz. summarize_group > "4) trading_view_symbol" adımı)
- KAP her zaman Borsa İstanbul'da işlem gören bir şirketle ilgili olduğundan,
kaydın ZATEN sakladığı OTORİTER `company_ticker` alanından ("BIST: XXXX"
formatında) deterministik olarak "BIST:XXXX" türetilir. Bu script DB'de
ÖNCEDEN biriken KAP kayıtlarını AYNI deterministik kuralla geriye dönük
doldurur - HİÇBİR LLM çağrısı YAPMAZ (maliyetsiz, anlık).

Ne YAPAR:
  - `sources` alanında "KAP" geçen, `trading_view_symbol` alanı HÂLÂ BOŞ olan
    TÜM news_records kayıtlarını çeker (idempotent - ikinci çalıştırmada zaten
    dolu olanları tekrar İŞLEMEZ).
  - `company_ticker` alanından ("BIST: XXXX" veya "BIST: XXXX, BIST: YYYY"
    gibi) İLK kodu alıp "BIST:XXXX" formatına çevirir (bkz.
    _first_bist_ticker_from_company_ticker).
  - `company_ticker` boş olan (ticker hiç çözülemeyen) kayıtları ATLAR.
  - SADECE `trading_view_symbol` kolonunu UPDATE eder.

Ne YAPMAZ (bilinçli olarak):
  - `notified`, `importance_score`, `importance_reason`, `summary`, `title`,
    `company_ticker`, `kap_category`, `short_summary` vb. HİÇBİR BAŞKA ALANA
    DOKUNMAZ. Hiçbir LLM/ağ çağrısı yapmaz.

Kullanım: proje kökünden
    .venv\\Scripts\\python.exe scripts\\backfill_kap_trading_view_symbol_2026_08.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.db import init_db, get_session, NewsRecord

_BIST_CODE_RE = re.compile(r"BIST\s*:\s*([A-Z0-9]+)", re.IGNORECASE)


def _first_bist_ticker_from_company_ticker(company_ticker: str | None) -> str | None:
    """"BIST: NETAS, BIST: NETAS2" -> "BIST:NETAS" (bkz. src/summarizer.py >
    _kap_company_ticker_from_stock_codes'un ÜRETTİĞİ formatın tersi - burada
    sadece İLK/ana kod alınır, short_summary'nin kullandığı
    _kap_primary_ticker_code ile AYNI "ilk kodu al" mantığı)."""
    if not company_ticker:
        return None
    match = _BIST_CODE_RE.search(company_ticker)
    if not match:
        return None
    return f"BIST:{match.group(1).upper()}"


def main() -> None:
    config = load_config()
    init_db(config["database"]["path"])

    with get_session() as session:
        records = (
            session.query(NewsRecord)
            .filter(NewsRecord.sources.like("%KAP%"))
            .filter(NewsRecord.trading_view_symbol.is_(None))
            .order_by(NewsRecord.id.asc())
            .all()
        )
        before = [{"id": r.id, "title": r.title, "company_ticker": r.company_ticker} for r in records]

    print(f"trading_view_symbol BOŞ olan {len(before)} KAP kaydı bulundu.")

    updated = []
    skipped = []
    with get_session() as session:
        for item in before:
            symbol = _first_bist_ticker_from_company_ticker(item["company_ticker"])
            if not symbol:
                skipped.append(item)
                continue
            record = session.get(NewsRecord, item["id"])
            if record is not None:
                record.trading_view_symbol = symbol
                updated.append({**item, "trading_view_symbol": symbol})

    if skipped:
        print(f"  -> {len(skipped)} kayıt company_ticker boş/BIST formatında olmadığı için ATLANDI.")

    print("\n" + "=" * 100)
    for r in updated:
        print(f"{r['id']:>6} | {r['title'][:60]:<60} | {r['trading_view_symbol']}")
    print("=" * 100)
    print(f"TOPLAM: {len(updated)}/{len(before)} kayıt trading_view_symbol ile güncellendi.")


if __name__ == "__main__":
    main()
