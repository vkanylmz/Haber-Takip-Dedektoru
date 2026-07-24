"""Farklı kaynaklardaki aynı konulu haberleri gruplayan basit tekilleştirici.

Yaklaşım: başlıkların karakter bazlı benzerliğine (difflib.SequenceMatcher)
ve yayın zamanlarının belirli bir pencere içinde olmasına bakarak haberleri
gruplar. Bu, embedding tabanlı bir yaklaşımdan daha basit ama MVP için
yeterlidir; ileride anlam bazlı (semantic) bir yönteme geçilebilir.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from src.models import NewsGroup, NewsItem


def _title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def group_similar_news(
    items: list[NewsItem],
    similarity_threshold: float = 0.55,
    window_hours: int = 12,
) -> list[NewsGroup]:
    """Haberleri, tarihe göre en yeniden en eskiye doğru işleyerek gruplar.

    Her yeni haber, mevcut gruplardan biriyle hem zaman penceresi içinde hem
    de başlık benzerliği eşiği üzerindeyse o gruba eklenir; aksi halde kendi
    grubunu oluşturur.
    """
    now = datetime.now(timezone.utc)

    def sort_key(item: NewsItem) -> datetime:
        return item.published_at or now

    sorted_items = sorted(items, key=sort_key, reverse=True)

    groups: list[NewsGroup] = []
    for item in sorted_items:
        item_time = sort_key(item)
        placed = False
        for group in groups:
            rep = group.representative
            rep_time = sort_key(rep)
            if abs((item_time - rep_time).total_seconds()) > window_hours * 3600:
                continue
            if _title_similarity(item.title, rep.title) >= similarity_threshold:
                group.items.append(item)
                placed = True
                break
        if not placed:
            groups.append(NewsGroup(items=[item]))

    return groups


def sort_groups_by_recency(groups: list[NewsGroup]) -> list[NewsGroup]:
    now = datetime.now(timezone.utc)
    return sorted(
        groups,
        key=lambda g: g.latest_published_at or now,
        reverse=True,
    )
