"""Telegram HTML mesaj formatlama yardımcıları.

`src/notifier.py` (anlık eşik bildirimi), `src/telegram_bot.py` (bölge
komutları), `src/daily_digest.py` (günlük özet) ve `src/keyword_alerts.py`
(anahtar kelime bildirimi) hepsi "bir `NewsRecord`'u HTML mesaj bloğuna
çevir" ve "çok sayıda bloğu Telegram'ın ~4096 karakter mesaj limitini aşmayan
parçalara böl" işlemlerine ihtiyaç duyar. Tekrarı önlemek için bu ortak
mantık burada toplanır.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from src.summarizer import SECTOR_LABELS

if TYPE_CHECKING:
    from src.db import NewsRecord

# Telegram tek bir mesajda en fazla ~4096 karakter kabul eder.
TELEGRAM_MESSAGE_LIMIT = 4096

# bkz. src/summarizer.py > VALID_SENTIMENTS
SENTIMENT_EMOJI = {"pozitif": "📈", "negatif": "📉", "notr": "➖"}


def sentiment_emoji(sentiment: str | None) -> str:
    """Bilinmeyen/None bir değer için boş string döner (hiçbir yerde emoji
    gösterilmez) - hata fırlatmaz."""
    if not sentiment:
        return ""
    return SENTIMENT_EMOJI.get(sentiment.strip().lower(), "")


def format_news_block(record: "NewsRecord", extra_reason: str | None = None) -> str:
    """Bir `NewsRecord`'u tek bir HTML mesaj bloğuna çevirir: başlık (+ duygu
    emojisi), kaynak(lar) + önem skoru, özet, (varsa) ek gerekçe, linkler.
    `extra_reason`, günlük özet gibi "neden seçildi" bilgisini eklemek için
    kullanılır (bkz. src/daily_digest.py)."""
    title = html.escape(record.title)
    sources = html.escape(record.sources)
    summary = html.escape(record.summary or "")
    score = record.importance_score if record.importance_score is not None else "?"
    emoji = sentiment_emoji(getattr(record, "sentiment", None))
    emoji_prefix = f"{emoji} " if emoji else ""

    links = record.links_list()
    if links:
        link_lines = "\n".join(
            f'• <a href="{html.escape(l["link"])}">{html.escape(l["source"])}</a>' for l in links
        )
    else:
        link_lines = "(link yok)"

    sector_labels = [SECTOR_LABELS.get(s, s) for s in record.sectors_list()]
    sector_line = f"🏭 {html.escape(', '.join(sector_labels))}\n" if sector_labels else ""

    block = (
        f"<b>{emoji_prefix}{title}</b>\n"
        f"<i>Kaynak(lar): {sources}</i> — Önem: {score}/5\n"
        f"{sector_line}"
        f"{summary}\n"
    )
    if getattr(record, 'market_impact', None):
        block += f"\n💡 <b>Piyasa Öngörüsü:</b> {html.escape(record.market_impact)}\n"

    # ÇAPRAZ KAYNAK KARŞILAŞTIRMA (web dashboard'daki "Kaynak Karşılaştırması"
    # panelinin Telegram karşılığı, bkz. src/db.py > NewsRecord.source_comparison_list):
    # birden fazla kaynak varsa, her kaynağın konuyu KENDİ başlığıyla nasıl
    # ele aldığı kısaca listelenir.
    comparison = record.source_comparison_list()
    if comparison:
        compare_lines = "\n🔀 <i>Diğer kaynaklar bu haberi şöyle gördü:</i>\n"
        for c in comparison:
            source_label = html.escape(c["source"])
            title = c.get("title") or ""
            if title:
                compare_lines += f"• <b>{source_label}</b>: {html.escape(title)}\n"
            else:
                compare_lines += f"• <b>{source_label}</b>: (başlık henüz saklanmamış)\n"
        block += compare_lines

    if extra_reason:
        block += f"<i>Neden önemli: {html.escape(extra_reason)}</i>\n"
    block += link_lines
    return block


def chunk_messages(header: str, blocks: list[str], limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Bir başlık + haber bloklarını, her biri `limit` karakteri aşmayan bir
    veya daha fazla mesaja gruplar (tek bir blok tek başına limiti aşıyorsa
    sığacak şekilde kırpılır)."""
    messages: list[str] = []
    current = header
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit:
            if current:
                messages.append(current)
            current = block if len(block) <= limit else block[: limit - 20] + "\n...(kırpıldı)"
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages
