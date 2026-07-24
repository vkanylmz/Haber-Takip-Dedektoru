"""SQLite veritabanı katmanı (SQLAlchemy ile).

Bu modülün var olma amacı iki tanedir:
  1. Haberleri kalıcı hale getirmek (worker ve web sunucusu ayrı süreçler/
     thread'ler olarak çalıştığından, dashboard'un göstereceği veri buradan
     okunur).
  2. Aynı haberin birden fazla kez özetlenmesini/Telegram'a bildirilmesini
     engellemek: her haber grubu, başlığından türetilen kararlı bir
     `group_key` ile tanımlanır; bir çalıştırmada zaten görülmüş bir
     `group_key` tekrar geldiğinde (ör. 30 dakika sonraki bir sonraki
     taramada aynı RSS haberi hâlâ listedeyse), Claude'a tekrar özet/skor
     sorulmaz ve zaten bildirilmişse Telegram'a tekrar gönderilmez.

Not: `group_key`, temsilci haberin normalize edilmiş başlığından üretilir.
Bu, mükemmel olmayan pratik bir yaklaşımdır: aynı olayı farklı kaynaklar
biraz farklı başlıklarla yazarsa (ve dedup grubunun "temsilcisi" çalıştırmalar
arasında değişirse) nadiren aynı hikaye için ikinci bir kayıt oluşabilir.
Bkz. README > Bilinen Kısıtlamalar.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from src.models import NewsGroup

logger = logging.getLogger(__name__)

Base = declarative_base()


class NewsRecord(Base):
    """Özetlenmiş, skorlanmış bir haber grubunun kalıcı kaydı."""

    __tablename__ = "news_records"

    id = Column(Integer, primary_key=True)
    group_key = Column(String(64), unique=True, index=True, nullable=False)

    title = Column(String(1000), nullable=False)
    sources = Column(String(500), nullable=False)  # "Bloomberg HT, CNBC-e"
    links = Column(Text, nullable=False)  # JSON: [{"source": "...", "link": "..."}, ...]

    published_at = Column(DateTime(timezone=True), nullable=True)

    summary = Column(Text, nullable=True)
    key_points = Column(Text, nullable=True)  # JSON string listesi

    importance_score = Column(Integer, nullable=True)  # 1-5
    importance_reason = Column(Text, nullable=True)

    # Haberin içeriğine göre belirlenmiş bölge etiket(ler)i, JSON string listesi:
    # ör. '["turkiye"]' veya '["abd", "avrupa"]' (bkz. src/summarizer.py > VALID_REGIONS).
    # Telegram /turkiye, /abd, /avrupa, /asya komutları bu alana göre filtreler
    # (bkz. src/telegram_bot.py).
    regions = Column(Text, nullable=True)

    # Haberin piyasa/ekonomi açısından etkisi: "pozitif" | "negatif" | "notr"
    # (bkz. src/summarizer.py > VALID_SENTIMENTS). None -> henüz sınıflandırılmadı.
    sentiment = Column(String(16), nullable=True)

    # Haberin finansal/piyasa beklentisini aciklayan tek cumlelik analist yorumu
    market_impact = Column(Text, nullable=True)

    notified = Column(Boolean, nullable=False, default=False)
    notified_at = Column(DateTime(timezone=True), nullable=True)

    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)

    def key_points_list(self) -> list[str]:
        if not self.key_points:
            return []
        try:
            return json.loads(self.key_points)
        except json.JSONDecodeError:
            return []

    def links_list(self) -> list[dict[str, str]]:
        if not self.links:
            return []
        try:
            return json.loads(self.links)
        except json.JSONDecodeError:
            return []

    def regions_list(self) -> list[str]:
        if not self.regions:
            return []
        try:
            return json.loads(self.regions)
        except json.JSONDecodeError:
            return []


class Subscriber(Base):
    """Botla konuşup (/start ile) haber bildirimlerine abone olmuş bir kullanıcı.

    Her yeni önemli haber, bu tablodaki TÜM chat_id'lere gönderilir (bkz.
    src/notifier.py). Kullanıcı /stop yazarsa satırı silinir.
    """

    __tablename__ = "subscribers"

    id = Column(Integer, primary_key=True)
    chat_id = Column(String(64), unique=True, index=True, nullable=False)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    subscribed_at = Column(DateTime(timezone=True), nullable=False)


class KeywordSubscription(Base):
    """Bir kullanıcının /takip <kelime> ile eklediği bir anahtar kelime/varlık
    takibi. Aynı kullanıcı aynı kelimeyi (case-insensitive) birden fazla
    ekleyemez - bu uygulama katmanında `add_keyword_subscription` içinde
    kontrol edilir (bkz. NOT altta)."""

    __tablename__ = "keyword_subscriptions"

    id = Column(Integer, primary_key=True)
    chat_id = Column(String(64), index=True, nullable=False)
    keyword = Column(String(200), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    # NOT: Case-insensitive tekilliği DB seviyesinde değil (SQLite'ın
    # varsayılan collation'ı case-sensitive olduğundan "Tesla" ve "tesla"
    # burada farklı sayılır) uygulama katmanında (add_keyword_subscription)
    # sağlıyoruz - subscribers/news_records'taki diğer uniqueness kontrolleriyle
    # aynı stil.
    __table_args__ = (UniqueConstraint("chat_id", "keyword", name="uq_keyword_subscription_chat_keyword"),)


class KeywordNotification(Base):
    """Bir (chat_id, group_key) çiftine anahtar kelime eşleşmesi bildirimi
    zaten gönderildiğini işaretler - aynı haber için aynı kullanıcıya birden
    fazla bildirim gitmesini engeller (mevcut NewsRecord.notified mantığına
    benzer, ama kullanıcı+haber bazında, bkz. src/keyword_alerts.py)."""

    __tablename__ = "keyword_notifications"

    id = Column(Integer, primary_key=True)
    chat_id = Column(String(64), index=True, nullable=False)
    group_key = Column(String(64), index=True, nullable=False)
    notified_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (UniqueConstraint("chat_id", "group_key", name="uq_keyword_notification_chat_group"),)


# --------------------------------------------------------------------------
# Engine / session yönetimi
# --------------------------------------------------------------------------

_engine = None
_SessionFactory: sessionmaker | None = None


def init_db(db_path: str | Path) -> None:
    """Veritabanı motorunu ve tablolarını hazırlar. Uygulama başlarken (worker
    ve web sunucusu tarafından ayrı ayrı) çağrılması güvenlidir/idempotenttir."""
    global _engine, _SessionFactory

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # check_same_thread=False: worker (arka plan thread'i), Telegram bot
    # dinleyicisi (ayrı bir arka plan thread'i) ve web sunucusu (istek
    # thread'leri) aynı SQLite dosyasına farklı thread'lerden erişir.
    # timeout=30: SQLite bir yazma sırasında dosyayı kilitlediğinde, başka bir
    # thread/bağlantı aynı anda yazmaya çalışırsa varsayılan olarak sadece 5
    # saniye bekleyip "database is locked" hatası fırlatır - worker'ın yoğun
    # yazma trafiği (her tarama turunda ~90 satır upsert) ile bot thread'inin
    # (kullanıcı komutlarını işlerken okuma/yazma yapan) çakışması GERÇEK
    # production'da defalarca bu hataya yol açtı (bkz. data/logs/finans_haber.log)
    # - bazı kullanıcıların komutlarına hiç yanıt gitmemesine, anahtar kelime
    # bildirimlerinin "gönderildi" olarak işaretlenememesine (dolayısıyla
    # tekrar tekrar gönderilmesine) sebep oldu. 30 saniyeye çıkarmak, kısa
    # süreli kilitlenmelerde hata fırlatmak yerine beklenip tekrar denenmesini
    # sağlıyor.
    _engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(_engine)
    _migrate_add_missing_columns(_engine)
    _configure_sqlite_for_concurrency(_engine)
    # expire_on_commit=False: `with get_session() as s:` bloğundan döndürülen
    # nesnelerin (ör. dashboard'a aktarılan kayıtlar), session kapandıktan
    # sonra da alan değerleri okunabilir kalır (DetachedInstanceError almadan).
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
    logger.info("Veritabanı hazır: %s", path)


def _configure_sqlite_for_concurrency(engine) -> None:
    """WAL (Write-Ahead Logging) moduna geçer: varsayılan "rollback journal"
    modunda bir yazma işlemi TÜM veritabanı dosyasını kilitler ve okuyucularla/
    diğer yazıcılarla çakışır; WAL modunda okuyucular yazıcıyı bloklamaz,
    sadece yazıcı-yazıcı çakışması kalır (ki bu da yukarıdaki `timeout=30` +
    `busy_timeout` sayesinde hata yerine bekleme ile çözülür). Bu, worker'ın
    (RSS taraması) ve Telegram bot dinleyicisinin (kullanıcı komutları) aynı
    dosyaya eşzamanlı erişiminde yaşanan "database is locked" hatalarının kök
    nedenini gideriyor. `PRAGMA`'lar bağlantı bazlı olduğundan burada ayrıca
    `busy_timeout` de açıkça set ediliyor (connect_args'taki `timeout` ile
    aynı amaca hizmet eder, ikisi birlikte ek güvence sağlar)."""
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql("PRAGMA busy_timeout=30000")
        conn.commit()


def _migrate_add_missing_columns(engine) -> None:
    """`Base.metadata.create_all()` var olan tabloları DEĞİŞTİRMEZ, sadece
    eksik tabloları oluşturur. Bu yüzden modele sonradan eklenen kolonlar
    (ör. `regions`, `sentiment`) daha önce oluşturulmuş bir veritabanı
    dosyasında elle eklenmelidir. Basit, idempotent bir "eksikse ADD COLUMN"
    migrasyonu - var olan kayıtlar bozulmadan, yeni kolon(lar) NULL ile eklenir."""
    with engine.begin() as conn:
        existing_columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(news_records)").fetchall()
        }
        if "regions" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN regions TEXT")
            logger.info("Veritabanı migrasyonu: news_records.regions kolonu eklendi.")
        if "sentiment" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN sentiment VARCHAR(16)")
            logger.info("Veritabanı migrasyonu: news_records.sentiment kolonu eklendi.")
        if "market_impact" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN market_impact TEXT")
            logger.info("Veritabanı migrasyonu: news_records.market_impact kolonu eklendi.")


@contextmanager
def get_session() -> Iterator[Session]:
    if _SessionFactory is None:
        raise RuntimeError("init_db() çağrılmadan get_session() kullanılamaz.")
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------
# Grup anahtarı (group_key) hesaplama
# --------------------------------------------------------------------------


def compute_group_key(title: str) -> str:
    """Başlıktan, çalıştırmalar arası kararlı bir kimlik üretir."""
    normalized = re.sub(r"[^\w\s]", "", title.lower(), flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


# --------------------------------------------------------------------------
# Yardımcı sorgu/yazma fonksiyonları
# --------------------------------------------------------------------------


def find_record_by_group_key(session: Session, group_key: str) -> NewsRecord | None:
    return session.query(NewsRecord).filter_by(group_key=group_key).one_or_none()


def upsert_group(session: Session, group: NewsGroup, group_key: str) -> NewsRecord:
    """Bir NewsGroup'u veritabanına yazar (yoksa oluşturur, varsa günceller).

    Bu fonksiyon özetleme/skorlama YAPMAZ — `group` üzerindeki alanların
    (summary, key_points, importance_score/reason) çağrıdan önce doldurulmuş
    olması beklenir. Sadece kalıcı hale getirir.
    """
    now = datetime.now(timezone.utc)
    rep = group.representative

    links_payload = [{"source": i.source, "link": i.link} for i in group.items]
    sources_str = ", ".join(group.sources)

    record = find_record_by_group_key(session, group_key)
    if record is None:
        record = NewsRecord(
            group_key=group_key,
            title=rep.title,
            sources=sources_str,
            links=json.dumps(links_payload, ensure_ascii=False),
            published_at=group.latest_published_at,
            summary=group.summary,
            key_points=json.dumps(group.key_points, ensure_ascii=False),
            importance_score=group.importance_score,
            importance_reason=group.importance_reason,
            regions=json.dumps(group.regions, ensure_ascii=False),
            sentiment=group.sentiment,
            market_impact=group.market_impact,
            notified=False,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(record)
    else:
        record.sources = sources_str
        record.links = json.dumps(links_payload, ensure_ascii=False)
        record.published_at = group.latest_published_at or record.published_at
        record.summary = group.summary or record.summary
        record.key_points = json.dumps(group.key_points, ensure_ascii=False) if group.key_points else record.key_points
        record.importance_score = group.importance_score if group.importance_score is not None else record.importance_score
        record.importance_reason = group.importance_reason or record.importance_reason
        record.regions = json.dumps(group.regions, ensure_ascii=False) if group.regions else record.regions
        record.sentiment = group.sentiment or record.sentiment
        record.market_impact = group.market_impact or record.market_impact
        record.last_seen_at = now

    session.flush()
    return record


def mark_notified(group_key: str) -> None:
    """Kendi kısa ömürlü session'ını açıp/kapatır (çağıranın uzun süredir
    açık bir session'ı olması gerekmez - bkz. src/main.py > _persist_and_notify_single
    ve o fonksiyonun neden artık tek bir paylaşılan session kullanmadığına
    dair not)."""
    with get_session() as session:
        record = find_record_by_group_key(session, group_key)
        if record is None:
            return
        record.notified = True
        record.notified_at = datetime.now(timezone.utc)
        session.flush()


def get_recent_records(
    session: Session,
    limit: int = 100,
    source_filter: str | None = None,
) -> list[NewsRecord]:
    query = session.query(NewsRecord)
    if source_filter:
        query = query.filter(NewsRecord.sources.contains(source_filter))
    query = query.order_by(
        NewsRecord.published_at.desc().nullslast(),
        NewsRecord.last_seen_at.desc(),
    )
    return query.limit(limit).all()


def get_records_since(since: datetime) -> list[NewsRecord]:
    """`since` zamanından bu yana görülmüş (first_seen_at >= since) TÜM
    haberleri döner (bölge/önem skoru filtresi yok) — günlük özet raporu
    için kullanılır (bkz. src/daily_digest.py)."""
    with get_session() as session:
        return (
            session.query(NewsRecord)
            .filter(NewsRecord.first_seen_at >= since)
            .order_by(NewsRecord.importance_score.desc().nullslast(), NewsRecord.first_seen_at.desc())
            .all()
        )


def get_records_since_by_region(region: str, since: datetime) -> list[NewsRecord]:
    """`since` zamanından bu yana görülmüş (first_seen_at >= since) VE verilen
    bölgeye ("turkiye", "abd", "avrupa", "asya", "diger") etiketlenmiş TÜM
    haberleri döner — önem skorundan bağımsız (Telegram /turkiye, /abd,
    /avrupa, /asya komutları için, bkz. src/telegram_bot.py).

    `regions` kolonu JSON string listesi olarak tutulduğundan (bkz.
    NewsRecord.regions_list), bu ölçekte basitçe Python tarafında filtreleniyor.
    """
    region_norm = region.strip().lower()
    with get_session() as session:
        records = (
            session.query(NewsRecord)
            .filter(NewsRecord.first_seen_at >= since)
            .order_by(NewsRecord.published_at.desc().nullslast(), NewsRecord.first_seen_at.desc())
            .all()
        )
    return [r for r in records if region_norm in r.regions_list()]


# --------------------------------------------------------------------------
# Aboneler (Telegram) — src/telegram_bot.py ve src/notifier.py tarafından kullanılır
# --------------------------------------------------------------------------


def add_subscriber(chat_id: str | int, username: str | None = None, first_name: str | None = None) -> bool:
    """Yeni bir Telegram abonesi ekler. `chat_id` zaten kayıtlıysa (UNIQUE)
    hiçbir şey yapmaz. Döner: yeni eklendiyse True, zaten aboneyse False."""
    chat_id_str = str(chat_id)
    with get_session() as session:
        existing = session.query(Subscriber).filter_by(chat_id=chat_id_str).one_or_none()
        if existing is not None:
            return False
        session.add(
            Subscriber(
                chat_id=chat_id_str,
                username=username,
                first_name=first_name,
                subscribed_at=datetime.now(timezone.utc),
            )
        )
        logger.info("Yeni Telegram abonesi kaydedildi: %s (%s)", chat_id_str, first_name or username or "isimsiz")
        return True


def remove_subscriber(chat_id: str | int) -> bool:
    """Bir aboneyi siler (ör. /stop komutu veya botu engellemiş bir kullanıcı
    için hata izolasyonu). Döner: silindiyse True, zaten abone değilse False."""
    chat_id_str = str(chat_id)
    with get_session() as session:
        existing = session.query(Subscriber).filter_by(chat_id=chat_id_str).one_or_none()
        if existing is None:
            return False
        session.delete(existing)
        logger.info("Telegram abonesi silindi: %s", chat_id_str)
        return True


def get_all_subscriber_chat_ids(session: Session | None = None) -> list[str]:
    """Bildirim gönderilecek tüm chat_id'leri döner."""
    if session is not None:
        return [chat_id for (chat_id,) in session.query(Subscriber.chat_id).all()]
    with get_session() as s:
        return [chat_id for (chat_id,) in s.query(Subscriber.chat_id).all()]


