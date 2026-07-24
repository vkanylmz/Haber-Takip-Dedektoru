"""Terminale okunabilir, sade bir rapor basar (gereksinim #5, birinci adım)."""

from __future__ import annotations

from src.models import NewsGroup

_WIDTH = 78


def _format_date(group: NewsGroup) -> str:
    dt = group.latest_published_at
    if dt is None:
        return "tarih bilinmiyor"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def print_report(groups: list[NewsGroup]) -> None:
    if not groups:
        print("Gösterilecek haber bulunamadı.")
        return

    print("=" * _WIDTH)
    print(f" FİNANSAL HABER ÖZETİ  —  {len(groups)} konu")
    print("=" * _WIDTH)

    for idx, group in enumerate(groups, start=1):
        rep = group.representative
        sources_str = ", ".join(group.sources)
        print()
        score_str = f"{group.importance_score}/5" if group.importance_score is not None else "?"
        flag = " 🚨" if (group.importance_score or 0) >= 4 else ""
        print(f"[{idx}] {_format_date(group)}  |  Kaynak(lar): {sources_str}  |  Önem: {score_str}{flag}")
        print(f"    {rep.title}")
        print("-" * _WIDTH)
        if group.importance_reason:
            print(f"Önem gerekçesi: {group.importance_reason}")
        print(f"Özet: {group.summary or '(özet yok)'}")
        if group.key_points:
            print("Önemli noktalar:")
            for point in group.key_points:
                print(f"  - {point}")
        print("Linkler:")
        for item in group.items:
            print(f"  - {item.source}: {item.link}")
        print("=" * _WIDTH)
