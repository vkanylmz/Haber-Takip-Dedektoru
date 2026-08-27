"""TEK SEFERLİK/TEKRAR KULLANILABİLİR yardımcı - Fintables MCP'den (SADECE bir
Claude Code oturumu içinde erişilebilir, bkz. src/fintables_financials.py
modül docstring'i) elle/oturum-içi çekilip bir JSON dosyasına yazılmış tek bir
şirketin finansal önbellek payload'ını `save_financial_snapshot` ile DB'ye
(AppState) yazar - 2026-08-27, kullanıcı isteği: "TÜM BIST hisselerinde
çalışsın" (bkz. o oturumdaki toplu ingestion).

JSON dosyasının şekli `save_financial_snapshot`'ın (src/fintables_financials.py)
kwarg'larıyla BİREBİR aynı + zorunlu bir "ticker" alanı - bkz. o fonksiyonun
docstring'i için tam alan tanımları.

Kullanım (proje kökünden):
    .venv\\Scripts\\python.exe scripts\\save_snapshot_from_json.py <json_dosya_yolu> [--db data/finans_haber.db]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db
from src.fintables_financials import save_financial_snapshot


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("json_path")
    parser.add_argument("--db", default="data/finans_haber.db")
    args = parser.parse_args()

    init_db(args.db)

    with open(args.json_path, encoding="utf-8") as f:
        payload = json.load(f)

    ticker = payload.pop("ticker")
    result = save_financial_snapshot(ticker, **payload)

    gt = result.get("gelir_tablosu_detay")
    bd = result.get("bilanco_detay")
    na = result.get("nakit_akis_detay")
    print(
        f"KAYDEDİLDİ: {ticker} | gelir_satir={len(gt['satirlar']) if gt else 0} "
        f"| bilanco_satir={len(bd['satirlar']) if bd else 0} "
        f"| nakit_satir={len(na['satirlar']) if na else 0} "
        f"| oranlar_kategori={len(result.get('oranlar') or {})} "
        f"| carpanlar={'var' if result.get('carpanlar') else 'yok'}"
    )


if __name__ == "__main__":
    main()
