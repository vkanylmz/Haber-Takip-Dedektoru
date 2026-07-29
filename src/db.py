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
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy import inspect as sa_inspect
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

    # `title` yabancı dilde (ör. İngilizce kaynak) ise LLM tarafından üretilen
    # Türkçe çevirisi (bkz. src/summarizer.py > SYSTEM_PROMPT > title_tr).
    # `title` zaten Türkçe ise (ör. Bloomberg HT, CNBC-e, Ekonomim.com gibi
    # Türk kaynaklardan gelen haberler) None kalır - gösterim tarafı (bkz.
    # dashboard.html/detayli_inceleme.html/telegram_format.py) bu durumda
    # parantez içi çeviri EKLEMEZ. Bu özellik eklenmeden ÖNCE özetlenmiş
    # eski kayıtlarda da None kalır (geriye dönük yeniden özetleme YAPILMAZ -
    # gereksiz LLM maliyeti oluşturmamak için kasıtlı).
    title_tr = Column(Text, nullable=True)

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

    # Haberin ilgili olduğu sektör etiket(ler)i, JSON string listesi:
    # ör. '["finans"]' veya '["enerji", "otomotiv"]' (bkz.
    # src/summarizer.py > VALID_SECTORS). Dashboard'daki sektör filtresi bu
    # alana göre çalışır (bkz. src/web/app.py).
    sector = Column(Text, nullable=True)

    # Haberin piyasa/ekonomi açısından etkisi: "pozitif" | "negatif" | "notr"
    # (bkz. src/summarizer.py > VALID_SENTIMENTS). None -> henüz sınıflandırılmadı.
    sentiment = Column(String(16), nullable=True)

    # Haberin finansal/piyasa beklentisini aciklayan tek cumlelik analist yorumu
    market_impact = Column(Text, nullable=True)

    # "Detaylı İnceleme" sayfasındaki (bkz. src/web/app.py) TEK bir üst
    # kategori: "makro" | "sirket" | "siyasi" | "diger" (bkz.
    # src/summarizer.py > VALID_TOP_CATEGORIES). `sector`/`regions`'tan
    # BAĞIMSIZ bir sınıflandırma ekseni. None -> henüz sınıflandırılmadı
    # (ör. bu özellik eklenmeden önce özetlenmiş eski bir haber).
    top_category = Column(String(16), nullable=True)

    # Yalnızca top_category="sirket" olan kayıtlarda anlamlı: "BORSA: SEMBOL"
    # formatında (ör. "NASDAQ: TSLA", "BIST: THYAO") - bkz.
    # src/summarizer.py > _parse_company_ticker. None -> net bir şirket/ticker
    # belirlenemedi ya da bu özellik eklenmeden önce özetlenmiş eski bir haber.
    company_ticker = Column(String(64), nullable=True)

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

    def sectors_list(self) -> list[str]:
        if not self.sector:
            return []
        try:
            return json.loads(self.sector)
        except json.JSONDecodeError:
            return []

    def source_comparison_list(self) -> list[dict[str, str]]:
        """ÇAPRAZ KAYNAK KARŞILAŞTIRMA: bu haber grubunda birden fazla FARKLI
        kaynak varsa (dedup/group_key mantığıyla birleştirilmiş), her
        kaynağın konuyu KENDİ orijinal başlığı/özetiyle nasıl ele aldığını
        döner - bkz. upsert_group'taki links_payload (title/snippet alanları).
        Web dashboard'daki "Kaynak Karşılaştırması" paneli VE Telegram mesaj
        formatlayıcısı (bkz. src/telegram_format.py > format_news_block) AYNI
        bu metodu kullanır - ikisi arasında tutarsızlık olmaz.

        Yalnızca GERÇEKTEN birden fazla farklı kaynak varsa dolu bir liste
        döner, aksi halde boş liste (gösterilecek bir karşılaştırma yoktur)."""
        links = self.links_list()
        distinct_sources = {item.get("source") for item in links if item.get("source")}
        if len(distinct_sources) < 2:
            return []

        seen: set[str] = set()
        comparison: list[dict[str, str]] = []
        for item in links:
            source = item.get("source")
            if not source or source in seen:
                continue
            seen.add(source)
            # Eski kayıtlarda (bu özellik eklenmeden önce yazılmış) "title"/
            # "snippet" alanları olmayabilir - bu durumda boş string döner
            # (çağıran taraf bunu "henüz saklanmamış" olarak gösterir).
            comparison.append(
                {
                    "source": source,
                    "title": item.get("title") or "",
                    "snippet": item.get("snippet") or "",
                    "link": item.get("link") or "",
                }
            )
        return comparison


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

    # Kullanıcının KENDİ önem eşiği (1-5, bkz. Telegram /esik komutu). Bir
    # haberin importance_score'u bu değere eşit veya büyükse bildirim gider;
    # global config.yaml > importance.threshold artık yalnızca yeni abonelerin
    # VARSAYILAN eşiği olarak kullanılır (bkz. src/notifier.py).
    importance_threshold = Column(Integer, nullable=False, default=4)


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


