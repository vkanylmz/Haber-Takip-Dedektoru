"""Gemini veya Anthropic (Claude) API kullanarak haber özetleme + önem
skorlama. Hangi sağlayıcının kullanılacağı `config.yaml > summarizer.llm_provider`
ile seçilir (varsayılan: "gemini"); tek satır değiştirerek "anthropic"a
geri dönülebilir - her iki sağlayıcı da aynı prompt'u ve aynı JSON çıktı
şemasını kullanır.

Gereksinim #3: Özetler kesinlikle haberin birebir kopyası olmamalı, model
kendi cümleleriyle (telif hakkına dikkat ederek) 2-4 cümlelik bir özet ve
varsa madde madde "önemli noktalar" üretmelidir.

Önem skorlama: Ekstra bir API çağrısı yapmadan, AYNI çağrıda modelden 1-5
arası bir önem skoru ve kısa bir gerekçe de istenir (tek JSON yanıtı içinde).
Bu skor, config.yaml > importance.threshold değerini geçen haberlerin
Telegram'a bildirilmesi için kullanılır (bkz. src/notifier.py, src/main.py).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import anthropic
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from src.models import NewsGroup

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Sen finansal haberleri özetleyen ve önemini değerlendiren bir asistansın. \
Sana bir veya birden fazla kaynaktan gelen, aynı konudaki haber \
başlık(lar)ı ve kısa açıklama metinleri verilecek.

Özetleme kuralları:
- Kesinlikle kaynak metni birebir/kelimesi kelimesine kopyalama. Her zaman \
kendi cümlelerinle, parafraze ederek yaz (telif hakkı nedeniyle bu zorunludur).
- 2 ile 4 cümle arasında, öz ve bilgilendirici bir özet yaz. Türkçe yaz.
- Eğer metinde net, somut önemli noktalar (rakamlar, kararlar, tarihler, \
şirket/kurum isimleri vb.) varsa bunları ayrı bir madde listesi olarak da \
çıkar. Yoksa boş liste döndür.
- Sadece verilen metne dayan; uydurma bilgi ekleme.

Önem skorlama kuralları (importance_score, 1-5 arası tam sayı):
- 5 = Piyasayı doğrudan ve güçlü şekilde etkileyebilecek gelişme: merkez \
bankası faiz kararı, beklenmedik enflasyon/işsizlik verisi, büyük bir döviz/\
piyasa şoku, savaş/jeopolitik kriz, sistemik önemde bir şirketin iflası vb.
- 4 = Önemli ama 5 kadar acil olmayan gelişme: büyük bir şirketin çeyreklik \
sonuçları beklentilerden ciddi sapıyorsa, önemli bir düzenleyici karar, \
büyük çaplı birleşme/satın alma, kritik bir emtia (petrol, altın vb.) \
fiyatında sert hareket.
- 3 = Orta düzeyde ilgi çekici, piyasa katılımcılarının bilmesi faydalı ama \
acil aksiyon gerektirmeyen haber.
- 2 = Rutin şirket/sektör haberi, küçük ölçekli gelişme.
- 1 = Genel/ilgisiz/finansal önemi düşük haber (yaşam tarzı, spor, dizi/\
film vb. finans sitesinde görünse bile).
- importance_reason: skoru 1 kısa cümleyle (Türkçe) gerekçelendir.

Bölge etiketleme kuralları (regions, bir liste):
- Haberin hangi bölge(ler)i ilgilendirdiğine, SADECE İÇERİĞİNE bakarak karar \
ver — haberin geldiği KAYNAĞA göre değil (ör. CNBC-e'de yayınlanmış bir ABD \
şirketi haberi "abd" olarak etiketlenmeli, "turkiye" değil).
- Şu kategorilerden bir veya birden fazlasını seç: "turkiye" (Türkiye \
ekonomisi/piyasaları/şirketleri/hükümeti ile ilgiliyse), "abd" (ABD \
ekonomisi/Fed/şirketleri/piyasaları ile ilgiliyse), "avrupa" (AB/Euro \
Bölgesi/İngiltere/Avrupa ülkeleri ile ilgiliyse), "asya" (Çin/Japonya/\
Hindistan vb. Asya ülkeleri ile ilgiliyse), "diger" (yukarıdakilerin \
hiçbirine net şekilde girmiyorsa veya küresel/genel bir haberse).
- Haber birden fazla bölgeyi ilgilendiriyorsa (ör. hem ABD hem Avrupa \
piyasalarını etkileyen bir haberse) birden fazla etiket seç.
- Liste asla boş olmasın; hiçbiri net değilse ["diger"] kullan.

Duygu (sentiment) etiketleme kuralları:
- Haberin piyasa/ekonomi açısından etkisinin GENEL YÖNÜNÜ değerlendir: \
"pozitif" (ör. güçlü kâr açıklaması, faiz indirimi, olumlu veri, anlaşma/\
ortaklık), "negatif" (ör. faiz artışı, kötü veri, kriz, iflas, zarar \
açıklaması, jeopolitik gerilim) veya "notr" (rutin bir duyuru, net bir \
yön belirtmeyen haber) - üçünden sadece birini seç.

Sektör etiketleme kuralları (sector, bir liste):
- Haberin hangi sektör(ler)le ilgili olduğuna karar ver. Şu kategorilerden bir \
veya birden fazlasını seç: "teknoloji", "enerji", "finans" (bankacılık, \
sigorta, yatırım fonları vb.), "otomotiv", "perakende", "saglik", "savunma", \
"gayrimenkul", "tarim", "diger" (yukarıdakilerin hiçbirine net şekilde \
girmiyorsa veya genel/makroekonomik bir haberse).
- Haber birden fazla sektörü ilgilendiriyorsa (ör. hem enerji hem otomotiv \
şirketlerini etkileyen bir haberse) birden fazla etiket seç.
- Liste asla boş olmasın; hiçbiri net değilse ["diger"] kullan.

Piyasa Etkisi (market_impact) kuralları:
- Haberin finans piyasalarına yansımasını bir finans uzmanı/analist gözüyle 1 cümlelik beklenti veya yorumla belirt. Eğer haber piyasa için çok anlamsızsa boş bırak (""). Örn: "Fed'in faiz şahin tutumu nedeniyle altın tarafında kısa vadede sert bir satış baskısı görebiliriz."
- ÖNEMLİ: Gelen haber İngilizce veya başka yabancı bir dilde olsa bile çıktılarını KESİNLİKLE Türkçe olarak oluştur. Herhangi bir yabancı dilde sonuç döndürme.


Yanıtını SADECE aşağıdaki JSON şemasına uygun, başka hiçbir açıklama \
olmadan döndür:

{"summary": "...", "key_points": ["...", "..."], "importance_score": 3, "importance_reason": "...", "regions": ["turkiye"], "sector": ["finans"], "sentiment": "notr", "market_impact": "..."}
"""

