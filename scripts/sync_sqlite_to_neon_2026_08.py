"""TEK SEFERLİK KULLANIM İÇİN YAZILMIŞTIR - kalıcı bir özellik/otomasyon
DEĞİLDİR, tekrar tekrar çalıştırılmak üzere tasarlanmamıştır.

Bağlam (2026-08-18): Neon Postgres'in ücretsiz veri transfer kotası
2026-07-31'de aşıldığı için `DATABASE_URL` `.env`'de yorum satırına alınmış,
uygulama o tarihten beri yerel SQLite'a (data/finans_haber.db) yazıyordu.
Kota artık sıfırlanmış ve proje aktif (bkz. sohbet, Neon API ile doğrulandı,
2026-08-18). Bu script, offline geçen ~18 günde SQLite'a yazılmış YENİ
satırları Neon'a taşır - `DATABASE_URL` tekrar açılmadan ÖNCE çalıştırılmalı
(README > "Neon Kota Aşımı Sonrası Geçiş" bölümündeki plan).

Ne YAPAR:
  - SQLite (yerel) ve Neon (uzak) için AYRI iki engine açar (init_db()'nin
    global tek-engine state'ini KASITLI OLARAK kullanmaz - aynı anda hem
    okuma hem yazma hedefine ihtiyaç var).
  - Neon'da şema eksikse oluşturur (`Base.metadata.create_all` - idempotent,
    var olan tabloya dokunmaz).
  - Şu tablolar için SQLite'daki TÜM satırları Neon'a "INSERT ... ON
    CONFLICT DO NOTHING" ile dener: news_records (anahtar: group_key),
    subscribers (chat_id), keyword_subscriptions (chat_id+keyword),
    keyword_notifications (chat_id+group_key), app_state (key).
  - Her tablo için ÖNCE/SONRA Neon satır sayısını karşılaştırıp gerçekten
    kaç satır eklendiğini raporlar.

Ne YAPMAZ (bilinçli olarak, README'nin kendi tasarımıyla AYNI):
  - Var olan HİÇBİR Neon satırını GÜNCELLEMEZ/ÜZERİNE YAZMAZ/SİLMEZ - SADECE
    eksik olanı EKLER (ON CONFLICT DO NOTHING - tek yönlü, ek-sadece senkron).
  - `source_health`'i taşımaz (kullanıcı kararı - zaten anlık bir durum
    önbelleği, worker Neon'a geçer geçmez kendiliğinden dolar).
  - `push_subscriptions`/`push_notifications`/`api_keys`'e dokunmaz (ikisi de
    şu an 0 satır, kapsam dışı).
  - `.env`'deki `DATABASE_URL`'i AÇMAZ - bu script SADECE veriyi taşır, canlı
    geçişi TETİKLEMEZ (bilerek ayrı bir adım, kontrolü elden bırakmamak için).

Kullanım: proje kökünden
    .venv\\Scripts\\python.exe scripts\\sync_sqlite_to_neon_2026_08.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from src.db import (
    AppState,
    Base,
    KeywordNotification,
    KeywordSubscription,
    NewsRecord,
    Subscriber,
)

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
SQLITE_PATH = Path(__file__).resolve().parent.parent / "data" / "finans_haber.db"

# (Model, unique index kolonları) - ON CONFLICT hedefi bu kolonlara göre
# belirlenir, model tanımlarındaki UniqueConstraint/unique=True ile BİREBİR
# aynı olmalı (bkz. src/db.py).
_SYNC_TABLES = [
    (NewsRecord, ["group_key"]),
    (Subscriber, ["chat_id"]),
    (KeywordSubscription, ["chat_id", "keyword"]),
    (KeywordNotification, ["chat_id", "group_key"]),
    (AppState, ["key"]),
]


def _read_neon_url() -> str:
    """.env'deki YORUM SATIRINA ALINMIŞ `# DATABASE_URL=...` satırından
    connection string'i çıkarır - bu script çalıştığında henüz canlı
    geçiş YAPILMAMIŞ olmalı (bkz. modül docstring'i)."""
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"#\s*DATABASE_URL=(.+)", line.strip())
            if m:
                return m.group(1).strip()
    raise RuntimeError(".env içinde yorum satırına alınmış bir DATABASE_URL bulunamadı.")


def _row_to_dict(model, row) -> dict:
    """SQLAlchemy satırını dict'e çevirir - `id` kolonu (varsa) BİLEREK
    HARİÇ TUTULUR: bu, otomatik artan (autoincrement) bir yüzey anahtarı,
    iş anlamı taşımaz ve SQLite ile Neon'un id SAYAÇLARI TAMAMEN BAĞIMSIZ
    (ikisi de 1'den başlar) - aynı id değerlerini AÇIKÇA INSERT etmeye
    çalışmak, hedef conflict kolonumuz (group_key/chat_id/... - bkz.
    _SYNC_TABLES) ÇAKIŞMASA BİLE Neon'daki AYRI birincil anahtar kısıtını
    ihlal edip `duplicate key value violates unique constraint
    "..._pkey"` hatası fırlatır (bkz. ON CONFLICT DO NOTHING SADECE
    belirtilen index_elements için geçerlidir, TABLONUN BAŞKA unique/
    primary key kısıtları için DEĞİL) - bu script'in ilk çalıştırılışında
    TAM OLARAK bu hatayla karşılaşıldı, canlı testte doğrulandı. `id`
    hariç tutulunca Postgres kendi sekansından YENİ bir id atar - hiçbir
    satırın iş mantığı (group_key, chat_id vb.) bundan etkilenmez."""
    return {col.name: getattr(row, col.name) for col in model.__table__.columns if col.name != "id"}


def main() -> None:
    neon_url = _read_neon_url()

    sqlite_engine = create_engine(f"sqlite:///{SQLITE_PATH}")
    neon_engine = create_engine(neon_url, pool_pre_ping=True)

    print("Neon'da şema kontrol ediliyor/oluşturuluyor (idempotent)...")
    Base.metadata.create_all(neon_engine)

    SqliteSession = sessionmaker(bind=sqlite_engine)
    NeonSession = sessionmaker(bind=neon_engine)

    print()
    print(f"{'Tablo':<24} {'SQLite':>8} {'Neon(önce)':>12} {'Neon(sonra)':>12} {'Eklenen':>10}")

    sqlite_session = SqliteSession()
    neon_session = NeonSession()
    try:
        for model, unique_cols in _SYNC_TABLES:
            table_name = model.__tablename__

            rows = sqlite_session.query(model).all()
            payload = [_row_to_dict(model, r) for r in rows]

            before_count = neon_session.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar()

            if payload:
                stmt = pg_insert(model).values(payload).on_conflict_do_nothing(index_elements=unique_cols)
                neon_session.execute(stmt)
                neon_session.commit()

            after_count = neon_session.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar()

            print(
                f"{table_name:<24} {len(rows):>8} {before_count:>12} {after_count:>12} "
                f"{after_count - before_count:>10}"
            )
    finally:
        sqlite_session.close()
        neon_session.close()

    print()
    print("Senkronizasyon tamamlandı. DATABASE_URL henüz AÇILMADI - bu script sadece veriyi taşıdı.")


if __name__ == "__main__":
    main()
