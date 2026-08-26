"""Detaylı finansal tablolar sayfası (/analiz/{ticker}/finansal, bkz.
src/web/templates/financial_details.html) için hafif, kütüphanesiz grafik
yardımcıları (2026-08-26, kullanıcı isteği: "Fintables'ın kendi detay
sayfası gibi grafik-zengin").

BİLİNÇLİ OLARAK Chart.js/Recharts gibi bir kütüphane KULLANILMADI - proje
zaten bu felsefeyi benimsemiş (bkz. dashboard.html > "Hafif olsun diye
Chart.js gibi bir kütüphane YERİNE düz CSS/HTML" yorumu, mevcut sparkline'lar).
Bu modül AYNI yaklaşımı sürdürür: bar grafikleri düz CSS flexbox/absolute
konumlandırmayla, çizgi grafikleri inline SVG polyline ile - sunucu
tarafında (Jinja render anında) hazır veri/markup üretilir, istemci
tarafında EK bir JS kütüphanesi/çalışma zamanı GEREKMEZ.
"""

from __future__ import annotations

from typing import Any


def format_try(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", ".")


def format_try_short(value: float | int | None) -> str:
    """Bar grafik etiketleri için KISALTILMIŞ TL gösterimi (ör.
    "1,54 Mr") - tam tablolardaki (`format_try`) TAM hassasiyetten FARKLI
    olarak, dar bar sütunlarına sığması için. Sadece görsel kısaltma,
    ALTTAKİ değer HİÇ değişmez/yuvarlanmaz (bkz. `value`, ham veri olarak
    ayrıca saklanır)."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    abs_v = abs(value)
    if abs_v >= 1_000_000_000:
        return f"{sign}{abs_v / 1_000_000_000:.2f}Mr"
    if abs_v >= 1_000_000:
        return f"{sign}{abs_v / 1_000_000:.0f}Mn"
    if abs_v >= 1_000:
        return f"{sign}{abs_v / 1_000:.0f} Bin"
    return f"{sign}{abs_v:.0f}"


def build_bar_series(items: list[tuple[str, float | None]]) -> dict[str, Any]:
    """[(etiket, değer), ...] -> sıfır çizgisine göre konumlanan bar
    grafiği için hazır veri. Pozitif değerler sıfır çizgisinin ÜSTÜNE,
    negatifler ALTINA çizilir (Net Kar gibi zarar dönemleri olabilen
    serilerde doğru görsel için gerekli - bkz. kullanıcı isteği: "pozitif
    mavi/yeşil, negatif kırmızı")."""
    values = [v for _, v in items if v is not None]
    if not values:
        return {"bars": [], "zero_pct": 0.0, "has_data": False}

    max_v = max(values + [0.0])
    min_v = min(values + [0.0])
    span = (max_v - min_v) or 1.0
    zero_pct = (0 - min_v) / span * 100

    bars = []
    for label, v in items:
        if v is None:
            bars.append(
                {"label": label, "value": None, "display": "—", "height_pct": 0.0, "offset_pct": zero_pct, "negative": False}
            )
            continue
        height_pct = abs(v) / span * 100
        negative = v < 0
        offset_pct = (zero_pct - height_pct) if negative else zero_pct
        bars.append(
            {
                "label": label,
                "value": v,
                "display": format_try_short(v),
                "height_pct": round(height_pct, 2),
                "offset_pct": round(offset_pct, 2),
                "negative": negative,
            }
        )
    return {"bars": bars, "zero_pct": round(zero_pct, 2), "has_data": True}


def build_line_svg(
    items: list[tuple[str, float | None]],
    width: int = 320,
    height: int = 90,
    stroke: str = "#f472b6",
) -> str | None:
    """[(etiket, değer), ...] -> hazır <svg> polyline markup'ı (mevcut
    dashboard.html sparkline'larıyla AYNI teknik: viewBox +
    preserveAspectRatio="none", bkz. modül docstring'i). En az 2 GERÇEK
    (None olmayan) değer yoksa None döner - çağıran taraf "veri yok" gösterir,
    UYDURMA/enterpolasyon YAPILMAZ."""
    values = [v for _, v in items if v is not None]
    if len(values) < 2:
        return None

    max_v, min_v = max(values), min(values)
    span = (max_v - min_v) or 1.0
    n = len(items)
    pad = 8.0
    plot_w = width - 2 * pad
    plot_h = height - 2 * pad
    step = plot_w / (n - 1) if n > 1 else 0.0

    points: list[str] = []
    circles: list[str] = []
    for i, (_label, v) in enumerate(items):
        if v is None:
            continue
        x = pad + i * step
        y = pad + (1 - (v - min_v) / span) * plot_h
        points.append(f"{x:.1f},{y:.1f}")
        circles.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{stroke}" />')

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'preserveAspectRatio="none" role="img">'
        f'<polyline points="{" ".join(points)}" fill="none" stroke="{stroke}" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round" />'
        f"{''.join(circles)}"
        f"</svg>"
    )


def build_stacked_bar(segments: list[tuple[str, float | None, str]]) -> list[dict[str, Any]]:
    """[(etiket, tutar, renk), ...] -> toplam üzerinden yüzde genişlikli
    segment listesi (yatay stacked bar için, bkz. "Kaynak Dağılımı")."""
    total = sum(v for _, v, _c in segments if v is not None) or 1.0
    return [
        {"label": label, "value": v, "display": format_try(v), "pct": round((v or 0.0) / total * 100, 1), "color": color}
        for label, v, color in segments
    ]


def extract_summary_rows(detay: dict[str, Any] | None, kalemler: list[str]) -> list[dict[str, Any]]:
    """`bilanco_detay`/`gelir_tablosu_detay` şeklinden (bkz.
    src/fintables_financials.py > save_financial_snapshot docstring'i)
    istenen kalem adlarını, ilk iki dönem arasındaki % değişimle birlikte
    çıkarır. Kalem tabloda yoksa/veri boşsa SESSİZCE ATLANIR - UYDURMA bir
    satır ÜRETİLMEZ (bkz. kullanıcı isteği: sadece gerçek Fintables verisi)."""
    if not detay:
        return []
    by_kalem = {row["kalem"]: row["degerler"] for row in detay.get("satirlar", [])}
    result = []
    for kalem in kalemler:
        degerler = by_kalem.get(kalem)
        if not degerler:
            continue
        current = degerler[0]
        previous = degerler[1] if len(degerler) > 1 else None
        pct_change = None
        if current is not None and previous not in (None, 0):
            pct_change = (current - previous) / abs(previous) * 100
        result.append(
            {
                "kalem": kalem,
                "display": format_try(current),
                "pct_change": round(pct_change, 1) if pct_change is not None else None,
            }
        )
    return result


def build_ratio_trend(oranlar_serisi: dict[str, Any] | None, kategori: str, oran: str, stroke: str) -> dict[str, Any] | None:
    """`oranlar_serisi` (bkz. save_financial_snapshot docstring'i) içinden
    tek bir oranın 5 dönemlik çizgi grafiğini hazırlar. Oran o şirketin
    şablonunda yoksa (ör. bankacılık şablonu farklı oranlar kullanır) None
    döner - çağıran taraf o kartı hiç göstermez."""
    if not oranlar_serisi:
        return None
    seri = oranlar_serisi.get(kategori, {}).get(oran)
    if not seri:
        return None
    items = list(zip(seri["donemler"], seri["degerler"]))
    svg = build_line_svg(items, stroke=stroke)
    if not svg:
        return None
    latest = next((v for _, v in reversed(items) if v is not None), None)
    return {"oran": oran, "svg": svg, "latest": latest, "latest_display": f"{latest:.2f}" if latest is not None else "—"}