# Modelden istenebilecek geçerli bölge etiketleri (bkz. SYSTEM_PROMPT).
VALID_REGIONS = ("turkiye", "abd", "avrupa", "asya", "diger")

# Bölge etiketlerinin dashboard'da gösterilecek Türkçe karşılıkları.
REGION_LABELS = {
    "turkiye": "Türkiye",
    "abd": "ABD",
    "avrupa": "Avrupa",
    "asya": "Asya",
    "diger": "Diğer",
}

# Modelden istenebilecek geçerli sektör etiketleri (bkz. SYSTEM_PROMPT).
VALID_SECTORS = (
    "teknoloji",
    "enerji",
    "finans",
    "otomotiv",
    "perakende",
    "saglik",
    "savunma",
    "gayrimenkul",
    "tarim",
    "diger",
)

# Sektör etiketlerinin dashboard'da gösterilecek Türkçe karşılıkları.
SECTOR_LABELS = {
    "teknoloji": "Teknoloji",
    "enerji": "Enerji",
    "finans": "Finans",
    "otomotiv": "Otomotiv",
    "perakende": "Perakende",
    "saglik": "Sağlık",
    "savunma": "Savunma",
    "gayrimenkul": "Gayrimenkul",
    "tarim": "Tarım",
    "diger": "Diğer",
}