class SourceHealth(Base):
    """Bir fetcher'ın (RSS/scrape/lisanslı agregatör) TEK BİR çalıştırma
    denemesinin kaydı - kaynak sağlık paneli için (bkz. src/main.py >
    fetch_source, web/app.py > /kaynak-sagligi). Her tarama turunda, her
    kaynak için (başarılı da olsa başarısız da olsa) bir satır eklenir."""

    __tablename__ = "source_health"

    id = Column(Integer, primary_key=True)
    source = Column(String(255), index=True, nullable=False)
    ran_at = Column(DateTime(timezone=True), nullable=False, index=True)
    success = Column(Boolean, nullable=False)
    item_count = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)


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


class AppState(Base):
    """Genel amaçlı, tek satırlık key-value durum saklama tablosu. İLK
    kullanım amacı: haftalık emtia raporunun (bkz. src/commodity_report.py >
    Faz 2) son hesaplanmış sonucunu (LLM analizi DAHİL) önbelleğe almak -
    dashboard paneli her AJAX isteğinde 9 ayrı LLM çağrısı YAPMADAN, Telegram
    raporuyla AYNI (şirket isimleri dahil) veriyi gösterebilsin diye.

    Render'ın DOSYA SİSTEMİ deploy'lar arası KALICI DEĞİLDİR (bkz. Postgres/
    DATABASE_URL notu, init_db) - bu yüzden bir data/state/*.json dosyası
    yerine BİLİNÇLİ OLARAK veritabanı (SQLite yerelde / Postgres Render'da)
    kullanıldı, ikisinde de aynı şekilde çalışır.

    İleride (bkz. Faz 3 - ani emtia hareketi bildirimi) aynı tablo, hangi
    emtianın en son ne zaman/hangi fiyattan bildirim gönderdiğini (soğuma
    süresi takibi) saklamak için de kullanılabilir - yeni bir tablo/migrasyon
    gerekmeden, sadece yeni bir `key` ile."""

    __tablename__ = "app_state"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)  # JSON string
    updated_at = Column(DateTime(timezone=True), nullable=False)


# --------------------------------------------------------------------------
# Engine / session yönetimi
# --------------------------------------------------------------------------

_engine = None
_SessionFactory: sessionmaker | None = None


