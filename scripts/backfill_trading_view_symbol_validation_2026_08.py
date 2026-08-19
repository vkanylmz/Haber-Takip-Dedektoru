"""TEK SEFERLİK KULLANIM İÇİN YAZILMIŞTIR - kalıcı bir özellik/otomasyon
DEĞİLDİR, tekrar tekrar çalıştırılmak üzere tasarlanmamıştır.

⚠️ ÇALIŞTIRMADAN ÖNCE UYARI (bkz. scripts/backfill_kap_trading_view_symbol_2026_08.py'de
AYNI uyarı): watchdog/worker sürecini (bkz. restart_main.ps1) GEÇİCİ OLARAK
DURDURUN. Bu script `notified`/`importance_score`'a dokunmaz ama yine de canlı
süreç açıkken çalıştırmak GENEL bir prensip olarak riskli - script bitince
watchdog'u yeniden başlatmayı unutmayın.

Bağlam (2026-08-19, kullanıcı geri bildirimi): dashboard'daki "Teknik
Görünüm" butonları bazı KAP kayıtlarında TradingView'de GERÇEKTEN var
olmayan bir sembole gidiyordu (ör. çok küçük/az işlem gören BIST şirketleri
TradingView'in veritabanında hiç olmayabilir, ya da KAP'ın stockCodes
listesindeki İLK kod TradingView'in kullandığı asıl kod olmayabilir - ör.
"YKB" yok ama AYNI kaydın listesindeki "YKBNK" var). src/tradingview.py'ye
GERÇEK bir TradingView servisini (scanner.tradingview.com/symbol) sorgulayan
bir doğrulama eklendi (bkz. o modülün docstring'i - 90 gerçek sembolle test
edildi, ~%32'si TradingView'de YOK çıktı). Bu script DB'de ÖNCEDEN biriken
trading_view_symbol dolu ama HENÜZ doğrulanmamış (trading_view_symbol_valid
IS NULL) TÜM kayıtları geriye dönük doğrular/düzeltir.

Ne YAPAR:
  - `trading_view_symbol` dolu, `trading_view_symbol_valid` HÂLÂ NULL olan
    TÜM news_records kayıtlarını çeker (idempotent - ikinci çalıştırmada
    zaten doğrulanmış [True/False] kayıtları tekrar İŞLEMEZ - None/bilinmiyor
    kalanlar bir sonraki çalıştırmada tekrar denenir, bkz. src/tradingview.py
    > validate_symbol'ün None dönüş notu).
  - KAP kayıtlarında (`sources` "KAP" içeriyorsa): `company_ticker` alanındaki
    TÜM BIST kodlarını ("BIST: YKB, BIST: YKBNK" gibi) sırayla TradingView'de
    dener, İLK GEÇERLİ olanı `trading_view_symbol`'e YAZAR (böylece "YKB"
    gibi hatalı kodlar otomatik "YKBNK" ile DÜZELTİLİR) ve
    `trading_view_symbol_valid`'i günceller.
  - KAP-DIŞI kayıtlarda: mevcut TEK `trading_view_symbol`'ü (LLM'in tahmini,
    denenecek alternatif YOK) doğrudan doğrular, `trading_view_symbol_valid`'i
    günceller. Sembolün kendisi DEĞİŞTİRİLMEZ.
  - Sonunda özet rapor basar: kaç kayıt DÜZELTİLDİ (sembol değişti), kaç
    kayıt GEÇERLİ kaldı, kaç kayıt GEÇERSİZ (valid=False) işaretlendi, kaç
    kayıt BELİRSİZ (ağ hatası, valid=None, bir sonraki çalıştırmada tekrar
    denenecek) kaldı.

Ne YAPMAZ (bilinçli olarak):
  - `notified`, `importance_score`, `importance_reason`, `summary`, `title`,
    `company_ticker`, `kap_category`, `short_summary` vb. HİÇBİR BAŞKA ALANA
    DOKUNMAZ.

Kullanım: proje kökünden
    .venv\\Scripts\\python.exe scripts\\backfill_trading_view_symbol_validation_2026_08.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.db import init_db, get_session, NewsRecord
from src.tradingview import find_valid_bist_symbol, validate_symbol

_BIST_CODE_RE = re.compile(r"BIST\s*:\s*([A-Z0-9]+)", re.IGNORECASE)


def _bare_bist_codes_from_company_ticker(company_ticker: str | None) -> str:
    """"BIST: YKB, BIST: YKBNK" -> "YKB, YKBNK" (find_valid_bist_symbol'ün
    beklediği çıplak/virgüllü kod formatı - bkz. src/tradingview.py)."""
    if not company_ticker:
        return ""
    codes = _BIST_CODE_RE.findall(company_ticker)
    return ", ".join(c.upper() for c in codes)


def main() -> None:
    config = load_config()
    init_db(config["database"]["path"])

    with get_session() as session:
        records = (
            session.query(NewsRecord)
            .filter(NewsRecord.trading_view_symbol.isnot(None))
            .filter(NewsRecord.trading_view_symbol_valid.is_(None))
            .order_by(NewsRecord.id.asc())
            .all()
        )
        before = [
            {
                "id": r.id,
                "title": r.title,
                "sources": r.sources,
                "company_ticker": r.company_ticker,
                "trading_view_symbol": r.trading_view_symbol,
            }
            for r in records
        ]

    print(f"trading_view_symbol dolu, HENÜZ doğrulanmamış {len(before)} kayıt bulundu.")
    if not before:
        print("Yapılacak bir şey yok, çıkılıyor.")
        return

    corrected = []
    valid_unchanged = []
    invalid = []
    unknown = []

    with get_session() as session:
        for i, item in enumerate(before, start=1):
            is_kap = "KAP" in (item["sources"] or "")
            old_symbol = item["trading_view_symbol"]

            if is_kap:
                bare_codes = _bare_bist_codes_from_company_ticker(item["company_ticker"])
                new_symbol, valid = find_valid_bist_symbol(bare_codes)
                new_symbol = new_symbol or old_symbol
            else:
                new_symbol = old_symbol
                valid = validate_symbol(old_symbol)

            record = session.get(NewsRecord, item["id"])
            if record is None:
                continue
            record.trading_view_symbol = new_symbol
            record.trading_view_symbol_valid = valid

            row = {**item, "new_symbol": new_symbol, "valid": valid}
            if valid is True and new_symbol != old_symbol:
                corrected.append(row)
            elif valid is True:
                valid_unchanged.append(row)
            elif valid is False:
                invalid.append(row)
            else:
                unknown.append(row)

            if i % 20 == 0 or i == len(before):
                print(f"  [{i}/{len(before)}] işlendi...")

    print("\n" + "=" * 100)
    print(f"DÜZELTİLEN (sembol değişti, ör. YKB->YKBNK): {len(corrected)}")
    for r in corrected:
        print(f"  {r['id']:>6} | {r['trading_view_symbol']} -> {r['new_symbol']} | {r['title'][:60]}")

    print(f"\nGEÇERLİ (değişmeden kaldı): {len(valid_unchanged)}")

    print(f"\nGEÇERSİZ (valid=False, buton artık gösterilmeyecek): {len(invalid)}")
    for r in invalid:
        print(f"  {r['id']:>6} | {r['trading_view_symbol']} | {r['title'][:60]}")

    print(f"\nBELİRSİZ (ağ hatası, valid=None, sonraki çalıştırmada tekrar denenecek): {len(unknown)}")
    for r in unknown:
        print(f"  {r['id']:>6} | {r['trading_view_symbol']} | {r['title'][:60]}")

    print("=" * 100)
    print(
        f"TOPLAM: {len(before)} kayıt işlendi - "
        f"{len(corrected)} düzeltildi, {len(valid_unchanged)} geçerli, "
        f"{len(invalid)} geçersiz, {len(unknown)} belirsiz."
    )


if __name__ == "__main__":
    main()
