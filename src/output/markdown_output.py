"""Rapor çıktısını bir Markdown dosyasına yazar (gereksinim #5, ikinci adım)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.models import NewsGroup


def _format_date(group: NewsGroup) -> str:
    dt = group.latest_published_at
    if dt is None:
        return "_tarih bilinmiyor_"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def write_markdown(groups: list[NewsGroup], output_dir: str | Path) -> Path:
    reports_dir = Path(output_dir) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    report_path = reports_dir / f"{now.strftime('%Y%m%d_%H%M%S')}.md"

    lines: list[str] = []
    lines.append(f"# Finansal Haber Özeti — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append(f"Toplam {len(groups)} konu bulundu.")
    lines.append("")

    for group in groups:
        rep = group.representative
        sources_str = ", ".join(group.sources)
        lines.append(f"## {rep.title}")
        lines.append("")
        lines.append(f"**Tarih:** {_format_date(group)}  ")
        lines.append(f"**Kaynak(lar):** {sources_str}")
        lines.append("")
        if group.importance_score is not None:
            flag = " 🚨" if group.importance_score >= 4 else ""
            lines.append(f"**Önem Skoru:** {group.importance_score}/5{flag} — {group.importance_reason}")
            lines.append("")
        lines.append(f"**Özet:** {group.summary or '(özet yok)'}")
        lines.append("")
        if group.key_points:
            lines.append("**Önemli noktalar:**")
            for point in group.key_points:
                lines.append(f"- {point}")
            lines.append("")
        lines.append("**Linkler:**")
        for item in group.items:
            lines.append(f"- [{item.source}]({item.link})")
        lines.append("")
        lines.append("---")
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path