def init_db(db_path: str | Path) -> None:
    """Veritabanı motorunu ve tablolarını hazırlar. Uygulama başlarken (worker
    ve web sunucusu tarafından ayrı ayrı) çağrılması güvenlidir/idempotenttir.

    İKİ BACKEND DESTEKLENİR (bkz. README > "Vercel'e Deploy - Hibrit Mimari"):
      1. VARSAYILAN: yerel SQLite dosyası (`db_path` argümanı, config.yaml >
         database.path). Hiçbir env değişkeni ayarlamazsanız davranış BİREBİR
         ÖNCEKİYLE AYNIDIR - geriye dönük tam uyumluluk.
      2. `DATABASE_URL` ortam değişkeni TANIMLIYSA (ör. Neon/Supabase gibi bir
         Postgres bağlantı dizesi): `db_path` TAMAMEN YOK SAYILIR, bunun
         yerine o Postgres veritabanına bağlanılır. Bu, worker'ın/Telegram
         bot'un yerel bilgisayarda çalışmaya devam ederken TÜM verilerini
         paylaşımlı bir buluta yazmasını sağlar - Vercel'deki salt-okunur
         dashboard AYNI `DATABASE_URL`'i kullanarak bu veriyi okur.
    """
    global _engine, _SessionFactory

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url.startswith("postgres://"):
        # Bazı sağlayıcılar (Heroku'nun eski stili, bazen Neon dahil bazı
        # araçlarca üretilen bağlantı dizeleri) "postgres://" şemasını
        # kullanır - SQLAlchemy 2.x bunu artık KABUL ETMEZ, "postgresql://"
        # bekler. Kullanıcının .env'e hangi şemayla yapıştırdığından
        # bağımsız çalışması için burada sessizce normalize ediliyor.
        database_url = "postgresql://" + database_url[len("postgres://"):]

    if database_url:
        # pool_pre_ping=True: Neon'un ücretsiz katmanı 5 dakika hareketsizlik
        # sonrası compute'u "uykuya" alır (scale-to-zero) - sonraki istekte
        # otomatik uyanır ama ARADAKİ bağlantı SQLAlchemy'nin havuzunda "ölü"
        # kalmış olabilir. pool_pre_ping, her bağlantıyı kullanmadan önce
        # hafif bir "SELECT 1" ile test eder, ölüyse sessizce yeniden kurar -
        # bu olmadan "SSL connection has been closed unexpectedly" gibi
        # aralıklı hatalar alınabilirdi.
        # pool_size/max_overflow AÇIKÇA sınırlandırıldı (5 + 5 = en fazla 10
        # eşzamanlı bağlantı) - Render'ın ücretsiz katmanı yalnızca 512MB RAM
        # veriyor (bkz. README > "Çoklu Kullanıcı Performansı"), her açık
        # Postgres bağlantısı hem bu tarafta hem Neon tarafında bellek
        # tüketir. Bu ölçekte (kişisel/az sayıda eşzamanlı kullanıcı) 10
        # bağlantı fazlasıyla yeterli - varsayılan (belirtilmemiş) davranış
        # zaten 5+10=15'e denk geliyordu, burada sadece kasıtlı/açık hale
        # getirildi.
        _engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_recycle=300,
            pool_size=5,
            max_overflow=5,
        )
        Base.metadata.create_all(_engine)
        _migrate_add_missing_columns(_engine)
        _ensure_performance_indexes(_engine)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
        logger.info("Veritabanı hazır (DATABASE_URL üzerinden, Postgres).")
        return

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
    _ensure_performance_indexes(_engine)
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
    migrasyonu - var olan kayıtlar bozulmadan, yeni kolon(lar) NULL ile eklenir.

    `sqlalchemy.inspect()` kullanılır (ham `PRAGMA table_info(...)` YERİNE -
    bu SQLite'a ÖZGÜDÜR, Postgres'te çalışmaz) - böylece bu fonksiyon HER İKİ
    backend'de de (yerel SQLite VEYA DATABASE_URL üzerinden Postgres) aynı
    şekilde çalışır (bkz. init_db)."""
    inspector = sa_inspect(engine)
    with engine.begin() as conn:
        existing_columns = {col["name"] for col in inspector.get_columns("news_records")}
        if "regions" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN regions TEXT")
            logger.info("Veritabanı migrasyonu: news_records.regions kolonu eklendi.")
        if "sentiment" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN sentiment VARCHAR(16)")
            logger.info("Veritabanı migrasyonu: news_records.sentiment kolonu eklendi.")
        if "market_impact" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN market_impact TEXT")
            logger.info("Veritabanı migrasyonu: news_records.market_impact kolonu eklendi.")
        if "sector" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN sector TEXT")
            logger.info("Veritabanı migrasyonu: news_records.sector kolonu eklendi.")
        if "top_category" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN top_category VARCHAR(16)")
            logger.info("Veritabanı migrasyonu: news_records.top_category kolonu eklendi.")
        if "company_ticker" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN company_ticker VARCHAR(64)")
            logger.info("Veritabanı migrasyonu: news_records.company_ticker kolonu eklendi.")
        if "title_tr" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN title_tr TEXT")
            logger.info("Veritabanı migrasyonu: news_records.title_tr kolonu eklendi.")

        existing_subscriber_columns = {col["name"] for col in inspector.get_columns("subscribers")}
        if "importance_threshold" not in existing_subscriber_columns:
            # Var olan aboneler için varsayılan olarak mevcut global eşik (4)
            # kullanılır - config.yaml > importance.threshold değişse bile bu
            # geriye dönük backfill sabit kalır (kullanıcı /esik ile
            # istediğinde kendi değerini ayarlayabilir).
            conn.exec_driver_sql(
                "ALTER TABLE subscribers ADD COLUMN importance_threshold INTEGER NOT NULL DEFAULT 4"
            )
            logger.info("Veritabanı migrasyonu: subscribers.importance_threshold kolonu eklendi.")


def _ensure_performance_indexes(engine) -> None:
    """Dashboard'un sık kullandığı filtre/sıralama alanlarına (bkz.
    get_recent_records) index ekler - performans denetiminde (2026-07)
    gerçek ölçümle doğrulandı: index'siz ana sorgu ~300ms sürüyordu (bkz.
    README > "Dashboard Performansı"). `CREATE INDEX IF NOT EXISTS` HEM
    SQLite HEM Postgres'te aynı sözdizimiyle çalışır - idempotent, var olan
    bir index'e dokunmaz.

    Metin alanlarındaki (`sources`, `sector`, `regions`, `title`, `summary`)
    `.contains()`/`.ilike()` sorguları ("%x%" gibi baştan joker karakterli)
    düz bir B-tree index'ten YARARLANAMAZ - Postgres'te bunun için
    `pg_trgm` uzantısıyla GIN index eklenir (bkz. altta), ki bu SQLite'ta
    karşılığı olmayan Postgres'e özgü bir özelliktir - bu yüzden SADECE
    Postgres bağlantısında uygulanır.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_news_records_first_seen_at ON news_records (first_seen_at)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_news_records_last_seen_at ON news_records (last_seen_at)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_news_records_published_at ON news_records (published_at)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_news_records_sentiment ON news_records (sentiment)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_news_records_top_category ON news_records (top_category)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_news_records_importance_score ON news_records (importance_score)")

        if engine.dialect.name == "postgresql":
            try:
                conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                for column in ("sources", "sector", "regions", "title", "summary"):
                    conn.exec_driver_sql(
                        f"CREATE INDEX IF NOT EXISTS idx_news_records_{column}_trgm "
                        f"ON news_records USING gin ({column} gin_trgm_ops)"
                    )
                logger.info("Veritabanı: pg_trgm GIN index'leri hazır (metin filtreleri/arama için).")
            except Exception:  # noqa: BLE001 - pg_trgm oluşturulamazsa (ör. yetki kısıtı) uygulama YİNE DE çalışsın
                logger.warning(
                    "pg_trgm uzantısı/GIN index'leri oluşturulamadı - metin filtreleri/arama yine de "
                    "çalışır, sadece bu index'ler olmadan (daha yavaş) yürütülür.",
                    exc_info=True,
                )

    logger.info("Veritabanı: performans index'leri hazır.")


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

    # `title` ve `snippet`, dashboard'daki ÇAPRAZ KAYNAK KARŞILAŞTIRMA
    # görünümü için tutuluyor (bkz. src/web/app.py > _build_source_comparison):
    # dedup/gruplama aynı konudaki haberleri TEK bir kayıtta birleştirdiğinden
    # (bkz. group_key), her kaynağın o konuyu KENDİ başlığı/özetiyle nasıl
    # ele aldığı normalde kaybolur - burada saklanarak dashboard'da yan yana
    # gösterilebiliyor. `link`/`source` alanları zaten var olan "Linkler"
    # bölümü tarafından da kullanıldığından GERİYE DÖNÜK UYUMLU (eski
    # şablon kodu bu yeni alanları basitçe yok sayar).
    links_payload = [
        {"source": i.source, "link": i.link, "title": i.title, "snippet": (i.raw_text or "").strip()[:400]}
        for i in group.items
    ]
    sources_str = ", ".join(group.sources)

    record = find_record_by_group_key(session, group_key)
    if record is None:
        record = NewsRecord(
            group_key=group_key,
            title=rep.title,
            title_tr=group.title_tr,
            sources=sources_str,
            links=json.dumps(links_payload, ensure_ascii=False),
            published_at=group.latest_published_at,
            summary=group.summary,
            key_points=json.dumps(group.key_points, ensure_ascii=False),
            importance_score=group.importance_score,
            importance_reason=group.importance_reason,
            regions=json.dumps(group.regions, ensure_ascii=False),
            sector=json.dumps(group.sectors, ensure_ascii=False),
            sentiment=group.sentiment,
            market_impact=group.market_impact,
            top_category=group.top_category,
            company_ticker=group.company_ticker,
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
        record.sector = json.dumps(group.sectors, ensure_ascii=False) if group.sectors else record.sector
        record.sentiment = group.sentiment or record.sentiment
        record.market_impact = group.market_impact or record.market_impact
        record.top_category = group.top_category or record.top_category
        record.company_ticker = group.company_ticker or record.company_ticker
        record.title_tr = group.title_tr or record.title_tr
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
    sector_filter: str | None = None,
    region_filter: str | None = None,
    sentiment_filter: str | None = None,
    min_importance: int | None = None,
    search_query: str | None = None,
    since: datetime | None = None,
    sort_order: str = "newest",
    category_filter: str | None = None,
    additional_sector_filter: str | None = None,
) -> list[NewsRecord]:
    query = session.query(NewsRecord)
    if category_filter:
        # `top_category` sector/regions'ın AKSİNE düz bir string kolon
        # (JSON listesi DEĞİL, TEK bir değer) - bkz. src/summarizer.py >
        # VALID_TOP_CATEGORIES, "Detaylı İnceleme" sayfasının kategori
        # sekmeleri bunu kullanır (bkz. src/web/app.py).
        query = query.filter(NewsRecord.top_category == category_filter)
    if since is not None:
        # Şirket/hisse profili sayfası (bkz. src/company_profile.py) gibi
        # "son N gün" ile sınırlı sorgular için - diğer çağıranlar (ör.
        # dashboard'un ana listesi) bunu hiç kullanmaz, davranışları değişmez.
        query = query.filter(NewsRecord.first_seen_at >= since)
    if source_filter:
        query = query.filter(NewsRecord.sources.contains(source_filter))
    if search_query:
        # Basit case-insensitive LIKE arama (başlık + özet). SQLite'ın
        # varsayılan LIKE'ı ASCII dışı karakterlerde (ör. Türkçe İ/ı) tam
        # case-insensitive değildir, ama pratikte yeterlidir - karmaşık bir
        # full-text-search motoru (FTS5 vb.) KASITLI OLARAK kullanılmadı.
        like_pattern = f"%{search_query.strip()}%"
        query = query.filter(
            (NewsRecord.title.ilike(like_pattern)) | (NewsRecord.summary.ilike(like_pattern))
        )
    if sector_filter:
        # `sector` bir JSON string listesi olarak tutuluyor (ör. '["finans"]');
        # tırnaklarla birlikte arama ("finans" gibi tam eşleşme), "finansal"
        # gibi kısmi bir kelimeyle yanlışlıkla eşleşmeyi önler.
        query = query.filter(NewsRecord.sector.contains(f'"{sector_filter}"'))
    if additional_sector_filter:
        # "Detaylı İnceleme" sayfasındaki sütun bazlı çapraz sektör alt
        # filtresi için (bkz. src/web/app.py > detayli_inceleme_page) - genel
        # `sector_filter` ile AYNI ANDA verilirse (ör. genel filtre "enerji",
        # sütun alt filtresi "teknoloji") ikisi de AYRI birer AND koşulu
        # olarak eklenir, yani haberin sector listesinde HER İKİSİ de olmalı
        # (kesişim boşsa sonuç da boş döner - bu istenen/doğru davranıştır).
        query = query.filter(NewsRecord.sector.contains(f'"{additional_sector_filter}"'))
    if region_filter:
        # `regions` da aynı şekilde JSON string listesi (bkz. yukarıdaki not).
        query = query.filter(NewsRecord.regions.contains(f'"{region_filter}"'))
    if sentiment_filter:
        query = query.filter(NewsRecord.sentiment == sentiment_filter)
    if min_importance is not None:
        query = query.filter(NewsRecord.importance_score >= min_importance)

    # `first_seen_at` (bizim sistemimizin haberi İLK KEZ yakaladığı/özetlediği
    # an) kullanılır - `published_at` (kaynağın kendi bildirdiği yayın tarihi)
    # DEĞİL. İkisi arasında gerçek bir fark var: kaynaklar published_at'i
    # tutarsız/gecikmeli bildirebiliyor (ör. bir kaynak saatler önce
    # yayınlanmış ama bizim sistemimizin AZ ÖNCE keşfettiği bir haberi farklı
    # bir zaman damgasıyla verebiliyor) - bu yüzden "en son gelen/özetlenen
    # haber en üstte" gereksinimi için doğru alan first_seen_at'tir (bkz.
    # dashboard'daki sıralama dropdown'ı, src/web/app.py).
    if sort_order == "oldest":
        query = query.order_by(NewsRecord.first_seen_at.asc())
    else:
        query = query.order_by(NewsRecord.first_seen_at.desc())
    return query.limit(limit).all()


