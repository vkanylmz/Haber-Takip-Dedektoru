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

    block = (
        f"<b>{emoji_prefix}{title}</b>\n"
        f"<i>Kaynak(lar): {sources}</i> — Önem: {score}/5\n"
        f"{summary}\n"
    )
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