# --------------------------------------------------------------------------
# Anahtar kelime/varlık takibi (Telegram /takip, /takiplerim, /takipsil) —
# src/telegram_bot.py ve src/keyword_alerts.py tarafından kullanılır
# --------------------------------------------------------------------------


def add_keyword_subscription(chat_id: str | int, keyword: str) -> bool:
    """Bir kullanıcının takip listesine kelime ekler (case-insensitive
    tekillik kontrolü ile). Döner: yeni eklendiyse True, zaten takip
    ediliyorsa/kelime boşsa False."""
    chat_id_str = str(chat_id)
    keyword_clean = keyword.strip()
    if not keyword_clean:
        return False
    keyword_lower = keyword_clean.lower()
    with get_session() as session:
        existing = session.query(KeywordSubscription).filter_by(chat_id=chat_id_str).all()
        if any(k.keyword.strip().lower() == keyword_lower for k in existing):
            return False
        session.add(
            KeywordSubscription(
                chat_id=chat_id_str,
                keyword=keyword_clean,
                created_at=datetime.now(timezone.utc),
            )
        )
        logger.info("Yeni anahtar kelime takibi: chat_id=%s, keyword=%r", chat_id_str, keyword_clean)
        return True


def remove_keyword_subscription(chat_id: str | int, keyword: str) -> bool:
    """Bir kelimeyi takip listesinden çıkarır (case-insensitive eşleşme).
    Döner: silindiyse True, zaten takip edilmiyorsa False."""
    chat_id_str = str(chat_id)
    keyword_lower = keyword.strip().lower()
    with get_session() as session:
        candidates = session.query(KeywordSubscription).filter_by(chat_id=chat_id_str).all()
        target = next((k for k in candidates if k.keyword.strip().lower() == keyword_lower), None)
        if target is None:
            return False
        session.delete(target)
        logger.info("Anahtar kelime takibi silindi: chat_id=%s, keyword=%r", chat_id_str, keyword.strip())
        return True


