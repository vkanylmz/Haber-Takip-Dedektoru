"""TEK SEFERLİK KULLANIM İÇİN YAZILMIŞTIR - kalıcı bir özellik/otomasyon
DEĞİLDİR, tekrar tekrar çalıştırılmak üzere tasarlanmamıştır.

⚠️ ÇALIŞTIRMADAN ÖNCE UYARI (bkz. scripts/rescale_kap_importance_2026_08.py'de
aynı uyarı - buradan da AYNEN geçerli): watchdog/worker sürecini (bkz.
restart_main.ps1) GEÇİCİ OLARAK DURDURUN. Bu script `notified` alanına
dokunmaz ve `importance_score`'u da DEĞİŞTİRMEZ (bkz. altta) ama yine de
canlı süreç açıkken çalıştırmak GENEL bir prensip olarak riskli - script
bitince watchdog'u yeniden başlatmayı unutmayın.

Bağlam (2026-08-17): src/summarizer.py'ye KAP bildirim TÜRÜ/kategorisi
sınıflandırması eklendi (bkz. VALID_KAP_CATEGORIES, KAP_CATEGORY_LABELS).
Yeni gelen KAP kayıtları için bu otomatik çalışıyor (kap_fetcher.py artık
`subject` alanını NewsItem.kap_subject olarak taşıyor, summarize_group bunu
kural tabanlı eşler). AMA DB'de ÖNCEDEN biriken KAP kayıtlarının `subject`
bilgisi HİÇ SAKLANMAMIŞTI (bu özellik eklenmeden önce) - bu yüzden bu script
SADECE başlık+özete bakarak LLM'e (KAP_SYSTEM_PROMPT ile) kap_category
SORAR (kural tabanlı eşleme atlanır, subject yok).

Ne YAPAR:
  - `sources` alanında "KAP" geçen, `kap_category` alanı HÂLÂ BOŞ olan TÜM
    news_records kayıtlarını çeker (idempotent - ikinci çalıştırmada zaten
    kategorisi olanları tekrar SORMAZ, boşuna LLM çağrısı yapmaz).
  - Her biri için minimal bir LLM çağrısı yapar (KAP_SYSTEM_PROMPT, sadece
    başlık+özet gönderilir) ve SADECE `kap_category` alanını okur.
  - SADECE `kap_category` kolonunu UPDATE eder.

Ne YAPMAZ (bilinçli olarak):
  - `notified`, `importance_score`, `importance_reason`, `summary`,
    `company_ticker` vb. HİÇBİR BAŞKA ALANA DOKUNMAZ - bu alanlar zaten
    önceki migration'da (scripts/rescale_kap_importance_2026_08.py) güncel.
  - `company_ticker`'ı KAP'ın stockCodes'undan YENİDEN türetmeye ÇALIŞMAZ -
    o alan da bu özellik eklenmeden önce hiç saklanmamıştı; mevcut (LLM
    tahminli) company_ticker DEĞİŞTİRİLMEDEN bırakılır. Sadece BUNDAN
    SONRAKİ yeni KAP kayıtları KAP'ın otoriter stockCodes'unu kullanacak.

Kullanım: proje kökünden
    .venv\\Scripts\\python.exe scripts\\backfill_kap_category_2026_08.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config, get_gemini_api_key
from src.db import init_db, get_session, NewsRecord
from src.models import NewsGroup, NewsItem
from src.summarizer import Summarizer, KAP_SYSTEM_PROMPT, KAP_CATEGORY_LABELS, _extract_json


def main() -> None:
    config = load_config()
    init_db(config["database"]["path"])

    with get_session() as session:
        records = (
            session.query(NewsRecord)
            .filter(NewsRecord.sources.like("%KAP%"))
            .filter(NewsRecord.kap_category.is_(None))
            .order_by(NewsRecord.id.asc())
            .all()
        )
        before = [{"id": r.id, "title": r.title, "summary": r.summary} for r in records]

    print(f"kap_category BOŞ olan {len(before)} KAP kaydı bulundu.")
    if not before:
        print("Yapılacak bir şey yok, çıkılıyor.")
        return

    summarizer = Summarizer(
        config["summarizer"], get_gemini_api_key(), provider="gemini", output_dir=config["app"].get("output_dir", "data")
    )

    results = []
    for i, item in enumerate(before, start=1):
        # Sadece kap_category istendiğinden minimal bir grup - raw_text boş
        # (subject bilinmiyor, kural tabanlı eşleme zaten atlanacak).
        group = NewsGroup(items=[NewsItem(title=item["title"], link="", source="KAP", published_at=None, raw_text="")])
        user_prompt = summarizer._build_user_prompt(group)

        print(f"[{i}/{len(before)}] id={item['id']}: {item['title'][:70]}")
        try:
            raw = summarizer._call_model_with_retry(user_prompt, system_prompt=KAP_SYSTEM_PROMPT)
            parsed = _extract_json(raw) or {}
            category = summarizer._parse_kap_category(parsed.get("kap_category"))
        except Exception as exc:  # noqa: BLE001 - bir kaydın başarısız olması diğerlerini durdurmasın
            print(f"    HATA: {exc} - 'diger' olarak işaretlenecek.")
            category = "diger"

        results.append({**item, "category": category})

        with get_session() as session:
            record = session.get(NewsRecord, item["id"])
            if record is not None:
                record.kap_category = category

    print("\n" + "=" * 90)
    print(f"{'ID':>6} | {'Kategori':<28} | Başlık")
    for r in results:
        label = KAP_CATEGORY_LABELS.get(r["category"], r["category"])
        print(f"{r['id']:>6} | {label:<28} | {r['title'][:70]}")

    print("=" * 90)
    print(f"TOPLAM: {len(results)} kayıt kategorize edildi.")


if __name__ == "__main__":
    main()
