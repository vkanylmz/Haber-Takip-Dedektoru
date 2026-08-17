"""KAP'a özgü önem skorlama prompt'u degisikliginin (2026-08) regresyon
kontrolu: genel/KAP-disi haberlerin SYSTEM_PROMPT'unun BIREBIR ONCEKI GIBI
kaldigini, KAP_SYSTEM_PROMPT'un sadece rubrik blogunda farklilastigini ve
Summarizer._build_user_prompt/summarize_group'un dogru prompt'u sectigini
dogrular. Gercek bir LLM API cagrisi YAPMAZ (api_key sahte, sadece
_build_user_prompt gibi ag gerektirmeyen metotlar/sabitler test edilir).
"""

from src import summarizer as smz
from src.models import NewsGroup, NewsItem

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


# --- 1) SYSTEM_PROMPT (genel haberler) hic degismedi mi? ---
# git diff zaten bunu source seviyesinde kanitliyor (SYSTEM_PROMPT bloguna
# hicbir "-" satiri yok) - burada ayrica CALISMA ZAMANINDA da genel rubrik
# metninin SYSTEM_PROMPT icinde AYNEN (KAP varyanti DEGIL) bulundugunu
# dogruluyoruz.
check(
    "SYSTEM_PROMPT genel rubriği hâlâ içeriyor (Fed/piyasa şoku ölçütü)",
    "merkez bankası faiz kararı" in smz.SYSTEM_PROMPT and "KAP (Kamuyu Aydınlatma Platformu)" not in smz.SYSTEM_PROMPT,
)
check(
    "SYSTEM_PROMPT içinde KAP-özel rubrik YOK (sızma kontrolü)",
    "iflas/tasfiye/konkordota" not in smz.SYSTEM_PROMPT and "sermaye artırımı" not in smz.SYSTEM_PROMPT,
)

# --- 2) KAP_SYSTEM_PROMPT sadece rubrik bloğunda farklı, geri kalanı aynı ---
check(
    "KAP_SYSTEM_PROMPT KAP rubriğini içeriyor",
    "KAP (Kamuyu Aydınlatma Platformu) özel durum" in smz.KAP_SYSTEM_PROMPT
    and "sermaye artırımı" in smz.KAP_SYSTEM_PROMPT,
)

# Rubrik dışındaki her şey (özetleme kuralları, bölge/sektör/duygu/
# top_category/ticker/title_tr, JSON şeması) BİREBİR aynı olmalı - bunu
# doğrulamak için iki prompt'u rubrik bloklarını boş string'e indirgeyerek
# karşılaştırıyoruz.
system_prompt_without_rubric = smz.SYSTEM_PROMPT.replace(smz._GENERAL_IMPORTANCE_RUBRIC, "")
kap_prompt_without_rubric = smz.KAP_SYSTEM_PROMPT.replace(smz._KAP_IMPORTANCE_RUBRIC, "")
check(
    "Rubrik dışındaki TÜM metin (özetleme/bölge/sektör/duygu/top_category/ticker/title_tr/JSON şeması) birebir aynı",
    system_prompt_without_rubric == kap_prompt_without_rubric,
)

# Uzunluk farkı sadece rubrik farkı kadar olmalı (başka bir yerde sessizce
# metin kaybı/eklemesi olmadığının ek bir kanıtı).
expected_len_diff = len(smz._KAP_IMPORTANCE_RUBRIC) - len(smz._GENERAL_IMPORTANCE_RUBRIC)
actual_len_diff = len(smz.KAP_SYSTEM_PROMPT) - len(smz.SYSTEM_PROMPT)
check(
    f"Uzunluk farkı tam olarak rubrik farkına eşit ({expected_len_diff} char)",
    expected_len_diff == actual_len_diff,
    f"beklenen={expected_len_diff}, gerçek={actual_len_diff}",
)

# --- 3) _build_user_prompt: KAP kaydı -> özel etiket, genel kayıt -> "Metin:" ---
fake_summarizer = smz.Summarizer.__new__(smz.Summarizer)  # __init__ (API client kurulumu) atlanır, ağ çağrısı yok

kap_group = NewsGroup(
    items=[
        NewsItem(
            title="ÖRNEK A.Ş.: Sermaye Artırımı Kararı",
            link="https://www.kap.org.tr/tr/Bildirim/1",
            source="KAP",
            published_at=None,
            raw_text="Sermaye Piyasası Araçlarına İlişkin Bildirim",
        )
    ]
)
general_group = NewsGroup(
    items=[
        NewsItem(
            title="Fed faiz kararını açıkladı",
            link="https://example.com/1",
            source="Bloomberg HT",
            published_at=None,
            raw_text="Fed, politika faizini sabit tuttu.",
        )
    ]
)

kap_prompt_text = fake_summarizer._build_user_prompt(kap_group)
general_prompt_text = fake_summarizer._build_user_prompt(general_group)

check(
    "KAP kaydı için 'Bildirim Konusu (KAP taksonomisi):' etiketi kullanılıyor",
    "Bildirim Konusu (KAP taksonomisi): Sermaye Piyasası Araçlarına İlişkin Bildirim" in kap_prompt_text,
)
check(
    "Genel kayıt için hâlâ eski 'Metin:' etiketi kullanılıyor (değişmedi)",
    "Metin: Fed, politika faizini sabit tuttu." in general_prompt_text
    and "Bildirim Konusu" not in general_prompt_text,
)

# --- 4) summarize_group'un system_prompt SEÇİMİ (API çağrısı yapmadan) ---
# _call_model_with_retry'yi geçici olarak yakalayıp hangi system_prompt ile
# çağrıldığını kaydeden bir sahte (stub) ile değiştiriyoruz - gerçek ağ
# çağrısı YOK.
captured: dict[str, str] = {}


def fake_call_model_with_retry(user_prompt: str, system_prompt: str = smz.SYSTEM_PROMPT) -> str:
    captured["system_prompt"] = system_prompt
    # summarize_group'un geri kalanının (JSON parse) patlamaması için minimal geçerli JSON döndür.
    return (
        '{"summary": "x", "key_points": [], "importance_score": 3, "importance_reason": "x", '
        '"regions": ["diger"], "sector": ["diger"], "sentiment": "notr", "market_impact": "", '
        '"top_category": "diger", "company_ticker": "", "title_tr": ""}'
    )


fake_summarizer.daily_quota_guard_enabled = False
fake_summarizer._call_model_with_retry = fake_call_model_with_retry

fake_summarizer.summarize_group(kap_group)
check(
    "summarize_group(KAP grubu) -> KAP_SYSTEM_PROMPT seçildi",
    captured.get("system_prompt") == smz.KAP_SYSTEM_PROMPT,
)

captured.clear()
fake_summarizer.summarize_group(general_group)
check(
    "summarize_group(genel grup) -> (varsayılan) SYSTEM_PROMPT seçildi",
    captured.get("system_prompt") == smz.SYSTEM_PROMPT,
)

print()
if failures:
    print(f"SONUÇ: {len(failures)} kontrol BAŞARISIZ: {failures}")
    raise SystemExit(1)
print(
    "SONUÇ: Tüm kontroller (9/9) BAŞARILI - genel haber skorlaması etkilenmedi, "
    "KAP skorlaması ayrı rubrikle çalışıyor."
)
