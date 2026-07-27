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
