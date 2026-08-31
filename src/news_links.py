"""Haber-KAP Bağlantı Haritası: bir şirketle ilgili KAP (Kamuyu Aydınlatma
Platformu) açıklaması ile genel basında çıkan bir haberin AYNI OLAYI
anlattığını tespit eder (ör. KAP'a "büyük sözleşme imzalandı" bildirimi
yapılır, birkaç saat sonra basında "X şirketi rekor anlaşma yaptı" haberi
çıkar - bu ikisi bağlanır).

İki aşamalı akış (bkz. sohbette hazırlanan fizibilite planı):
  1. UCUZ FİLTRE (src/db.py > find_same_ticker_candidates): aynı
     `company_ticker` (tam eşleşme, "BORSA: SEMBOL") + ±72 saatlik zaman
     penceresi - maliyetsiz bir SQL sorgusu.
  2. LLM DOĞRULAMA (Summarizer.check_same_event): adayın GERÇEKTEN "karşı
     taraftan" olması (biri KAP, diğeri genel haber - bu özelliğin amacı
     KAP<->basın köprüsü kurmak, iki KAP ya da iki genel haber birbirine
     BAĞLANMAZ) VE tek küçük bir LLM çağrısıyla "aynı olayı mı anlatıyor?"
     sorusuna "evet" alması gerekir.

Gerçek veride (2026-08-31 tarihli fizibilite denetiminde) aynı ticker'da
hem KAP hem genel haber olan aday sayısı son derece düşük (5.5 haftada
sadece 13 ticker, bunlardan sadece 1'i gerçek bir eşleşmeydi) - bu yüzden
HER adayı LLM'e sormanın maliyeti ihmal edilebilir düzeyde kalıyor
(`link_already_evaluated` ile aynı çift tekrar tekrar sorulmuyor).

Worker'ın (src/main.py > _persist_and_notify_single) her kaydedilen/
güncellenen kayıt için çağırdığı bir HOOK'tur - ayrı bir periyodik job
DEĞİLDİR (webhook.py üzerinden gelen tekil/anlık kayıtlar için de AYNI hook
çalışır, bkz. summarize_and_persist_groups'un pull/push ortak pipeline
notu)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.config import get_summarizer_api_key, load_config
from src.db import find_same_ticker_candidates, link_already_evaluated, record_news_link_evaluation

if TYPE_CHECKING:
    from src.db import NewsRecord

logger = logging.getLogger(__name__)

# src/fetchers/kap_fetcher.py > KAP_SOURCE_NAME ile AYNI değer olmalıdır -
# bu modülün src.fetchers.kap_fetcher'a bağımlı olmasını gerektirmez (bkz.
# src/notifier.py > _KAP_SOURCE_NAME'deki AYNI desen).
_KAP_SOURCE_NAME = "KAP"

# KAP bildirimi genelde önce, basın haberi birkaç saat SONRA gelir - ama
# bazen tersi de olabilir (ör. önce basında söylenti, sonra KAP resmi
# teyidi) - bu yüzden pencere İKİ YÖNLÜ (bkz. find_same_ticker_candidates).
_LINK_WINDOW_HOURS = 72


def _is_kap_record(record: "NewsRecord") -> bool:
    """Kaydın `sources` alanında (virgülle ayrılmış, ör. "Bloomberg HT,
    CNBC-e") tam olarak `_KAP_SOURCE_NAME` geçip geçmediğini kontrol eder -
    basit bir substring kontrolü DEĞİL (ör. yanlışlıkla "KAPİTAL" gibi bir
    kaynak adını eşleştirmesin diye, bkz. src/notifier.py > _is_kap_record
    AYNI desen)."""
    return _KAP_SOURCE_NAME in (s.strip() for s in (record.sources or "").split(","))


def _build_link_summarizer(config: dict[str, Any] | None):
    """Bağlantı doğrulama için `config.yaml > summarizer` ile AYNI
    sağlayıcı/anahtarı kullanan bir Summarizer örneği kurar - EK bir LLM
    sağlayıcı/anahtar yapılandırması GEREKMEZ. API anahtarı tanımlı değilse
    (bkz. get_summarizer_api_key) None döner - özellik sessizce devre dışı
    kalır, worker akışını DURDURMAZ (company_profile.py > _generate_outlook_summary
    İLE AYNI desen)."""
    cfg = config or load_config()
    summarizer_cfg = cfg.get("summarizer", {})
    try:
        provider, api_key = get_summarizer_api_key(summarizer_cfg)
    except RuntimeError as exc:
        logger.info("%s Haber-KAP bağlantı tespiti bu çalıştırmada atlanıyor.", exc)
        return None

    from src.summarizer import Summarizer  # döngüsel import'tan kaçınmak için burada

    output_dir = cfg.get("app", {}).get("output_dir", "data")
    return Summarizer(summarizer_cfg, api_key=api_key, provider=provider, output_dir=output_dir)


def check_and_link_related_news(record: "NewsRecord", config: dict[str, Any] | None = None) -> None:
    """Verilen (yeni kaydedilmiş/güncellenmiş) haberi, AYNI `company_ticker`'a
    sahip ve ZIT türden (biri KAP, diğeri genel haber) komşu kayıtlarla
    karşılaştırır; LLM "aynı olay" derse (bkz. Summarizer.check_same_event)
    `news_links` tablosuna yazar.

    Hiçbir durumda exception fırlatmaz (worker akışını asla durdurmaz) -
    projedeki diğer bildirim/eşleşme hook'larıyla (bkz.
    src/keyword_alerts.py > check_keyword_matches_and_notify,
    src/web_push.py > notify_matching_push_subscriptions) AYNI "izole et"
    deseni."""
    if not record.company_ticker or record.id is None:
        return

    try:
        candidates = find_same_ticker_candidates(record, window_hours=_LINK_WINDOW_HOURS)
    except Exception:  # noqa: BLE001
        logger.exception("Bağlantı adayları sorgulanırken beklenmeyen hata: %s", record.title)
        return
    if not candidates:
        return

    record_is_kap = _is_kap_record(record)
    # SADECE karşı taraftan (biri KAP, diğeri genel haber) gelen adaylar -
    # iki KAP ya da iki genel haber birbirine bağlanmaz (bkz. modül
    # docstring'i).
    opposite_candidates = [c for c in candidates if _is_kap_record(c) != record_is_kap]
    if not opposite_candidates:
        return

    to_evaluate: list["NewsRecord"] = []
    for candidate in opposite_candidates:
        try:
            if link_already_evaluated(record.id, candidate.id):
                continue
        except Exception:  # noqa: BLE001
            logger.exception(
                "Bağlantı daha önce değerlendirilmiş mi kontrolü başarısız: '%s' <-> '%s'",
                record.title,
                candidate.title,
            )
            continue
        to_evaluate.append(candidate)
    if not to_evaluate:
        return

    summarizer = _build_link_summarizer(config)
    if summarizer is None:
        return

    for candidate in to_evaluate:
        try:
            same_event, reason = summarizer.check_same_event(record, candidate)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Bağlantı doğrulama sırasında beklenmeyen hata: '%s' <-> '%s'", record.title, candidate.title
            )
            continue

        try:
            created = record_news_link_evaluation(record.id, candidate.id, llm_verdict=same_event, reason=reason)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Bağlantı değerlendirmesi veritabanına yazılamadı: '%s' <-> '%s'", record.title, candidate.title
            )
            continue

        if not created:
            continue
        if same_event:
            logger.info(
                "Haber-KAP bağlantısı BULUNDU (ticker=%s): '%s' <-> '%s' (%s)",
                record.company_ticker,
                record.title,
                candidate.title,
                reason,
            )
        else:
            logger.debug(
                "Bağlantı adayı değerlendirildi, aynı olay DEĞİL (ticker=%s): '%s' <-> '%s'",
                record.company_ticker,
                record.title,
                candidate.title,
            )
