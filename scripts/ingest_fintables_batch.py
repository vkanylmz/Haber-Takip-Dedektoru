"""TEK SEFERLİK toplu ingestion yardımcısı (2026-08-27, kullanıcı isteği:
"TÜM BIST hisselerinde çalışsın") - Fintables MCP'den (SADECE bir Claude Code
oturumu içinde erişilebilir, bkz. src/fintables_financials.py modül
docstring'i) bir GRUP ticker için manuel/oturum-içi çekilip `veri_sorgula`
tool'unun ham markdown pipe-table çıktısı olarak dosyaya kaydedilmiş İKİ
sorgu sonucunu (bilanço+gelir+nakit UNION'ı ve oranlar) + bir fiyat/şablon
meta JSON'unu parse edip her ticker için `save_financial_snapshot` çağırır.

Kürasyon KASITLI: mevcut cache'teki (2026-08-26 turu, ör. PGSUS/ASELS/SASA)
şirketlerle AYNI kalem seti kullanılır (bkz. aşağıdaki _GELIR_KALEMLER vb.) -
bu proje TÜM ham satırları değil, önceden belirlenmiş temel kalemleri
saklıyor (gerçek veriyle doğrulandı, bkz. sohbet 2026-08-27).

Girdi dosyaları (`veri_sorgula` sonucundaki "table" alanının AYNEN kopyası -
300 satır/sorgu limitine takılmamak için 4 AYRI sorgu/dosya, bkz. sohbet
2026-08-27: "ORDER BY + UNION ALL" güvenli çalışmıyor, tek sorgu 300 satırla
SESSİZCE kırpılıyor):
  --bilanco-md:  sütunlar hisse_senedi_kodu|kalem|yil|ay|deger
  --gelir-md:    sütunlar hisse_senedi_kodu|kalem|yil|ay|deger (try_ceyreklik)
  --gelir-ttm-md: sütunlar hisse_senedi_kodu|kalem|yil|ay|deger (try_ttm, SADECE FAVÖK)
  --nakit-md:    sütunlar hisse_senedi_kodu|kalem|yil|ay|deger
  --oranlar-md:  sütunlar hisse_senedi_kodu|kategori|oran|yil|ay|deger
  --meta-json: {"TICKER": {"son_fiyat":..,"piyasa_degeri":..,"sablon":..}, ...}

Kullanım (proje kökünden):
    .venv\\Scripts\\python.exe scripts\\ingest_fintables_batch.py \
        --bilanco-md scratch_bilanco.md --gelir-md scratch_gelir.md \
        --gelir-ttm-md scratch_gelir_ttm.md --nakit-md scratch_nakit.md \
        --oranlar-md scratch_oranlar.md --meta-json scratch_bist100_meta.json \
        [--db data/finans_haber.db] [--tickers TIC1,TIC2]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import init_db
from src.fintables_financials import save_financial_snapshot

_BILANCO_KALEMLER = [
    "Nakit ve Nakit Benzerleri",
    "Toplam Dönen Varlıklar",
    "Toplam Duran Varlıklar",
    "Toplam Varlıklar",
    "Toplam Kısa Vadeli Yükümlülükler",
    "Toplam Uzun Vadeli Yükümlülükler",
    "Toplam Özkaynaklar",
    "Toplam Kaynaklar",
    "Net Borç",
    "Toplam Finansal Borçlar",
]
_GELIR_KALEMLER = ["Satış Gelirleri", "Brüt Kar (Zarar)", "Faaliyet Karı (Zararı)", "Dönem Karı (Zararı)", "FAVÖK"]
_NAKIT_KALEMLER = [
    "İşletme Faaliyetlerinden Nakit Akışları",
    "Yatırım Faaliyetlerinden Kaynaklanan Nakit Akışları",
    "Finansman Faaliyetlerinden Nakit Akışları",
    "Nakit ve Nakit Benzerlerindeki Net Artış (Azalış)",
    "Dönem Sonu Nakit ve Nakit Benzerleri",
]

_HESAPLAMA_NOTU = (
    "F/K, PD/DD ve FD/FAVOK Fintables'in hazir bir 'degerleme carpanlari' tablosu "
    "SUNMADIGI icin ham verilerden (hisse_senetleri.son_fiyat/piyasa_degeri, Hisse "
    "Basina Kar TTM, Ozkaynaklar, Net Borc, FAVOK TTM) standart formullerle "
    "HESAPLANMISTIR. Temettu Verimi ve PEG Orani Fintables'ta bulunamadi."
)


def _parse_pipe_table(path: str) -> list[dict[str, str]]:
    """`veri_sorgula`'nın "table" alanındaki markdown pipe-table metnini
    (bkz. modül docstring'i) dict listesine çevirir - ilk satır başlık,
    ikinci satır `---` ayırıcı (atlanır)."""
    with open(path, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    if not lines:
        return []
    headers = [h.strip() for h in lines[0].strip("|").split("|")]
    rows = []
    for line in lines[1:]:
        if set(line.replace("|", "").strip()) <= {"-", " "}:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != len(headers):
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


def _num(raw: str | None) -> float | None:
    if raw is None or raw == "" or raw.upper() == "NULL":
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return int(v) if v == int(v) else v


def _period_label(yil: str, ay: str) -> str:
    return f"{yil}/{int(ay):02d}"


def _build_ticker_payload(
    ticker: str,
    bilanco_rows: list[dict],
    gelir_rows: list[dict],
    gelir_ttm_rows: list[dict],
    nakit_rows: list[dict],
    oranlar_rows: list[dict],
    meta: dict,
) -> dict | None:
    t_bilanco = [r for r in bilanco_rows if r["hisse_senedi_kodu"] == ticker]
    t_gelir = [r for r in gelir_rows if r["hisse_senedi_kodu"] == ticker]
    t_gelir_ttm = [r for r in gelir_ttm_rows if r["hisse_senedi_kodu"] == ticker]
    t_nakit = [r for r in nakit_rows if r["hisse_senedi_kodu"] == ticker]
    t_oranlar = [r for r in oranlar_rows if r["hisse_senedi_kodu"] == ticker]
    if not (t_bilanco or t_gelir or t_nakit or t_oranlar):
        return None

    periods = sorted(
        {(r["yil"], r["ay"]) for r in (t_bilanco + t_gelir + t_nakit)},
        key=lambda p: (int(p[0]), int(p[1])),
        reverse=True,
    )[:5]
    if not periods:
        return None
    period_labels = [_period_label(y, a) for y, a in periods]

    def _rows_for(rows: list[dict], kalemler: list[str]) -> dict[str, list[float | None]]:
        by_kalem_period = {(r["kalem"], r["yil"], r["ay"]): _num(r["deger"]) for r in rows}
        out: dict[str, list[float | None]] = {}
        for kalem in kalemler:
            values = [by_kalem_period.get((kalem, y, a)) for y, a in periods]
            if any(v is not None for v in values):
                out[kalem] = values
        return out

    bilanco_vals = _rows_for(t_bilanco, _BILANCO_KALEMLER)
    gelir_vals = _rows_for(t_gelir, _GELIR_KALEMLER)
    nakit_vals = _rows_for(t_nakit, _NAKIT_KALEMLER)

    def _detay(kalemler_order: list[str], vals: dict[str, list]) -> dict:
        return {
            "donemler": period_labels,
            "satirlar": [{"kalem": k, "degerler": vals[k]} for k in kalemler_order if k in vals],
        }

    bilanco_detay = _detay(_BILANCO_KALEMLER, bilanco_vals)
    gelir_detay = _detay(_GELIR_KALEMLER, gelir_vals)
    nakit_detay = _detay(_NAKIT_KALEMLER, nakit_vals)

    donemler_legacy = []
    for i, (y, a) in enumerate(periods):
        donemler_legacy.append(
            {
                "yil": int(y),
                "ay": int(a),
                "satis_geliri_ceyreklik": gelir_vals.get("Satış Gelirleri", [None] * 5)[i],
                "favok_ceyreklik": gelir_vals.get("FAVÖK", [None] * 5)[i],
                "net_kar_ceyreklik": gelir_vals.get("Dönem Karı (Zararı)", [None] * 5)[i],
            }
        )

    bilanco_ozet = {
        "toplam_varlik": bilanco_vals.get("Toplam Varlıklar", [None])[0],
        "toplam_ozkaynak": bilanco_vals.get("Toplam Özkaynaklar", [None])[0],
        "net_borc": bilanco_vals.get("Net Borç", [None])[0],
    }

    favok_ttm = None
    for r in t_gelir_ttm:
        if r["kalem"] == "FAVÖK" and (r["yil"], r["ay"]) == periods[0]:
            favok_ttm = _num(r["deger"])
            break

    oranlar_serisi: dict[str, dict[str, dict]] = {}
    for r in t_oranlar:
        kat, oran, y, a = r["kategori"], r["oran"], r["yil"], r["ay"]
        if (y, a) not in periods:
            continue
        idx = periods.index((y, a))
        oranlar_serisi.setdefault(kat, {}).setdefault(oran, {"donemler": period_labels, "degerler": [None] * 5})
        oranlar_serisi[kat][oran]["degerler"][idx] = _num(r["deger"])

    oranlar_latest = {
        kat: {oran: seri["degerler"][0] for oran, seri in oranlar.items()} for kat, oranlar in oranlar_serisi.items()
    }

    eps_ttm = None
    for oranlar in oranlar_serisi.values():
        if "Hisse Başına Kar" in oranlar:
            eps_ttm = oranlar["Hisse Başına Kar"]["degerler"][0]
            break

    m = meta.get(ticker, {})
    son_fiyat, piyasa_degeri = m.get("son_fiyat"), m.get("piyasa_degeri")
    net_borc = bilanco_ozet["net_borc"]
    toplam_ozkaynak = bilanco_ozet["toplam_ozkaynak"]

    fk = son_fiyat / eps_ttm if (son_fiyat is not None and eps_ttm not in (None, 0)) else None
    pd_dd = (
        piyasa_degeri / toplam_ozkaynak if (piyasa_degeri is not None and toplam_ozkaynak not in (None, 0)) else None
    )
    fd_favok = (
        (piyasa_degeri + net_borc) / favok_ttm
        if (piyasa_degeri is not None and net_borc is not None and favok_ttm not in (None, 0))
        else None
    )
    net_borc_favok = net_borc / favok_ttm if (net_borc is not None and favok_ttm not in (None, 0)) else None

    carpanlar = None
    if son_fiyat is not None and piyasa_degeri is not None:
        carpanlar = {
            "son_fiyat": son_fiyat,
            "piyasa_degeri": piyasa_degeri,
            "fk": fk,
            "pd_dd": pd_dd,
            "fd_favok": fd_favok,
            "net_borc_favok": net_borc_favok,
            "hesaplama_notu": _HESAPLAMA_NOTU,
        }

    return {
        "donemler": donemler_legacy,
        "oranlar": oranlar_latest,
        "oranlar_serisi": oranlar_serisi or None,
        "bilanco_ozet": bilanco_ozet,
        "bilanco_detay": bilanco_detay,
        "gelir_tablosu_detay": gelir_detay,
        "nakit_akis_detay": nakit_detay,
        "carpanlar": carpanlar,
        "sablon": m.get("sablon", "default"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bilanco-md", required=True)
    parser.add_argument("--gelir-md", required=True)
    parser.add_argument("--gelir-ttm-md", required=True)
    parser.add_argument("--nakit-md", required=True)
    parser.add_argument("--oranlar-md", required=True)
    parser.add_argument("--meta-json", required=True)
    parser.add_argument("--db", default="data/finans_haber.db")
    parser.add_argument("--tickers", help="Virgülle ayrılmış, sadece bu ticker'ları işle (opsiyonel)")
    args = parser.parse_args()

    init_db(args.db)

    bilanco_rows = _parse_pipe_table(args.bilanco_md)
    gelir_rows = _parse_pipe_table(args.gelir_md)
    gelir_ttm_rows = _parse_pipe_table(args.gelir_ttm_md)
    nakit_rows = _parse_pipe_table(args.nakit_md)
    oranlar_rows = _parse_pipe_table(args.oranlar_md)
    with open(args.meta_json, encoding="utf-8") as f:
        meta = json.load(f)

    tickers = args.tickers.split(",") if args.tickers else sorted(meta.keys())

    for ticker in tickers:
        payload = _build_ticker_payload(ticker, bilanco_rows, gelir_rows, gelir_ttm_rows, nakit_rows, oranlar_rows, meta)
        if payload is None:
            print(f"ATLANDI (veri yok): {ticker}")
            continue
        result = save_financial_snapshot(ticker, **payload)
        gt, bd, na = result.get("gelir_tablosu_detay"), result.get("bilanco_detay"), result.get("nakit_akis_detay")
        print(
            f"KAYDEDİLDİ: {ticker} | donem={gt['donemler'][0] if gt and gt['donemler'] else '?'} "
            f"| gelir={len(gt['satirlar']) if gt else 0} bilanco={len(bd['satirlar']) if bd else 0} "
            f"nakit={len(na['satirlar']) if na else 0} oranlar_kat={len(result.get('oranlar') or {})} "
            f"carpanlar={'var' if result.get('carpanlar') else 'yok'}"
        )


if __name__ == "__main__":
    main()