def get_records_by_company_ticker(company_ticker: str, limit: int = 10, since: datetime | None = None) -> list[NewsRecord]:
    """Verilen `company_ticker` ("BORSA: SEMBOL", ör. "NASDAQ: TSLA") alanı
    TAM olarak eşleşen haberleri döner - bkz. src/web/app.py >
    /api/company-detail (emtia raporundaki şirket detay modalı). BİLİNÇLİ
    olarak `src/company_profile.py > get_company_profile`'daki gibi
    başlık/özet içinde serbest metin arama (LIKE) YAPILMAZ: burada amaç
    "bu TİCKER için sistemin zaten etiketlediği haberler" - ayrı bir LLM
    çağrısı da gerektirmez (bkz. company_profile.py'nin aksine), bu yüzden
    modal her açıldığında ek maliyet/gecikme YOKTUR."""
    with get_session() as session:
        query = session.query(NewsRecord).filter(NewsRecord.company_ticker == company_ticker)
        if since is not None:
            query = query.filter(NewsRecord.first_seen_at >= since)
        return query.order_by(NewsRecord.first_seen_at.desc()).limit(limit).all()


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


def search_records(search_query: str, limit: int = 15) -> list[NewsRecord]:
    """Telegram /ara komutu için (bkz. src/telegram_bot.py): dashboard'daki
    arama kutusuyla (bkz. get_recent_records > search_query) AYNI basit
    case-insensitive LIKE mantığını kullanır - kendi kısa ömürlü session'ını
    açar/kapatır, çağıran tarafın bir session yönetmesi gerekmez."""
    with get_session() as session:
        return get_recent_records(session, limit=limit, search_query=search_query)


