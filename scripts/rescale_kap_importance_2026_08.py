"""TEK SEFERLİK KULLANIM İÇİN YAZILMIŞTIR - kalıcı bir özellik/otomasyon
DEĞİLDİR, tekrar tekrar çalıştırılmak üzere tasarlanmamıştır.

⚠️ ÇALIŞTIRMADAN ÖNCE UYARI: Bu tür toplu geçmişe dönük skor güncellemeleri
yapmadan önce watchdog/worker sürecini (bkz. restart_main.ps1) GEÇİCİ OLARAK
DURDURUN, aksi halde hâlâ aktif taranan (fetcher'ın son ~30 dk içinde tekrar
tekrar döndürdüğü) kayıtlarda istenmeyen GERÇEK bir Telegram bildirimi
tetiklenebilir. Bu script `notified` alanına dokunmasa/notifier.py'yi hiç
çağırmasa BİLE, bu script sadece `importance_score`'u DB'de günceller;
watchdog CANLI çalışıyorsa, main.py'nin kendi normal döngüsü
(_reuse_or_mark_for_summarization -> _persist_and_notify_single, bkz.
src/main.py) bir sonraki taramasında bu GÜNCEL skoru DB'den okuyup
"notified=0 ve skor >= eşik" şartını sağlıyor bulursa KENDİ BAŞINA gerçek
bir bildirim gönderir - script'in bunu istemesi/tetiklemesi GEREKMEZ, sadece
skor değişikliği + o an hâlâ "taze" olan bir kayıt yeterlidir. Bu TEORİK bir
risk değil: 2026-08-17'de bu script canlı watchdog çalışırken kullanıldığında
tam olarak bu şekilde bir kayıt (GÖKNUR GIDA, skor 3->5) 3 aboneye gerçek bir
bildirim olarak gitti (script'in DB yazmasından dakikalar sonra, worker'ın
kendi KAP hızlı yoklama döngüsü tetikledi).

Bağlam (2026-08-17): src/summarizer.py'ye KAP'a özgü bir önem skorlama
rubriği (KAP_SYSTEM_PROMPT) eklendi (bkz. ilgili commit/README notu). Bu
script, DEĞİŞİKLİKTEN ÖNCE zaten işlenmiş, DB'de biriken KAP kayıtlarını
YENİ rubrikle bir kerelik yeniden skorlamak için yazıldı - böylece geçmiş
kayıtlar da yeni, KAP'a özgü ölçütten faydalanır.

Ne YAPAR:
  - `sources` alanında "KAP" geçen TÜM news_records kayıtlarını çeker.
  - Her biri için (başlıktan yeniden kurulmuş minimal bir NewsGroup ile)
    GERÇEK bir LLM çağrısı yapar (KAP_SYSTEM_PROMPT ile).
  - SADECE importance_score ve importance_reason alanlarını UPDATE eder
    (aynı satır, id değişmez - yeni satır eklenmez).

Ne YAPMAZ (bilinçli olarak):
  - `notified` alanına DOKUNMAZ - geçmişe dönük Telegram bildirimi TETİKLEMEZ
    (src/notifier.py hiç import edilmez/çağrılmaz). Kullanıcı isteği
    (2026-08-17): "zaten gönderilmiş kayıtlar tekrar gönderilmemeli" -
    aslında bu 17 kaydın hiçbiri hiç gönderilmemişti (hepsi eski eşiğin
    altındaydı) ama script yine de notified'a dokunmayacak şekilde yazıldı,
    ileride benzer bir migration için de güvenli bir şablon olsun diye.
  - summary/key_points/regions/sector/sentiment/market_impact/top_category/
    company_ticker/title_tr alanlarını GÜNCELLEMEZ - sadece skor + gerekçe.

Kullanım: proje kökünden
    .venv\\Scripts\\python.exe scripts\\rescale_kap_importance_2026_08.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config, get_gemini_api_key
from src.db import init_db, get_session, NewsRecord
from src.models import NewsGroup, NewsItem
from src.summarizer import Summarizer, KAP_SYSTEM_PROMPT, _extract_json


def main() -> None:
    config = load_config()
    db_path = config["database"]["path"]
    init_db(db_path)

    with get_session() as session:
        records = (
            session.query(NewsRecord)
            .filter(NewsRecord.sources.like("%KAP%"))
            .order_by(NewsRecord.id.asc())
            .all()
        )
        # Session kapanmadan ÖNCE ihtiyaç duyacağımız alanları ayrı bir
        # listeye kopyalıyoruz - LLM çağrıları uzun sürebileceğinden session'ı
        # bu kadar süre açık tutmak istemiyoruz (bkz. src/main.py'deki AYNI
        # gerekçe, summarize_groups üstündeki NOT).
        before = [
            {"id": r.id, "title": r.title, "old_score": r.importance_score, "old_reason": r.importance_reason}
            for r in records
        ]

    print(f"Toplam {len(before)} KAP kaydı bulundu, yeniden skorlanacak.\n")

    summarizer_cfg = config["summarizer"]
    api_key = get_gemini_api_key()
    summarizer = Summarizer(summarizer_cfg, api_key, provider="gemini", output_dir=config["app"].get("output_dir", "data"))

    results = []
    for i, item in enumerate(before, start=1):
        group = NewsGroup(items=[NewsItem(title=item["title"], link="", source="KAP", published_at=None, raw_text="")])
        user_prompt = summarizer._build_user_prompt(group)

        print(f"[{i}/{len(before)}] id={item['id']}: {item['title'][:70]}")
        try:
            raw = summarizer._call_model_with_retry(user_prompt, system_prompt=KAP_SYSTEM_PROMPT)
            parsed = _extract_json(raw) or {}
            new_score = summarizer._parse_importance_score(parsed.get("importance_score"))
            new_reason = str(parsed.get("importance_reason", "")).strip()
        except Exception as exc:  # noqa: BLE001 - bir kaydın başarısız olması diğerlerini durdurmasın
            print(f"    HATA: {exc} - bu kayıt atlanıyor, eski skoru korunacak.")
            new_score = item["old_score"]
            new_reason = item["old_reason"]

        results.append({**item, "new_score": new_score, "new_reason": new_reason})

        if new_score is not None:
            # SADECE importance_score + importance_reason - notified'a DOKUNULMAZ.
            with get_session() as session:
                record = session.get(NewsRecord, item["id"])
                if record is not None:
                    record.importance_score = new_score
                    record.importance_reason = new_reason

        # Rate limit koruması (bkz. Summarizer._min_interval_seconds) zaten
        # _call_model_with_retry içinde uygulanıyor - ekstra bir sleep GEREKMEZ.

    print("\n" + "=" * 100)
    print(f"{'ID':>6} | {'Eski':>4} | {'Yeni':>4} | {'Değişti mi':>10} | Başlık")
    changed = 0
    for r in results:
        did_change = r["old_score"] != r["new_score"]
        changed += did_change
        print(
            f"{r['id']:>6} | {r['old_score']!s:>4} | {r['new_score']!s:>4} | "
            f"{'EVET' if did_change else 'hayır':>10} | {r['title'][:70]}"
        )

    print("=" * 100)
    print(f"TOPLAM: {len(results)} kayıt işlendi, {changed} kaydın skoru değişti, {len(results) - changed} aynı kaldı.")


if __name__ == "__main__":
    main()
