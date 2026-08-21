"""TEK SEFERLİK KULLANIM İÇİN YAZILMIŞTIR - kalıcı bir özellik/otomasyon
DEĞİLDİR, tekrar tekrar çalıştırılmak üzere tasarlanmamıştır.

Bağlam (2026-08-21): Dashboard'da bazı kartlarda önem skoru "?/5" olarak
görünüyordu. Kök neden araştırıldı: bu kayıtların `importance_score`'u
GERÇEKTEN NULL - şablon render sorunu DEĞİL (bkz. src/web/app.py >
_record_to_view, `score is None` -> badge_text="?"). NULL'un kendisi de bir
çökme/yarıda kesilme sonucu DEĞİL, İKİ ayrı, kendi içinde tutarlı sebepten:
  1) 68 kayıt: Gemini'nin ÜCRETSİZ katmanının GÜNLÜK istek kotası tükenmiş
     (data/state/gemini_daily_quota_state.json, bkz. src/summarizer.py >
     _quota_exhausted_today) - kota Pasifik saatine göre gece yarısı
     resetleniyor, tespit anında (2026-08-21 sabahı) hâlâ "2026-08-20" günü
     içindeydi (bkz. modül başındaki _QUOTA_RESET_TZ).
  2) 27 kayıt: 2026-08-20 20:22-20:47 arası kısa bir pencerede
     GEMINI_API_KEY ortam değişkeni o an okunamamış (bkz. src/main.py > satır
     153-160) - geçici/tekil bir olay, .env'de anahtar hâlâ mevcut, şu an
     tekrar denendiğinde sorunsuz çalışıyor.
Laptop'un o sabah donup yeniden başlamasıyla İLİŞKİLİ DEĞİL: kota tükenme
zaman damgası (21:47) restart'tan çok önce başlamış ve restart sırasında/
sonrasında da "aynı gün" (Pasifik saati) devam etmiş - watchdog'ta bir
çökme/yarıda kesilme belirtisi YOK (restart_main.log'da süreç düzgün
exit code ile durup yeniden başlamış).

⚠️ ÇALIŞTIRMADAN ÖNCE OKUYUN (bkz. scripts/rescale_kap_importance_2026_08.py
AYNI uyarı - 2026-08-17'de canlı watchdog açıkken bir importance_score
güncellemesi, worker'ın kendi bir sonraki taramasında bu güncel skoru
"notified=0 ve skor >= eşik" olarak bulup GERÇEK bir Telegram bildirimi
göndermesine yol açmıştı): Bu script BİLEREK watchdog'u DURDURMADAN
çalıştırılmak üzere tasarlandı (kullanıcı isteği, 2026-08-21) - o riski
KENDİSİ bertaraf ediyor: her güncellenen kayıt için `importance_score` ile
AYNI DB yazımında `notified=True` de set edilir (bkz. aşağıdaki `record.notified
= True` satırı). Böylece worker'ın kendi taraması aynı group_key'i tekrar
bulsa bile `if record.notified: return` (bkz. src/main.py >
_persist_and_notify_single) devreye girer ve gerçek bir bildirim asla
gönderilmez - bu kayıtlar zaten saatler önce dashboard'da görünüyordu, şimdi
geriye dönük "breaking news" bildirimi göndermek istenen davranış DEĞİL.

Gemini günlük kotası script yazılırken hâlâ tükenmişti; alternatif olarak
Anthropic denendi ama ANTHROPIC_API_KEY'in kredisi yetersiz çıktı ("credit
balance is too low", 2026-08-21 sabahı gerçek bir çağrıyla doğrulandı). Bu
yüzden script normal/varsayılan sağlayıcı olan Gemini'yi kullanır
(config.yaml > summarizer.llm_provider ile AYNI) - kota Pasifik gece
yarısında (≈10:00 Türkiye saati) kendiliğinden resetlendiğinden, bu script
o saatten SONRA çalıştırılmalıdır; ondan önce çalıştırılırsa
_quota_exhausted_today() hâlâ True döner ve TÜM kayıtlar sessizce yine
fallback'e düşer (veri kaybı/hata YOKTUR, sadece hiçbir şey düzelmez -
idempotent olduğundan kota açıldıktan sonra tekrar çalıştırmak güvenlidir).

Ne YAPAR:
  - `importance_score IS NULL` olan TÜM news_records kayıtlarını çeker
    (idempotent - başarıyla düzeltilen bir kayıt bir sonraki çalıştırmada
    zaten importance_score dolu olacağından tekrar SEÇİLMEZ).
  - Her biri için, DB'de zaten saklı olan `summary` alanından (bilinen
    "(... kotası doldu ...)" / "(Özetlenmedi - API anahtarı yok)"
    ön-ekini temizleyip) ham metni geri kurar ve GERÇEK bir Gemini LLM
    çağrısı yapar (summarizer.summarize_group - normal canlı akışla BİREBİR
    AYNI kod yolu/provider).
  - Başarılı olursa: summary, key_points, importance_score,
    importance_reason, regions, sector, sentiment, market_impact,
    top_category, company_ticker, title_tr, trading_view_symbol(_valid)
    alanlarını GÜNCEL LLM çıktısıyla üzerine yazar (aynı satır, group_key
    DEĞİŞMEZ) VE notified=True set eder (yukarıdaki uyarıya bkz.).
  - Bu kayıtlar KAP kaynaklı DEĞİL (2026-08-21'de doğrulandı, `sources`
    sütununda "KAP" geçen hiçbir NULL kayıt yok) - bu yüzden KAP'a özgü
    alanlara (kap_category, short_summary, stockCodes tabanlı
    trading_view_symbol) dokunulmaz/gerek duyulmaz.

Ne YAPMAZ (bilinçli olarak):
  - `title`, `sources`, `links`, `published_at`, `first_seen_at`,
    `last_seen_at`, `group_key` alanlarına dokunmaz.
  - Telegram/Web Push/anahtar kelime bildirim fonksiyonlarını HİÇ import
    etmez/çağırmaz - script kendisi ASLA bildirim göndermez (yukarıdaki not,
    watchdog'un KENDİ taramasıyla ilgili, script'in kendisiyle değil).
  - Bir kayıt başarısız olursa (LLM hatası/beklenmeyen yanıt) o kaydı
    OLDUĞU GİBİ bırakır (eski yer tutucu metin + importance_score NULL),
    diğer kayıtları işlemeye devam eder.

Kullanım: proje kökünden
    .venv\\Scripts\\python.exe scripts\\backfill_null_importance_2026_08.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config, get_gemini_api_key
from src.db import get_session, init_db, NewsRecord, upsert_group
from src.models import NewsGroup, NewsItem
from src.summarizer import Summarizer

# summarizer.py > _apply_fallback ile AYNI iki sabit ön-ek (bkz. modül
# başındaki bağlam notu) - DB'deki yer tutucu özetten ham metni geri
# kurmak için temizlenir.
_FALLBACK_PREFIXES = [
    "(Günlük Gemini kotası doldu, yarın otomatik özetlenecek)",
    "(Özetlenmedi - API anahtarı yok)",
    "(Otomatik özet üretilemedi)",
]


def _recover_raw_text(summary: str | None, title: str) -> str:
    text = (summary or "").strip()
    for prefix in _FALLBACK_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):].strip() or title
    return text or title


def main() -> None:
    config = load_config()
    init_db(config["database"]["path"])

    with get_session() as session:
        records = (
            session.query(NewsRecord)
            .filter(NewsRecord.importance_score.is_(None))
            .order_by(NewsRecord.id.asc())
            .all()
        )
        targets = [
            {
                "id": r.id,
                "group_key": r.group_key,
                "title": r.title,
                "sources": r.sources,
                "summary": r.summary,
                "published_at": r.published_at,
            }
            for r in records
        ]

    print(f"importance_score NULL olan {len(targets)} kayıt bulundu.")
    if not targets:
        print("Yapılacak bir şey yok, çıkılıyor.")
        return

    kap_count = sum(1 for t in targets if "KAP" in (t["sources"] or ""))
    if kap_count:
        print(f"UYARI: {kap_count} kayıt KAP kaynaklı görünüyor - bu script KAP'a özgü alanları doldurmaz, atlanmayacak ama eksik kalabilir.")

    summarizer = Summarizer(
        config["summarizer"],
        get_gemini_api_key(),
        provider="gemini",
        output_dir=config["app"].get("output_dir", "data"),
    )
    if summarizer._quota_exhausted_today():
        print(
            "DUR: Gemini günlük kotası bu script'e göre HÂLÂ tükenmiş durumda "
            "(bkz. data/state/gemini_daily_quota_state.json). Şimdi devam edilirse "
            "tüm kayıtlar sessizce fallback'e düşer. Kota Pasifik gece yarısında "
            "resetlenir (≈10:00 Türkiye saati) - o saatten sonra tekrar çalıştırın."
        )
        return

    fixed: list[dict] = []
    failed: list[dict] = []
    for i, item in enumerate(targets, start=1):
        raw_text = _recover_raw_text(item["summary"], item["title"])
        source_name = (item["sources"] or "").split(",")[0].strip() or "bilinmiyor"
        group = NewsGroup(
            items=[
                NewsItem(
                    title=item["title"],
                    link="",
                    source=source_name,
                    published_at=item["published_at"],
                    raw_text=raw_text,
                )
            ]
        )

        print(f"[{i}/{len(targets)}] id={item['id']}: {item['title'][:70]}")
        try:
            summarizer.summarize_group(group)
        except Exception as exc:  # noqa: BLE001 - bir kaydın başarısız olması diğerlerini durdurmasın
            print(f"    HATA (istisna): {exc} - atlanıyor.")
            failed.append(item)
            continue

        if group.importance_score is None:
            print("    HATA: LLM yine skor üretemedi (fallback'e düştü) - atlanıyor.")
            failed.append(item)
            continue

        with get_session() as session:
            record = upsert_group(session, group, item["group_key"])
            # bkz. modül başındaki ÇALIŞTIRMADAN ÖNCE OKUYUN notu - watchdog
            # canlıyken gerçek bir Telegram bildirimi tetiklenmesini önler.
            record.notified = True

        print(f"    OK: skor={group.importance_score}/5")
        fixed.append({**item, "importance_score": group.importance_score})

    print("\n" + "=" * 100)
    for r in fixed:
        print(f"{r['id']:>6} | {r['importance_score']}/5 | {r['title'][:70]}")
    print("=" * 100)
    print(f"TOPLAM: {len(fixed)}/{len(targets)} kayıt düzeltildi. {len(failed)} kayıt başarısız oldu.")
    if failed:
        print("Başarısız kayıt id'leri:", ", ".join(str(f["id"]) for f in failed))


if __name__ == "__main__":
    main()