def get_records_between(start: datetime, end: datetime) -> list[NewsRecord]:
    """`start` (dahil) ile `end` (hariç) arasında görülmüş (first_seen_at)
    TÜM haberleri döner - haftalık/aylık trend raporu için bir önceki
    dönemle karşılaştırma yapmakta kullanılır (bkz. src/trend_report.py).
    `get_records_since`'ten farkı: üst sınırı da olması (o fonksiyon yalnızca
    "şu ana kadar" için kullanılıyor, geçmiş bir dönemi izole edemez)."""
    with get_session() as session:
        return (
            session.query(NewsRecord)
            .filter(NewsRecord.first_seen_at >= start, NewsRecord.first_seen_at < end)
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


def add_subscriber(
    chat_id: str | int,
    username: str | None = None,
    first_name: str | None = None,
    default_threshold: int = 4,
) -> bool:
    """Yeni bir Telegram abonesi ekler. `chat_id` zaten kayıtlıysa (UNIQUE)
    hiçbir şey yapmaz. Döner: yeni eklendiyse True, zaten aboneyse False.

    `default_threshold`: yeni abonenin başlangıç önem eşiği (bkz. Telegram
    /esik komutu) - çağıran taraf (src/telegram_bot.py) burada config.yaml >
    importance.threshold değerini geçirir."""
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
                importance_threshold=default_threshold,
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


def get_app_state(key: str) -> Any | None:
    """`AppState` tablosundan verilen anahtarın JSON-çözülmüş değerini döner
    (bkz. AppState modeli). Anahtar yoksa veya değer bozuksa (JSON
    ayrıştırma hatası) None döner - çağıran taraf bunu "önbellek boş/henüz
    hesaplanmadı" olarak yorumlar, exception fırlatmaz."""
    with get_session() as session:
        row = session.query(AppState).filter_by(key=key).one_or_none()
        if row is None:
            return None
        try:
            return json.loads(row.value)
        except (TypeError, ValueError):
            logger.warning("AppState[%s] JSON olarak ayrıştırılamadı, None dönülüyor.", key)
            return None


def set_app_state(key: str, value: Any) -> None:
    """`AppState` tablosuna verilen anahtar için JSON-serileştirilmiş
    değeri yazar (varsa günceller, yoksa oluşturur - upsert)."""
    with get_session() as session:
        row = session.query(AppState).filter_by(key=key).one_or_none()
        payload = json.dumps(value, ensure_ascii=False)
        now = datetime.now(timezone.utc)
        if row is None:
            session.add(AppState(key=key, value=payload, updated_at=now))
        else:
            row.value = payload
            row.updated_at = now
        session.commit()


def get_all_subscriber_chat_ids(session: Session | None = None) -> list[str]:
    """Bildirim gönderilecek tüm chat_id'leri döner."""
    if session is not None:
        return [chat_id for (chat_id,) in session.query(Subscriber.chat_id).all()]
    with get_session() as s:
        return [chat_id for (chat_id,) in s.query(Subscriber.chat_id).all()]


def get_subscriber_chat_ids_for_score(score: int) -> list[str]:
    """Verilen önem skorunu ALACAK abonelerin chat_id'lerini döner: yani
    kendi `importance_threshold` değeri `score`'a eşit veya küçük olanlar
    (ör. skor 3 olan bir haber, eşiği 3 VEYA daha düşük olan herkese gider,
    eşiği 4 olanlara gitmez). Bkz. Telegram /esik komutu, src/notifier.py."""
    with get_session() as session:
        return [
            chat_id
            for (chat_id,) in session.query(Subscriber.chat_id)
            .filter(Subscriber.importance_threshold <= score)
            .all()
        ]


def set_subscriber_threshold(chat_id: str | int, threshold: int) -> bool:
    """Bir abonenin kişisel önem eşiğini günceller (bkz. /esik komutu).
    Döner: abone kayıtlıysa ve güncellendiyse True, abone değilse False."""
    chat_id_str = str(chat_id)
    with get_session() as session:
        existing = session.query(Subscriber).filter_by(chat_id=chat_id_str).one_or_none()
        if existing is None:
            return False
        existing.importance_threshold = threshold
        logger.info("Abone eşiği güncellendi: chat_id=%s, esik=%d", chat_id_str, threshold)
        return True


def get_subscriber_threshold(chat_id: str | int) -> int | None:
    """Bir abonenin kişisel önem eşiğini döner. Abone değilse None döner
    (bkz. /esikgoster komutu)."""
    chat_id_str = str(chat_id)
    with get_session() as session:
        existing = session.query(Subscriber).filter_by(chat_id=chat_id_str).one_or_none()
        return existing.importance_threshold if existing is not None else None


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


def record_source_health(source: str, success: bool, item_count: int = 0, error_message: str | None = None) -> None:
    """Bir fetcher çalıştırma denemesini kaydeder (bkz. SourceHealth, src/main.py
    > fetch_source). Kendi kısa ömürlü session'ını açar/kapatır - çağıran
    taraf (ThreadPoolExecutor içindeki her kaynak thread'i) başka bir session
    yönetmek zorunda kalmaz. Hiçbir durumda exception fırlatmaz (bkz. çağrı
    yeri) - sağlık kaydı asla asıl tarama akışını bozmasın."""
    try:
        with get_session() as session:
            session.add(
                SourceHealth(
                    source=source,
                    ran_at=datetime.now(timezone.utc),
                    success=success,
                    item_count=item_count,
                    error_message=(error_message or "")[:2000] or None,
                )
            )
    except Exception:  # noqa: BLE001 - sağlık kaydı asla ana akışı etkilemesin
        logger.exception("Kaynak sağlık kaydı yazılamadı: source=%s", source)


def get_source_health_summary(since: datetime) -> list[dict[str, Any]]:
    """Dashboard'daki kaynak sağlık panelinin (bkz. src/web/app.py >
    /kaynak-sagligi) ihtiyaç duyduğu özet istatistikleri hesaplar: her kaynak
    için `since`'ten bu yana başarı oranı, ortalama haber sayısı (yalnızca
    başarılı çalıştırmalar üzerinden) ve son başarılı/başarısız çalıştırma
    zamanı. Kaynak adına göre alfabetik sıralı döner."""
    with get_session() as session:
        rows = (
            session.query(SourceHealth)
            .filter(SourceHealth.ran_at >= since)
            .order_by(SourceHealth.ran_at.asc())
            .all()
        )

    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        s = stats.setdefault(
            row.source,
            {
                "source": row.source,
                "total_runs": 0,
                "success_runs": 0,
                "item_count_sum": 0,
                "last_success_at": None,
                "last_failure_at": None,
                "last_error_message": None,
            },
        )
        s["total_runs"] += 1
        if row.success:
            s["success_runs"] += 1
            s["item_count_sum"] += row.item_count
            s["last_success_at"] = row.ran_at
        else:
            s["last_failure_at"] = row.ran_at
            s["last_error_message"] = row.error_message

    result = []
    for source, s in stats.items():
        success_rate = round(100 * s["success_runs"] / s["total_runs"], 1) if s["total_runs"] else 0.0
        avg_items = round(s["item_count_sum"] / s["success_runs"], 1) if s["success_runs"] else 0.0
        result.append(
            {
                "source": source,
                "success_rate": success_rate,
                "avg_item_count": avg_items,
                "total_runs": s["total_runs"],
                "last_success_at": s["last_success_at"],
                "last_failure_at": s["last_failure_at"],
                "last_error_message": s["last_error_message"],
            }
        )

    result.sort(key=lambda item: item["source"])
    return result


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


def get_distinct_sectors(session: Session) -> list[str]:
    """Dashboard'daki sektör filtresi için görülmüş tüm sektör etiketlerini
    döner. `sector` kolonu JSON string listesi olarak tutulduğundan (bkz.
    NewsRecord.sectors_list), tüm kayıtlar taranıp Python tarafında ayrıştırılır."""
    slugs: set[str] = set()
    for (sector_json,) in session.query(NewsRecord.sector).filter(NewsRecord.sector.isnot(None)).all():
        try:
            for slug in json.loads(sector_json):
                slugs.add(slug)
        except json.JSONDecodeError:
            continue
    return sorted(slugs)