def get_keywords_for_chat(chat_id: str | int) -> list[str]:
    """Bir kullanıcının takip ettiği tüm kelimeleri (eklenme sırasına göre) döner."""
    chat_id_str = str(chat_id)
    with get_session() as session:
        rows = (
            session.query(KeywordSubscription)
            .filter_by(chat_id=chat_id_str)
            .order_by(KeywordSubscription.created_at)
            .all()
        )
        return [row.keyword for row in rows]


def get_all_keyword_subscriptions() -> list[KeywordSubscription]:
    """Eşleşme kontrolü için TÜM kullanıcıların TÜM takip ettiği kelimelerini
    döner (bkz. src/keyword_alerts.py > check_keyword_matches_and_notify)."""
    with get_session() as session:
        return session.query(KeywordSubscription).all()


def was_keyword_notified(chat_id: str | int, group_key: str) -> bool:
    """Bu (chat_id, group_key) çiftine daha önce anahtar kelime bildirimi
    gönderilip gönderilmediğini kontrol eder (tekrar bildirim önleme)."""
    chat_id_str = str(chat_id)
    with get_session() as session:
        return (
            session.query(KeywordNotification)
            .filter_by(chat_id=chat_id_str, group_key=group_key)
            .one_or_none()
            is not None
        )


def mark_keyword_notified(chat_id: str | int, group_key: str) -> None:
    """(chat_id, group_key) çiftini "bildirildi" olarak işaretler (idempotent)."""
    chat_id_str = str(chat_id)
    with get_session() as session:
        existing = (
            session.query(KeywordNotification).filter_by(chat_id=chat_id_str, group_key=group_key).one_or_none()
        )
        if existing is not None:
            return
        session.add(
            KeywordNotification(
                chat_id=chat_id_str,
                group_key=group_key,
                notified_at=datetime.now(timezone.utc),
            )
        )


def get_distinct_sources(session: Session) -> list[str]:
    """Dashboard'daki kaynak filtresi için tekil kaynak adlarını çıkarır.

    `sources` alanı "Bloomberg HT, CNBC-e" gibi virgülle ayrılmış bir metin
    olarak tutulduğundan, tüm kayıtları tarayıp kendimiz ayrıştırıyoruz."""
    names: set[str] = set()
    for (sources_str,) in session.query(NewsRecord.sources).all():
        for name in sources_str.split(","):
            name = name.strip()
            if name:
                names.add(name)
    return sorted(names)