# Modelden istenebilecek geçerli duygu etiketleri (bkz. SYSTEM_PROMPT).
VALID_SENTIMENTS = ("pozitif", "negatif", "notr")

# Duygu etiketlerinin dashboard'da gösterilecek Türkçe karşılıkları (emoji + metin).
SENTIMENT_LABELS = {
    "pozitif": "📈 Pozitif",
    "negatif": "📉 Negatif",
    "notr": "➖ Nötr",
}

# Günlük özet için: bir önceki 24 saatte toplanan haberler arasından
# gerçekten değerli/aksiyon alınabilir 5-10 tanesini seçtirmek üzere ayrı bir
# sistem promptu (bkz. Summarizer.select_daily_highlights, src/daily_digest.py).
DAILY_DIGEST_SYSTEM_PROMPT = """\
Sen bir finans haber editörüsün. Sana, son 24 saatte toplanmış bir haber \
listesi verilecek; her satırda sıra numarası (index), önem skoru, başlık ve \
kısa özet bulunuyor.

Görevin: bunlar arasından GERÇEKTEN piyasa/yatırımcı için değerli, aksiyon \
alınabilir, önemli olan 5 ile 10 arası haberi seçmek. Sadece önem skoruna \
göre sıralama YETERLİ DEĞİL - aynı skorda çok haber olabilir; içeriğe bakarak \
gerçekten öne çıkanları seç. Rutin/tekrar eden/önemsiz haberleri eleme. Aday \
sayısı 5'ten azsa sadece uygun olanları seç (asla var olmayan bir haber \
uydurma). Her seçim için 1 kısa cümlelik (Türkçe) bir gerekçe yaz.

Yanıtını SADECE aşağıdaki JSON şemasına uygun, başka hiçbir açıklama \
olmadan döndür:

{"selections": [{"index": 3, "reason": "..."}, {"index": 7, "reason": "..."}]}
"""

# Şirket/hisse profili sayfası için (bkz. src/company_profile.py, web
# dashboard'daki "Şirket Profili" sayfası): kullanıcının aradığı şirketle
# ilgili son 30 günün haberlerinden TEK bir genel görünüm (outlook) paragrafı
# ürettirmek üzere ayrı bir sistem promptu.
COMPANY_PROFILE_SYSTEM_PROMPT = """\
Sen bir finans analistisin. Sana bir şirket/varlık adı ve o şirketle ilgili \
son 30 günde toplanmış haberlerin başlık+özetleri verilecek.

Görevin: TÜM bu haberlere bakarak, bu şirket hakkında son 30 günün GENEL \
GÖRÜNÜMÜNÜ (outlook) özetleyen 3-5 cümlelik TEK bir paragraf (Türkçe) \
yazmak - hangi konular/temalar öne çıktı, genel duygu/yön ne (olumlu/olumsuz/\
karışık), varsa en dikkat çekici gelişme(ler) neydi. Objektif ve dengeli bir \
üslup kullan, yatırım tavsiyesi verme.

Yanıtını SADECE aşağıdaki JSON şemasına uygun, başka hiçbir açıklama \
olmadan döndür:

{"summary": "..."}
"""

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')

