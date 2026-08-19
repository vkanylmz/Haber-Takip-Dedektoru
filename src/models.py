"""Haber öğelerini temsil eden veri modelleri."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NewsItem:
    """Tek bir kaynaktan çekilen ham haber girdisi."""

    title: str
    link: str
    source: str
    published_at: datetime | None
    raw_text: str = ""  # RSS/HTML'den gelen ham özet/açıklama metni
    # SADECE KAP kaynaklı kayıtlarda dolu (bkz. src/fetchers/kap_fetcher.py) -
    # diğer TÜM kaynaklarda boş string kalır. `kap_subject`, KAP'ın bildirimi
    # ALIRKEN kendi atadığı SABİT taksonomi kategorisidir (ör. "Sermaye
    # Artırımı - Azaltımı İşlemlerine İlişkin Bildirim") - kural tabanlı
    # kap_category atamasının birincil girdisi (bkz. summarizer.py >
    # _rule_based_kap_category). `kap_stock_codes`, KAP API'sinin kendi
    # `stockCodes` alanı - virgülle ayrılmış, BAZEN birden fazla kod
    # içerebilir (ör. "YKB, YKBNK") - LLM'in tahmin ettiği company_ticker
    # YERİNE bu OTORİTER kaynak kullanılır (bkz. summarize_group).
    kap_subject: str = ""
    kap_stock_codes: str = ""
    # Haber görseli (2026-08-18, kullanıcı isteği: dashboard hero/öne çıkan
    # kartlarında GERÇEK haber görseli kullanılsın) - kaynağın kendi RSS
    # feed'inde varsa (media:content/media:thumbnail/enclosure, bkz.
    # src/fetchers/rss_fetcher.py > _extract_image_url) doldurulur. Bu
    # UYDURMA/üretilmiş bir görsel DEĞİL - RSS'in ZATEN sağladığı, feed
    # okuyucularında gösterilmek üzere yayıncının kendisinin verdiği bir
    # URL. Kaynakta yoksa boş string kalır (dashboard bu durumda görselsiz
    # tipografik tasarıma düşer, bkz. templates/dashboard.html > hero-card).
    image_url: str = ""


@dataclass
class NewsGroup:
    """Farklı kaynaklarda aynı konuyu anlatan haberlerin gruplanmış hali."""

    items: list[NewsItem] = field(default_factory=list)
    summary: str = ""
    key_points: list[str] = field(default_factory=list)
    # 1 (rutin) - 5 (piyasayı doğrudan etkileyebilecek büyük gelişme) arası
    # önem skoru. None -> henüz skorlanmadı (ör. özetleme adımı hiç
    # çalıştırılmadıysa veya başarısız olduysa).
    importance_score: int | None = None
    importance_reason: str = ""
    # Haberin içeriğine göre (kaynağına göre DEĞİL) belirlenen bölge etiket(ler)i:
    # "turkiye", "abd", "avrupa", "asya", "diger" alt kümesinden bir veya daha
    # fazlası. Boş liste -> henüz sınıflandırılmadı (bkz. summarizer.py).
    regions: list[str] = field(default_factory=list)
    # Haberin ilgili olduğu sektör etiket(ler)i: "teknoloji", "enerji",
    # "finans", "otomotiv", "perakende", "saglik", "savunma", "gayrimenkul",
    # "tarim", "diger" alt kümesinden bir veya daha fazlası. Boş liste ->
    # henüz sınıflandırılmadı (bkz. summarizer.py).
    sectors: list[str] = field(default_factory=list)
    # Haberin piyasa/ekonomi açısından etkisi: "pozitif", "negatif", "notr".
    # None -> henüz sınıflandırılmadı (bkz. summarizer.py).
    sentiment: str | None = None
    # Haberin piyasaya yansimasina dair AI tarafindan uretilen profesyonel yorum
    market_impact: str | None = None
    # Haberin "Detaylı İnceleme" sayfasındaki (bkz. src/web/app.py) TEK bir üst
    # kategorisi: "makro" (Fed/TCMB/enflasyon/faiz gibi makroekonomi), "sirket"
    # (belirli bir şirketi ilgilendiren haber, ör. Tesla/Turkcell), "siyasi"
    # (siyasi/jeopolitik, ör. ABD-İran) veya "diger" (üçüne de net girmiyor).
    # `sector`/`regions`'tan FARKLI bir sınıflandırma ekseni olduğundan
    # (ör. bir TCMB haberi sector="finans" olabilir ama bu "makro"dur, bir
    # banka kâr açıklaması da sector="finans" olabilir ama bu "sirket"tir)
    # AYRI bir alan olarak tutulur. None -> henüz sınıflandırılmadı.
    top_category: str | None = None
    # Yalnızca top_category="sirket" olan haberlerde anlamlı: ilgili şirketin
    # borsa kodu + ticker sembolü, "BORSA: SEMBOL" formatında (ör.
    # "NASDAQ: TSLA", "BIST: THYAO"). None -> net bir şirket/ticker
    # belirlenemedi (bkz. summarizer.py > _parse_company_ticker).
    company_ticker: str | None = None
    # `representative.title` yabancı dilde ise LLM tarafından üretilen
    # Türkçe çevirisi (bkz. summarizer.py > SYSTEM_PROMPT > title_tr).
    # Başlık zaten Türkçe ise None kalır - gösterim tarafı parantez içi
    # çeviri EKLEMEZ (bkz. src/db.py > NewsRecord.title_tr).
    title_tr: str | None = None
    # SADECE KAP kaynaklı gruplarda anlamlı (bkz. src/summarizer.py >
    # VALID_KAP_CATEGORIES, KAP_CATEGORY_LABELS): 8 sabit kategoriden biri
    # ("sermaye_artirimi", "temettu", "genel_kurul", "finansal_rapor",
    # "borclanma_araci", "hukuki", "yonetim_kurumsal", "diger"). Öncelik
    # `kap_subject`'e uygulanan kural tabanlı eşlemedir (bkz.
    # _rule_based_kap_category) - eşleşmezse (ör. subject="Özel Durum
    # Açıklaması (Genel)") KAP_SYSTEM_PROMPT'un LLM'den istediği
    # `kap_category` alanına düşülür. KAP-dışı haberlerde HER ZAMAN None.
    kap_category: str | None = None
    # SADECE KAP kaynaklı gruplarda anlamlı (bkz. src/summarizer.py >
    # _KAP_SHORT_SUMMARY_INSTRUCTION, kullanıcı isteği 2026-08-18): dashboard
    # KAP kartlarının ana/vurgulu satırı için "[TICKER]: [somut eylem/sonuç]"
    # formatında kısa özet - uzun resmi başlığın YERİNE gösterilir, orijinal
    # başlık+tam özet kart detayında kalır. Ticker LLM'e prompt'ta zaten
    # verildiğinden (kap_stock_codes'tan, bkz. _kap_primary_ticker_code)
    # LLM sadece cümleyi kurar. KAP-dışı haberlerde HER ZAMAN None.
    short_summary: str | None = None
    # TradingView entegrasyonu (2026-08-19, kullanıcı isteği): haberin/KAP
    # bildiriminin en doğru TradingView sembolü, TradingView'in kendi
    # "BORSA:SEMBOL" formatında (ör. "NASDAQ:NFLX", "FX:USDJPY", "TVC:GOLD").
    # LLM tarafından AYNI özetleme çağrısında üretilir (bkz. summarizer.py >
    # SYSTEM_PROMPT/KAP_SYSTEM_PROMPT) - ek bir API çağrısı YOK. Haber somut
    # bir sembolle ilişkilendirilemiyorsa (genel/soyut bir konu) None kalır -
    # LLM'in UYDURMA sembol üretmesi kesinlikle istenmez (bkz.
    # _parse_trading_view_symbol, format doğrulaması yapar ama içeriğin
    # GERÇEKTEN var olan bir sembol olduğunu garanti EDEMEZ - bu yüzden
    # prompt'ta "emin değilsen boş bırak" talimatı var).
    trading_view_symbol: str | None = None
    # TradingView sembol doğrulaması (2026-08-19, kullanıcı geri bildirimi:
    # bazı KAP kayıtlarında "Teknik Görünüm" butonu TradingView'de
    # GERÇEKTEN var olmayan bir sembole gidiyordu) - bkz. src/tradingview.py >
    # validate_symbol. None = henüz doğrulanamadı (ağ hatası/zaman aşımı,
    # bkz. o modülün docstring'i) - dashboard bu durumda butonu GÖSTERMEZ
    # ama kaydı kalıcı olarak "geçersiz" işaretlemez.
    trading_view_symbol_valid: bool | None = None

    @property
    def representative(self) -> NewsItem:
        return self.items[0]

    @property
    def sources(self) -> list[str]:
        # Sırayı koruyarak tekrarsız kaynak listesi
        seen: dict[str, None] = {}
        for item in self.items:
            seen.setdefault(item.source, None)
        return list(seen.keys())

    @property
    def latest_published_at(self) -> datetime | None:
        dates = [i.published_at for i in self.items if i.published_at is not None]
        return max(dates) if dates else None

    @property
    def image_url(self) -> str:
        """Gruptaki (genelde birleştirilmiş birden fazla kaynaktan) ilk
        dolu `image_url`'i döner - `representative` (items[0]) görselsiz
        ama grup başka bir kaynaktan birleştiyse o kaynağın görseli
        kullanılabilsin diye TÜM item'lar taranır (bkz. src/db.py >
        upsert_group)."""
        for item in self.items:
            if item.image_url:
                return item.image_url
        return ""
