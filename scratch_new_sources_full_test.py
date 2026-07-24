"""Yeni eklenen lisansli kaynaklarin (investing.com, cnbc.com, forbes.com,
ft.com, wsj.com, businessinsider.com, barrons.com, marketwatch.com,
fortune.com, economist.com) TAM pipeline'dan (fetch -> dedup -> ozetleme ->
onem skoru -> bolge -> sentiment -> DB kaydi) sorunsuz gectigini dogrular.

KESIN KURALLAR (kullanicinin talimati):
  - GERCEK Telegram botuna / abonelere HICBIR mesaj gonderilmez. Bu script
    `src.notifier` veya `src.telegram_bot`'taki gonderim fonksiyonlarini HIC
    IMPORT ETMEZ/CAGIRMAZ - sadece mesaj METNiNi olusturup YAZDIRIR, gercek
    Telegram API'sine (sendMessage) hicbir HTTP istegi atilmaz.
  - GERCEK uretim veritabanina (data/finans_haber.db) hicbir yazma yapilmaz.
    `init_db()` izole/gecici bir dosyaya yonlendirilir (bu script'in kendi
    process'i icinde - canli main.py surecini etkilemez, ayri bir process).
  - EventRegistry API cagrisi (fetch) icin de canli surecin data/state/
    kota/onbellek dosyasina dokunulmaz - ayri bir output_dir kullanilir.
"""

import tempfile
from pathlib import Path

from src.config import get_summarizer_api_key, load_config
from src.db import compute_group_key, get_session, init_db, upsert_group
from src.deduplicator import group_similar_news, sort_groups_by_recency
from src.fetchers.licensed_aggregator import fetch_licensed_aggregator
from src.summarizer import Summarizer

# --------------------------------------------------------------------------
# 1) IZOLE test veritabani (GECICI dosya, uretim db'sine ASLA dokunulmaz)
# --------------------------------------------------------------------------
tmp_dir = Path(tempfile.mkdtemp(prefix="finans_haber_test_"))
test_db_path = tmp_dir / "test_pipeline.db"
init_db(test_db_path)
print(f"[IZOLE] Test veritabani: {test_db_path} (uretim db'sine dokunulmadi)\n")

config = load_config()

# --------------------------------------------------------------------------
# 2) GERCEK API cagrisiyla, yeni eklenen TUM kaynaklari kapsayan haberleri cek
#    (izole output_dir -> canli surecin kota/onbellek dosyasina dokunulmaz)
# --------------------------------------------------------------------------
source_cfg = next(s for s in config["sources"] if s["id"] == "licensed_reuters_bloomberg")
new_domains = [d for d in source_cfg["source_domains"] if d not in ("reuters.com", "bloomberg.com")]
print("Test edilen YENI kaynaklar:", new_domains, "\n")

test_source_cfg = dict(source_cfg)
test_source_cfg["min_interval_minutes"] = 0
test_app_cfg = dict(config["app"])
test_app_cfg["output_dir"] = str(tmp_dir / "state_izole")
test_app_cfg["max_articles_per_source"] = 80

items = fetch_licensed_aggregator(test_source_cfg, test_app_cfg)


def domain_of(link: str) -> str:
    for d in source_cfg["source_domains"]:
        if d in link.lower():
            return d
    return "?"


new_items = [i for i in items if domain_of(i.link) in new_domains]
print(f"Toplam {len(items)} haber cekildi, {len(new_items)} tanesi YENI kaynaklardan.\n")

# --------------------------------------------------------------------------
# 3) Dedup (gercek, bellek ici - DB'ye dokunmaz)
# --------------------------------------------------------------------------
dedup_cfg = config["app"]
groups = group_similar_news(
    items,
    similarity_threshold=dedup_cfg.get("dedup_similarity_threshold", 0.55),
    window_hours=dedup_cfg.get("dedup_window_hours", 12),
)
groups = sort_groups_by_recency(groups)
print(f"Dedup sonrasi {len(groups)} grup ({len(items)} haberden).\n")

# Her YENI kaynaktan (varsa) bir grup secip test edecegiz (breadth testi)
groups_by_new_domain: dict[str, "object"] = {}
for g in groups:
    for it in g.items:
        d = domain_of(it.link)
        if d in new_domains and d not in groups_by_new_domain:
            groups_by_new_domain[d] = g

print(f"Test icin secilen gruplar ({len(groups_by_new_domain)} farkli yeni kaynaktan):")
for d, g in groups_by_new_domain.items():
    print(f"  [{d}] {g.representative.title}")
print()

# --------------------------------------------------------------------------
# 4) Ozetleme + onem skoru + bolge + sentiment (GERCEK Gemini cagrisi)
#    + IZOLE test DB'sine kayit (upsert_group)
# --------------------------------------------------------------------------
provider, api_key = get_summarizer_api_key(config["summarizer"])
summarizer = Summarizer(config["summarizer"], api_key=api_key, provider=provider)

results = []
with get_session() as session:
    for domain, group in groups_by_new_domain.items():
        summarizer.summarize_group(group)
        group_key = compute_group_key(group.representative.title)
        record = upsert_group(session, group, group_key)
        results.append((domain, record))
        print(f"=== [{domain}] {record.title[:70]} ===")
        print(f"  ozet: {record.summary[:150]}")
        print(f"  onem skoru: {record.importance_score}  gerekce: {record.importance_reason[:100]}")
        print(f"  bolgeler: {record.regions_list()}")
        print(f"  sentiment: {record.sentiment}")
        print(f"  DB'ye kaydedildi mi: group_key={record.group_key} (izole test db'sinde)")
        print()

# --------------------------------------------------------------------------
# 5) "Bildirim gonderilir miydi" kontrolu - GERCEK GONDERIM YOK, sadece
#    mesaj METNININ dogru olusturulup olusturulmadigini goruyoruz. Telegram
#    API'sine hicbir HTTP istegi atilmiyor (src.notifier import edilmiyor).
# --------------------------------------------------------------------------
import html as html_lib  # sadece format ornegi icin, notifier'daki mantigin AYNISI


def preview_message(record) -> str:
    """notifier._format_message ile AYNI format, ama bu fonksiyon Telegram'a
    HICBIR SEY GONDERMEZ - sadece string dondurur, biz de yazdiririz."""
    from src.telegram_format import sentiment_emoji

    emoji = sentiment_emoji(record.sentiment)
    prefix = f"{emoji} " if emoji else ""
    return f"[ONIZLEME - GONDERILMEDI] {prefix}{record.title} (skor={record.importance_score}/5)"


threshold = config.get("importance", {}).get("threshold", 4)
print("=== Bildirim ESIGI kontrolu (esik gecerse GONDERILIRDI, ama GERCEKTE GONDERMIYORUZ) ===")
for domain, record in results:
    would_notify = record.importance_score is not None and record.importance_score >= threshold
    print(f"  [{domain}] skor={record.importance_score} -> esik({threshold}) gecti mi: {would_notify}")
    if would_notify:
        print(f"     {preview_message(record)}")

print("\n[DOGRULAMA] Bu script boyunca:")
print("  - src.notifier / src.telegram_bot HIC import edilmedi -> Telegram'a hicbir istek atilmadi")
print(f"  - Tum DB yazmalari izole test db'sine gitti: {test_db_path}")
print("  - Uretim veritabani (data/finans_haber.db) HIC acilmadi/degistirilmedi")