# gemini-2.5-flash Google tarafından yeni kullanıcılara kapatıldı ("no longer
# available to new users" hatası). İlk denemede gemini-3.6-flash'a geçildi,
# ANCAK gerçek bir çalıştırmada bunun ücretsiz katımda GÜNLÜK sadece 20 istek
# (RPD) ile sınırlı olduğu ortaya çıktı (hata gövdesinde
# "GenerateRequestsPerDayPerProjectPerModel-FreeTier", quotaValue: 20) - bu,
# ~90 haberlik bir tarama için yetersiz. gemini-2.5-flash-lite de yeni
# kullanıcılara kapatılmış durumda. gemini-3.5-flash-lite ("en hızlı, en
# uygun maliyetli 3.5 modeli" olarak tanıtılıyor) hem çalışıyor hem de "lite"
# modeller yüksek hacimli/ücretsiz kullanım için tasarlandığından çok daha
# yüksek bir günlük kotaya sahip - gerçek bir çalıştırmayla doğrulandı
# (2026-07, bkz. README > Rate Limit Koruması).
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    # Model bazen ```json ... ``` gibi kod bloğu döndürebilir; temizle.
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    match = _JSON_BLOCK_RE.search(text)
    candidate = match.group(0) if match else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _extract_retry_delay_seconds(exc: Exception) -> float | None:
    """429 (RESOURCE_EXHAUSTED) yanıtındaki `retryDelay` alanını (ör. "38s")
    okuyup saniyeye çevirir. Google'ın hata gövdesi genelde şu şekildedir:

        {"error": {..., "details": [..., {"@type": "...RetryInfo", "retryDelay": "38s"}]}}

    Tam iç içe yapıyı ayrıştırmak yerine, hatanın (varsa) `details`
    alanını ya da metnini regex ile taramak, küçük yapı farklılıklarına karşı
    daha dayanıklıdır."""
    payload = getattr(exc, "details", None)
    text = json.dumps(payload) if payload is not None else str(exc)
    match = _RETRY_DELAY_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _is_daily_quota_error(exc: Exception) -> bool:
    """Bir 429 hatasının dakika-başına (RPM) geçici bir limitten mi, yoksa
    günlük (RPD - "requests per day") bir kotanın tamamen tükenmesinden mi
    kaynaklandığını ayırt eder. Google'ın quotaId alanı bunu açıkça belirtir,
    ör. "GenerateRequestsPerDayPerProjectPerModel-FreeTier". Bu ayrım önemli:
    günlük kota tükendiğinde `retryDelay` kadar (birkaç saniye/dakika)
    beklemek işe yaramaz - kota ancak ertesi gün sıfırlanır. Bu durumda
    gereksiz yere tekrar denemek yerine hemen vazgeçip fallback'e düşmek
    (bkz. _call_model_with_retry) hem zaman kazandırır hem de kotayı daha
    fazla tüketmez."""
    payload = getattr(exc, "details", None)
    text = json.dumps(payload) if payload is not None else str(exc)
    return "perday" in text.lower().replace(" ", "").replace("-", "").replace("_", "")


class Summarizer:
    def __init__(self, summarizer_cfg: dict[str, Any], api_key: str, provider: str = "gemini"):
        if provider not in ("gemini", "anthropic"):
            raise ValueError(f"Bilinmeyen llm_provider: {provider!r} ('gemini' veya 'anthropic' olmalı)")

        self.provider = provider
        self.max_output_tokens = summarizer_cfg.get("max_output_tokens", 900)
        self.language = summarizer_cfg.get("language", "tr")
        self.effort = summarizer_cfg.get("effort", "low")

        # Rate limit koruması: sağlayıcının dakika başına istek (RPM) limitini
        # aşmamak için istekler arasına otomatik bekleme eklenir (gereksinim:
        # Gemini ücretsiz katmanının düşük RPM limiti - varsayılan 5). 429
        # (RESOURCE_EXHAUSTED) alınırsa ayrıca otomatik tekrar denenir (bkz.
        # _call_model_with_retry). config.yaml > summarizer altından
        # ayarlanabilir/kapatılabilir.
        self.rate_limit_enabled = summarizer_cfg.get("rate_limit_enabled", True)
        self.requests_per_minute = summarizer_cfg.get("requests_per_minute", 5)
        self.max_retries = summarizer_cfg.get("max_retries", 3)
        # %5'lik küçük bir güvenlik payı + sabit 0.5 sn: tam sınırda (ör. 12.00
        # sn) bazen yine de 429 alınabiliyor, hafif bir marj bunu engeller.
        self._min_interval_seconds = (
            (60.0 / self.requests_per_minute) + 0.5 if self.requests_per_minute > 0 else 0.0
        )
        self._last_call_started_at: float | None = None

        if provider == "anthropic":
            self.model = summarizer_cfg.get("anthropic_model", DEFAULT_ANTHROPIC_MODEL)
            self.client = anthropic.Anthropic(api_key=api_key)
        else:
            self.model = summarizer_cfg.get("gemini_model", DEFAULT_GEMINI_MODEL)
            self.client = genai.Client(api_key=api_key)

    def _build_user_prompt(self, group: NewsGroup) -> str:
        parts: list[str] = []
        for item in group.items:
            snippet = item.raw_text.strip()
            # Aşırı uzun ham metinleri kırp (token/maliyet kontrolü)
            if len(snippet) > 1500:
                snippet = snippet[:1500] + "..."
            parts.append(f"Kaynak: {item.source}\nBaşlık: {item.title}\nMetin: {snippet or '(açıklama yok)'}")
        return "\n\n---\n\n".join(parts)

    def summarize_group(self, group: NewsGroup) -> None:
        """`group.summary`, `group.key_points`, `group.importance_score` ve
        `group.importance_reason` alanlarını TEK bir API çağrısıyla doldurur
        (ekstra bir "skorlama" çağrısı yapılmaz).

        Herhangi bir hata durumunda exception fırlatmaz; hatayı loglar ve
        başlıktan türetilmiş kısa bir yedek (fallback) özet bırakır (önem
        skoru None kalır -> bu haber Telegram eşiğini asla geçmez), böylece
        bir haberin özetlenmesindeki hata tüm çalıştırmayı durdurmaz.
        """
        user_prompt = self._build_user_prompt(group)
        try:
            raw_text = self._call_model_with_retry(user_prompt)
        except Exception as exc:  # noqa: BLE001
            logger.error("Özetleme API çağrısı başarısız (%s): %s", group.representative.title, exc)
            self._apply_fallback(group)
            return

        parsed = _extract_json(raw_text)
        if not parsed or "summary" not in parsed:
            logger.warning(
                "Özet yanıtı beklenen JSON formatında değildi (%s), ham metin kullanılıyor.",
                group.representative.title,
            )
            group.summary = raw_text or group.representative.title
            group.key_points = []
            group.importance_score = None
            group.importance_reason = ""
            group.regions = []
            group.sectors = []
            group.sentiment = None
            return

        group.summary = str(parsed.get("summary", "")).strip()
        key_points = parsed.get("key_points", [])
        if isinstance(key_points, list):
            group.key_points = [str(p).strip() for p in key_points if str(p).strip()]
        else:
            group.key_points = []

        group.importance_score = self._parse_importance_score(parsed.get("importance_score"))
        group.importance_reason = str(parsed.get("importance_reason", "")).strip()
        group.regions = self._parse_regions(parsed.get("regions"))
        group.sectors = self._parse_sectors(parsed.get("sector"))
        group.sentiment = self._parse_sentiment(parsed.get("sentiment"))
        group.market_impact = str(parsed.get("market_impact", "")).strip() or None

    def _throttle(self) -> None:
        """Sağlayıcının dakika başına istek (RPM) limitini aşmamak için, bir
        önceki çağrının üzerinden `_min_interval_seconds` geçmediyse bekler.
        `rate_limit_enabled: false` ise hiçbir şey yapmaz."""
        if not self.rate_limit_enabled or self._min_interval_seconds <= 0:
            return
        if self._last_call_started_at is not None:
            elapsed = time.monotonic() - self._last_call_started_at
            wait = self._min_interval_seconds - elapsed
            if wait > 0:
                logger.info(
                    "Rate limit koruması: sonraki %s çağrısından önce %.1f sn bekleniyor "
                    "(requests_per_minute=%s).",
                    self.provider,
                    wait,
                    self.requests_per_minute,
                )
                time.sleep(wait)
        self._last_call_started_at = time.monotonic()

    def _classify_rate_limit_error(self, exc: Exception) -> tuple[bool, float | None]:
        """Verilen exception'ın bir 429 (rate limit) hatası olup olmadığını
        ve varsa sağlayıcının önerdiği bekleme süresini (saniye) döner."""
        if self.provider == "gemini":
            if isinstance(exc, genai_errors.ClientError) and getattr(exc, "code", None) == 429:
                return True, _extract_retry_delay_seconds(exc)
            return False, None

        # anthropic
        if isinstance(exc, anthropic.RateLimitError):
            retry_after: float | None = None
            try:
                retry_after = float(exc.response.headers.get("retry-after", ""))
            except (AttributeError, ValueError, TypeError):
                retry_after = None
            return True, retry_after
        return False, None

    def _call_model_with_retry(self, user_prompt: str, system_prompt: str = SYSTEM_PROMPT) -> str:
        """`_call_model`'i rate-limit koruması (istekler arası bekleme) ve
        429 (RESOURCE_EXHAUSTED) hatasında otomatik tekrar deneme ile sarar.

        429 dışındaki hatalar hiç yakalanmadan olduğu gibi yukarı fırlatılır
        (`summarize_group` bunları yakalayıp fallback'e düşer). 429'da,
        sağlayıcının döndürdüğü `retryDelay` (Gemini) / `retry-after`
        (Anthropic) kadar beklenip en fazla `max_retries` kez tekrar denenir;
        hepsi tükenirse son hata olduğu gibi fırlatılır.

        `system_prompt`: varsayılan olarak haber özetleme/skorlama promptu
        (SYSTEM_PROMPT); günlük özet seçimi gibi farklı görevler için
        (bkz. `select_daily_highlights`) DAILY_DIGEST_SYSTEM_PROMPT geçilir -
        rate-limit/retry altyapısı aynı kalır, sadece görev talimatı değişir.
        """
        attempt = 0
        while True:
            self._throttle()
            try:
                return self._call_model(user_prompt, system_prompt)
            except Exception as exc:  # noqa: BLE001
                is_rate_limit, retry_delay = self._classify_rate_limit_error(exc)
                attempt += 1
                if not is_rate_limit:
                    raise
                if _is_daily_quota_error(exc):
                    # Günlük kota tükenmiş: retryDelay kadar beklemek işe
                    # yaramaz (kota ancak yarın sıfırlanır). Tekrar denemeden
                    # direkt vazgeçip fallback'e düşülüyor.
                    logger.error(
                        "%s: '%s' modelinin ücretsiz GÜNLÜK istek kotası tükendi. "
                        "Bu dakika-başına bir limit değil; beklemek/tekrar denemek "
                        "yardımcı olmaz. config.yaml > summarizer.gemini_model "
                        "alanından daha yüksek günlük kotalı bir modele geçmeyi "
                        "veya ertesi gün tekrar denemeyi düşünün.",
                        self.provider,
                        self.model,
                    )
                    raise
                if attempt > self.max_retries:
                    raise
                wait = retry_delay if retry_delay is not None else (self._min_interval_seconds or 15.0)
                logger.warning(
                    "%s: 429 (rate limit) hatası alındı, %.1f sn beklenip tekrar denenecek "
                    "(deneme %d/%d).",
                    self.provider,
                    wait,
                    attempt,
                    self.max_retries,
                )
                time.sleep(wait)
                # Yeniden deneme öncesi zaten bekledik; _throttle'ın normal
                # aralığı bir kez daha uygulamaması için sayacı şimdi güncelle.
                self._last_call_started_at = time.monotonic()

    def _call_model(self, user_prompt: str, system_prompt: str = SYSTEM_PROMPT) -> str:
        """Seçili sağlayıcıya tek bir çağrı yapar ve modelin ham metin
        yanıtını döner. Herhangi bir hata olduğunda exception'ı olduğu gibi
        yukarı fırlatır - `_call_model_with_retry` / `summarize_group` bunu
        yakalayıp sırasıyla tekrar dener ya da fallback'e düşer."""
        if self.provider == "anthropic":
            return self._call_anthropic(user_prompt, system_prompt)
        return self._call_gemini(user_prompt, system_prompt)

    def _call_anthropic(self, user_prompt: str, system_prompt: str = SYSTEM_PROMPT) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_output_tokens,
            thinking={"type": "disabled"},
            output_config={"effort": self.effort},
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text_blocks = [b.text for b in response.content if getattr(b, "type", None) == "text"]
        return "\n".join(text_blocks).strip()

    def _call_gemini(self, user_prompt: str, system_prompt: str = SYSTEM_PROMPT) -> str:
        # Not: `thinking_config=ThinkingConfig(thinking_budget=0)` (tam kapatma)
        # gemini-3.6-flash'ta 400 INVALID_ARGUMENT ile reddediliyor - bu model
        # düşünmeyi tamamen sıfıra çekmeyi kabul etmiyor (test edilerek
        # doğrulandı). thinking_config'i hiç göndermemek de (model varsayılan/
        # dinamik davranışına düşer) güvenilir değil: gerçek bir test
        # çalıştırmasında model iç "düşünme"ye ~700 token harcayıp
        # max_output_tokens'ı tükettiğinden, görünür JSON yanıtı yarıda
        # kesiliyordu (finish_reason=MAX_TOKENS). Sınırlı, küçük bir pozitif
        # bütçe (200-500 arası test edildi, ikisi de güvenilir tamamlanıyor)
        # hem 400 hatasını önlüyor hem de görünür yanıt için yeterli token
        # bırakıyor.
        response = self.client.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=self.max_output_tokens,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=400),
                # Model zaten SYSTEM_PROMPT'ta salt JSON döndürmesi için
                # talimatlandırılıyor; bunu ayrıca zorunlu kılmak ayrıştırmayı
                # (bkz. _extract_json) daha güvenilir hale getirir.
                response_mime_type="application/json",
            ),
        )
        return (response.text or "").strip()

    @staticmethod
    def _parse_importance_score(raw_value: Any) -> int | None:
        try:
            score = int(raw_value)
        except (TypeError, ValueError):
            return None
        # Modelin ürettiği değer aralık dışıysa (nadiren olabilir) sınırlara çek.
        return max(1, min(5, score))

    @staticmethod
    def _parse_regions(raw_value: Any) -> list[str]:
        """Modelin döndürdüğü `regions` listesini VALID_REGIONS ile sınırlar,
        bilinmeyen/hatalı değerleri sessizce eler, tekrarları kaldırır. Hiç
        geçerli değer kalmazsa ["diger"] döner (liste asla tamamen boş
        kalmasın diye - bkz. SYSTEM_PROMPT)."""
        if not isinstance(raw_value, list):
            return []
        regions: list[str] = []
        for item in raw_value:
            region = str(item).strip().lower()
            if region in VALID_REGIONS and region not in regions:
                regions.append(region)
        return regions or ["diger"]

    @staticmethod
    def _parse_sectors(raw_value: Any) -> list[str]:
        """Modelin döndürdüğü `sector` listesini VALID_SECTORS ile sınırlar,
        bilinmeyen/hatalı değerleri sessizce eler, tekrarları kaldırır. Hiç
        geçerli değer kalmazsa ["diger"] döner (bkz. _parse_regions - aynı
        yaklaşım)."""
        if not isinstance(raw_value, list):
            return []
        sectors: list[str] = []
        for item in raw_value:
            sector = str(item).strip().lower()
            if sector in VALID_SECTORS and sector not in sectors:
                sectors.append(sector)
        return sectors or ["diger"]

    @staticmethod
    def _parse_sentiment(raw_value: Any) -> str | None:
        """Modelin döndürdüğü `sentiment` değerini VALID_SENTIMENTS ile
        sınırlar; bilinmeyen/hatalı bir değer gelirse None döner (haber
        duygu etiketsiz kalır, hiçbir yerde emoji gösterilmez)."""
        value = str(raw_value).strip().lower() if raw_value is not None else ""
        return value if value in VALID_SENTIMENTS else None

    def select_daily_highlights(self, records: list[Any]) -> list[dict[str, Any]]:
        """Verilen (son 24 saatteki) `NewsRecord` listesi arasından GERÇEKTEN
        önemli 5-10 tanesini seçtirmek için Gemini/Claude'a TEK bir ek çağrı
        yapar (bkz. DAILY_DIGEST_SYSTEM_PROMPT, src/daily_digest.py).

        Döner: [{"index": <records listesindeki sıra>, "reason": "..."}, ...]
        Herhangi bir hata/ayrıştırma sorununda boş liste döner (exception
        fırlatmaz) - çağıran taraf (src/daily_digest.py) bu durumda önem
        skoruna göre basit bir geri dönüşe (fallback) düşer.
        """
        if not records:
            return []

        lines = []
        for i, r in enumerate(records):
            score = r.importance_score if r.importance_score is not None else "?"
            summary = (r.summary or "").strip()[:300]
            lines.append(f"{i}. [Önem: {score}/5] {r.title}\nÖzet: {summary or '(özet yok)'}")
        user_prompt = "\n\n".join(lines)

        try:
            raw_text = self._call_model_with_retry(user_prompt, system_prompt=DAILY_DIGEST_SYSTEM_PROMPT)
        except Exception:  # noqa: BLE001 - günlük özet seçimi başarısız olursa fallback'e düşülsün
            logger.exception("Günlük özet için haber seçimi (LLM çağrısı) başarısız oldu.")
            return []

        parsed = _extract_json(raw_text)
        if not parsed or not isinstance(parsed.get("selections"), list):
            logger.warning("Günlük özet seçim yanıtı beklenen JSON formatında değildi.")
            return []

        selections: list[dict[str, Any]] = []
        seen_indices: set[int] = set()
        for item in parsed["selections"]:
            try:
                index = int(item.get("index"))
            except (TypeError, ValueError, AttributeError):
                continue
            if index in seen_indices or not (0 <= index < len(records)):
                continue
            seen_indices.add(index)
            reason = str(item.get("reason", "")).strip()
            selections.append({"index": index, "reason": reason})

        return selections[:10]

    def summarize_company_profile(self, company_name: str, records: list[Any]) -> str:
        """Verilen şirket adı ve (son 30 günlük) `NewsRecord` listesi için,
        Gemini/Claude'a TEK bir ek çağrı ile genel görünüm (outlook) paragrafı
        ürettirir (bkz. COMPANY_PROFILE_SYSTEM_PROMPT, src/company_profile.py).

        Herhangi bir hata/ayrıştırma sorununda boş string döner (exception
        fırlatmaz) - çağıran taraf bu durumda otomatik özeti göstermeden
        sadece haber listesini gösterir."""
        if not records:
            return ""

        lines = [f"Şirket/Varlık: {company_name}\n"]
        for r in records:
            summary = (r.summary or "").strip()[:300]
            lines.append(f"- {r.title}\n  Özet: {summary or '(özet yok)'}")
        user_prompt = "\n".join(lines)

        try:
            raw_text = self._call_model_with_retry(user_prompt, system_prompt=COMPANY_PROFILE_SYSTEM_PROMPT)
        except Exception:  # noqa: BLE001 - profil özeti başarısız olursa çağıran taraf sessizce atlasın
            logger.exception("Şirket profili özeti (LLM çağrısı) başarısız oldu: %s", company_name)
            return ""

        parsed = _extract_json(raw_text)
        if not parsed or not isinstance(parsed.get("summary"), str):
            logger.warning("Şirket profili özeti yanıtı beklenen JSON formatında değildi: %s", company_name)
            return ""

        return parsed["summary"].strip()

    def _apply_fallback(self, group: NewsGroup) -> None:
        rep = group.representative
        fallback = rep.raw_text.strip()[:280] or rep.title
        group.summary = f"(Otomatik özet üretilemedi) {fallback}"
        group.key_points = []
        group.importance_score = None
        group.importance_reason = ""
        group.regions = []
        group.sectors = []
        group.sentiment = None
        group.market_impact = None
