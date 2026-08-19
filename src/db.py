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
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint, create_engine, func, or_
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

    # SADECE KAP kaynaklı kayıtlarda dolu (bkz. src/summarizer.py >
    # VALID_KAP_CATEGORIES/KAP_CATEGORY_LABELS, src/models.py >
    # NewsGroup.kap_category) - 8 sabit kategoriden biri. KAP-dışı
    # kayıtlarda/bu özellik eklenmeden önceki KAP kayıtlarında None.
    kap_category = Column(String(32), nullable=True)

    # SADECE KAP kaynaklı kayıtlarda dolu (bkz. src/summarizer.py >
    # _KAP_SHORT_SUMMARY_INSTRUCTION, src/models.py > NewsGroup.short_summary,
    # kullanıcı isteği 2026-08-18): dashboard KAP kartının ana/vurgulu satırı
    # için "[TICKER]: [somut eylem/sonuç]" kısa özeti - uzun resmi başlığın
    # YERİNE gösterilir. KAP-dışı kayıtlarda/bu özellik eklenmeden önceki KAP
    # kayıtlarında None (dashboard bu durumda eski davranışa - uzun başlık -
    # düşer, bkz. templates/dashboard.html).
    short_summary = Column(String(280), nullable=True)

    # TradingView entegrasyonu (2026-08-19, kullanıcı isteği) - bkz.
    # src/models.py > NewsGroup.trading_view_symbol, src/summarizer.py >
    # SYSTEM_PROMPT/KAP_SYSTEM_PROMPT. "BORSA:SEMBOL" formatında (ör.
    # "NASDAQ:NFLX", "FX:USDJPY") ya da None - dashboard bu doluysa bir
    # "Teknik Görünüm" butonu gösterir (bkz. src/web/app.py > _record_to_view,
    # templates/dashboard.html), boşsa buton HİÇ render edilmez.
    trading_view_symbol = Column(String(64), nullable=True)

    # TradingView sembol doğrulaması (2026-08-19, kullanıcı geri bildirimi) -
    # bkz. src/tradingview.py > validate_symbol, src/models.py >
    # NewsGroup.trading_view_symbol_valid. NULL = henüz doğrulanamadı (ağ
    # hatası) - dashboard SADECE True olduğunda "Teknik Görünüm" butonunu
    # gösterir (bkz. src/web/app.py > _record_to_view).
    trading_view_symbol_valid = Column(Boolean, nullable=True)

    # Haber görseli (2026-08-18, kullanıcı isteği) - kaynağın kendi RSS
    # feed'inden (media:content/thumbnail/enclosure, bkz.
    # src/fetchers/rss_fetcher.py > _extract_image_url) veya lisanslı
    # NewsAPI.ai kaynağından (bkz. src/fetchers/licensed_aggregator.py)
    # gelen GERÇEK URL - UYDURMA/üretilmiş DEĞİL. Boş -> kaynakta görsel
    # yoktu (KAP'ta HER ZAMAN boş - resmi bildirimlerde görsel olmaz) veya
    # bu özellik eklenmeden önce çekilmiş eski bir kayıt.
    image_url = Column(String(1024), nullable=True)

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

    # "Sadece KAP" modu (bkz. Telegram /kap_sadece komutu, 2026-08-17) - bu
    # zamana KADAR (UTC) aktifse, abone KAP DIŞI hiçbir bildirim almaz (bkz.
    # src/notifier.py, get_subscriber_chat_ids_for_score > is_kap parametresi).
    # None = normal mod (varsayılan, tüm kaynaklardan bildirim alır). Süre
    # dolduğunda AYRI bir "moddan çıkar" işlemi/scheduler job'ı GEREKMEZ -
    # lazy-expiry: bu alan geçmiş bir zamana işaret ediyorsa sorgu tarafında
    # otomatik olarak "normal mod" sayılır (bu projede zaten yaygın bir desen,
    # bkz. src/web/app.py'deki TTL-cache'ler) - eski değer DB'de kalabilir,
    # bir sonraki /kap_sadece veya /kap_bitir zaten üzerine yazar.
    kap_only_until = Column(DateTime(timezone=True), nullable=True)

    # Botla en son ne zaman etkileşime girdiği (herhangi bir komut/serbest
    # metin/buton - bkz. src/telegram_bot.py > _touch_last_active, TÜM gelen
    # update'lerde tetiklenen düşük öncelikli bir handler). Admin görünümü
    # (bkz. src/web/app.py > /admin/subscribers) için - kritik bir alan
    # DEĞİLDİR, güncellenemezse (ör. abone DB'de henüz yoksa) sessizce atlanır.
    last_active_at = Column(DateTime(timezone=True), nullable=True)


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


class PushSubscription(Base):
    """Bir tarayıcının Web Push aboneliği (bkz. src/web_push.py). Kullanıcı
    dashboard'da "🔔 Bildirimleri Aç" ile abone olduğunda, tarayıcının
    `PushManager.subscribe()` çağrısından dönen `endpoint` + `keys.p256dh`/
    `keys.auth` burada saklanır - sekme/tarayıcı KAPALIYKEN bile bu bilgiyle
    push gönderilebilir (Telegram'daki `Subscriber.chat_id`'nin tarayıcı
    karşılığı).

    `endpoint` (push servisinin verdiği benzersiz URL - ör.
    fcm.googleapis.com/.../<id>) doğal birincil kimlik olarak kullanılır;
    aynı tarayıcı/cihaz kombinasyonu HER ZAMAN aynı endpoint'i üretir, bu
    yüzden tekil (UNIQUE)."""

    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True)
    endpoint = Column(Text, unique=True, index=True, nullable=False)
    p256dh = Column(String(255), nullable=False)
    auth = Column(String(255), nullable=False)

    # Bu aboneliğin hangi haberler için bildirim almak istediğini belirten
    # filtre - JSON: {"sources": [...], "sectors": [...], "regions": [...],
    # "sentiments": [...], "min_importance": int|None}. Bir alan boş/None ise
    # o boyutta filtre YOK (her değeri eşleşir) - bkz. src/web_push.py >
    # _record_matches_filters. NULL -> kullanıcı henüz filtre kaydetmedi
    # (Faz 1'de sadece abone olunur, Faz 2'de filtre eklenir).
    filters = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False)
    # Tarayıcı aynı endpoint'le TEKRAR subscribe ederse (ör. filtre
    # güncellemesi) bu alan güncellenir - "hâlâ aktif" sinyali.
    last_seen_at = Column(DateTime(timezone=True), nullable=False)


class PushNotification(Base):
    """Bir (endpoint, group_key) çiftine push bildirimi zaten gönderildiğini
    işaretler - `KeywordNotification` ile AYNI desen, aynı haberin aynı
    tarayıcıya birden fazla gönderilmesini engeller (bkz. src/web_push.py)."""

    __tablename__ = "push_notifications"

    id = Column(Integer, primary_key=True)
    endpoint = Column(Text, index=True, nullable=False)
    group_key = Column(String(64), index=True, nullable=False)
    notified_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("endpoint", "group_key", name="uq_push_notification_endpoint_group"),
    )


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


class EconomicCalendarEvent(Base):
    """Ekonomik takvim olayı (2026-08-18, kullanıcı isteği) - kaynak
    tradingeconomics.com/calendar (bkz. src/economic_calendar.py; Investing.com
    Cloudflare bot-koruması nedeniyle KULLANILAMADI, bkz. sohbet). AYRI bir
    tablo (`news_records`e ZORLANMADI) - haber kaydı DEĞİL, tekrarlayan/
    güncellenen bir gösterge takvimi girdisi.

    Kapsam (kullanıcı kararı): Türkiye için TÜM önem seviyeleri, diğer
    ülkeler için SADECE 2-3 yıldız (bkz. economic_calendar.py > REFRESH
    mantığı - importance=2/3 sorgularının küme farkıyla hesaplanır, TE
    sayfası satır başına yıldız sayısını DOĞRUDAN vermiyor).

    İKİ AŞAMALI güncelleme: `actual_value` başlangıçta NULL (bkz.
    previous_value - "bekleniyor" durumunda gösterilir), olayın açıklanma
    saatinden SONRA (bkz. worker.py > _economic_calendar_watch_job, 30sn'lik
    hafif interval-polling) gerçek değerle DOLAR. Upsert anahtarı
    (event_time, country_code, event_name) - TE'nin `data-id`'si AYNI
    gösterge serisi için farklı tarihlerde TEKRARLANDIĞINDAN (ör. "Unemployment
    Rate" her ay aynı data-id) tek başına güvenilir bir birincil anahtar
    DEĞİL, sadece tek bir tarama içinde (bkz. economic_calendar.py) 3 farklı
    importance sorgusunu eşleştirmek için kullanılıyor."""

    __tablename__ = "economic_calendar_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_time = Column(DateTime(timezone=True), nullable=False, index=True)
    country_code = Column(String(8), nullable=False)  # ISO 2 harf (ör. "TR", "US") - bkz. flag/iso td
    country_name = Column(String(64), nullable=False)
    event_name = Column(String(255), nullable=False)
    importance = Column(Integer, nullable=False)  # 1-3 (bkz. sınıf docstring'i)
    previous_value = Column(String(64), nullable=True)
    actual_value = Column(String(64), nullable=True)  # NULL -> henüz açıklanmadı ("bekleniyor")
    reference_period = Column(String(32), nullable=True)  # ör. "JUN" - hangi aya/döneme ait
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("event_time", "country_code", "event_name", name="uq_calendar_event"),
    )


class ApiKey(Base):
    """Genel/dış kullanıma açık `/api/v1/*` uç noktaları (bkz.
    src/web/api_v1.py) için API anahtarı kaydı. Bu tablo SADECE anahtar
    doğrulama/kullanım sayacı için var - dashboard'un iç kullandığı
    `/api/*` rotaları bu tabloyu HİÇ kullanmaz, kimlik doğrulaması
    gerektirmez (gereksinim: iç/dış API'ler birbirinden bağımsız).

    `key` DEĞERİNİN KENDİSİ hiçbir zaman `/api/v1/*` yanıtlarında geri
    DÖNMEZ/SIZDIRILMAZ - sadece `X-API-Key` header'ıyla gelen değerle
    karşılaştırılır (bkz. src/web/api_v1.py > require_api_key)."""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True)
    key = Column(String(64), unique=True, index=True, nullable=False)
    # Anahtarın kime/hangi projeye ait olduğunu hatırlamak için (ör. "admin",
    # "proje-x") - salt görsel/yönetimsel, yetkilendirmede kullanılmaz.
    label = Column(String(200), nullable=False)
    # "standard" (varsayılan, düşük rate-limit) veya "admin" (kullanıcının
    # KENDİ kullanımı için - çok daha yüksek/pratikte sınırsız rate-limit,
    # bkz. src/web/api_v1.py > RATE_LIMITS).
    tier = Column(String(20), nullable=False, default="standard")
    created_at = Column(DateTime(timezone=True), nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    # Rate-limit SAYAÇLARI değil (bunlar süreç-içi/in-memory tutulur, bkz.
    # src/web/api_v1.py - Render zaten TEK worker çalıştırdığından güvenilir)
    # - bu sadece TOPLAM kullanım istatistiği, kalıcı/DB tarafında.
    total_request_count = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)


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
        if "kap_category" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN kap_category VARCHAR(32)")
            logger.info("Veritabanı migrasyonu: news_records.kap_category kolonu eklendi.")
        if "short_summary" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN short_summary VARCHAR(280)")
            logger.info("Veritabanı migrasyonu: news_records.short_summary kolonu eklendi.")
        if "image_url" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN image_url VARCHAR(1024)")
            logger.info("Veritabanı migrasyonu: news_records.image_url kolonu eklendi.")
        if "trading_view_symbol" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN trading_view_symbol VARCHAR(64)")
            logger.info("Veritabanı migrasyonu: news_records.trading_view_symbol kolonu eklendi.")
        if "trading_view_symbol_valid" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE news_records ADD COLUMN trading_view_symbol_valid BOOLEAN")
            logger.info("Veritabanı migrasyonu: news_records.trading_view_symbol_valid kolonu eklendi.")

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
        if "kap_only_until" not in existing_subscriber_columns:
            # TIMESTAMP (SQLAlchemy'nin DateTime(timezone=True) için dialect'e
            # özgü DDL'i YERİNE) BİLİNÇLİ OLARAK portable/ANSI bir tip -
            # diğer migrasyon satırlarıyla AYNI yaklaşım (bkz. yukarıdaki
            # TEXT/VARCHAR/INTEGER'lar): hem SQLite hem Postgres'te (bkz.
            # DATABASE_URL) çalışır, exec_driver_sql ham SQL çalıştırdığından
            # SQLAlchemy'nin dialect çevirisinden yararlanamaz.
            conn.exec_driver_sql("ALTER TABLE subscribers ADD COLUMN kap_only_until TIMESTAMP")
            logger.info("Veritabanı migrasyonu: subscribers.kap_only_until kolonu eklendi.")
        if "last_active_at" not in existing_subscriber_columns:
            conn.exec_driver_sql("ALTER TABLE subscribers ADD COLUMN last_active_at TIMESTAMP")
            logger.info("Veritabanı migrasyonu: subscribers.last_active_at kolonu eklendi.")


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
            kap_category=group.kap_category,
            short_summary=group.short_summary,
            trading_view_symbol=group.trading_view_symbol,
            trading_view_symbol_valid=group.trading_view_symbol_valid,
            image_url=group.image_url or None,
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
        record.kap_category = group.kap_category or record.kap_category
        record.title_tr = group.title_tr or record.title_tr
        record.short_summary = group.short_summary or record.short_summary
        record.trading_view_symbol = group.trading_view_symbol or record.trading_view_symbol
        # `or` DEĞİL `is not None` kontrolü (2026-08-19) - trading_view_symbol_valid
        # bir boolean, group.trading_view_symbol_valid=False GEÇERLİ bir
        # sonuç (sembol doğrulandı ve GERÇEKTEN yok) - `or` kullansaydık
        # False, Python'da "falsy" olduğundan record'un ESKİ değerine
        # sessizce düşerdi (GERÇEK bir bug, `short_summary or ...` gibi
        # diğer satırlarla AYNI kalıbı kopyalarken fark edildi).
        if group.trading_view_symbol_valid is not None:
            record.trading_view_symbol_valid = group.trading_view_symbol_valid
        record.image_url = group.image_url or record.image_url
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
    exclude_source_filter: str | None = None,
    sector_filter: str | None = None,
    region_filter: str | None = None,
    sentiment_filter: str | None = None,
    min_importance: int | None = None,
    search_query: str | None = None,
    since: datetime | None = None,
    sort_order: str = "newest",
    category_filter: str | None = None,
    additional_sector_filter: str | None = None,
    kap_category_filter: str | None = None,
) -> list[NewsRecord]:
    query = session.query(NewsRecord)
    if kap_category_filter:
        # KAP sütunu için (bkz. src/web/app.py > dashboard() > kap_category
        # query param) - `top_category` gibi düz bir string kolon, JSON liste
        # DEĞİL (bkz. src/summarizer.py > VALID_KAP_CATEGORIES).
        query = query.filter(NewsRecord.kap_category == kap_category_filter)
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
    if exclude_source_filter:
        # Ana dashboard'un iki-sütun düzeni için (bkz. src/web/app.py >
        # dashboard() - sağ sütun/genel akış artık KAP'ı HİÇ göstermez, KAP
        # kendi ayrı sol sütununda) - `source_filter` ile AYNI ANDA da
        # verilebilir (pratikte kullanılmıyor ama birbirini dışlamıyor,
        # ikisi de bağımsız AND koşulu).
        query = query.filter(~NewsRecord.sources.contains(exclude_source_filter))
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
    elif sort_order == "importance":
        # Ana sayfa Hero/Son Dakika döngüsü için (2026-08-18, kullanıcı
        # kararı: "eşik koymadan, sadece en yüksek 10") - önem skoruna göre
        # azalan, eşit skorlarda en yeni önce (ikincil sıralama). NULL
        # (henüz skorlanmamış) kayıtlar hem SQLite hem Postgres'te DESC
        # sıralamada doğal olarak SONA düşer - çağıran taraf yine de
        # `min_importance=1` ile bunları AÇIKÇA filtrelemeli (bkz.
        # app.py > dashboard() > hero carousel sorgusu), aksi halde
        # skorsuz kayıtlar limit'i doldurabilir.
        query = query.order_by(NewsRecord.importance_score.desc(), NewsRecord.first_seen_at.desc())
    else:
        query = query.order_by(NewsRecord.first_seen_at.desc())
    return query.limit(limit).all()


def get_public_news_by_id(group_key: str) -> NewsRecord | None:
    """Genel/dış `/api/v1/news/{id}` uç noktası için tek bir haberi
    `group_key`'ine göre döner (bkz. find_record_by_group_key - AYNI sorgu,
    sadece kendi session'ını açan bağımsız bir sarmalayıcı)."""
    with get_session() as session:
        return find_record_by_group_key(session, group_key)


def get_public_news_page(
    page: int = 1,
    page_size: int = 20,
    source: str | None = None,
    sector: str | None = None,
    region: str | None = None,
    sentiment: str | None = None,
    min_importance: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[NewsRecord], int]:
    """Genel/dış `/api/v1/news` uç noktası (bkz. src/web/api_v1.py) için
    SAYFALAMA destekli sorgu - BİLİNÇLİ OLARAK `get_recent_records`'tan
    AYRI bir fonksiyon (iç dashboard sorgusunu değiştirme/bozma riski
    almamak için, gereksinim: iç/dış API'ler birbirinden bağımsız).

    `get_recent_records` ile AYNI filtre mantığı (JSON liste alanlarında
    tam eşleşme için tırnaklı `contains`), ama EK olarak `date_to` (üst
    sınır) ve gerçek OFFSET-tabanlı sayfalama destekler.

    Döner: (bu sayfadaki kayıtlar, TÜM filtreye uyan toplam kayıt sayısı) -
    toplam sayı, istemcinin "kaç sayfa var" hesaplayabilmesi için AYRI bir
    COUNT sorgusuyla hesaplanır (tüm kayıtları belleğe çekmeden)."""
    with get_session() as session:
        query = session.query(NewsRecord)
        if source:
            query = query.filter(NewsRecord.sources.contains(source))
        if sector:
            query = query.filter(NewsRecord.sector.contains(f'"{sector}"'))
        if region:
            query = query.filter(NewsRecord.regions.contains(f'"{region}"'))
        if sentiment:
            query = query.filter(NewsRecord.sentiment == sentiment)
        if min_importance is not None:
            query = query.filter(NewsRecord.importance_score >= min_importance)
        if date_from is not None:
            query = query.filter(NewsRecord.first_seen_at >= date_from)
        if date_to is not None:
            query = query.filter(NewsRecord.first_seen_at <= date_to)

        total = query.count()

        page = max(1, page)
        page_size = max(1, min(page_size, 100))  # sunucu tarafı üst sınır - kötüye kullanıma karşı
        offset = (page - 1) * page_size

        records = (
            query.order_by(NewsRecord.first_seen_at.desc())
            .offset(offset)
            .limit(page_size)
            .all()
        )
        return records, total


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


def get_latest_published_at(session: Session | None = None) -> datetime | None:
    """Tüm veritabanındaki (aktif filtre/sıralama görünümünden BAĞIMSIZ) en
    yeni `published_at` değerini döner - dashboard'un "bu veri güncel mi"
    tazelik uyarısı için kullanılır (bkz. src/web/app.py > _is_data_stale).
    `published_at` NULL olan kayıtlar (bkz. yayın tarihi bilinmiyor durumu)
    MAX() tarafından zaten otomatik göz ardı edilir. Hiç kayıt yoksa/hepsi
    NULL ise None döner.

    ÖNEMLİ: SQLite backend'inde `func.max()` gibi agregat fonksiyonlar,
    `DateTime(timezone=True)` kolonunun kendi tip dönüştürücüsünü (result
    processor) ATLAYIP tz-naive bir datetime döndürebiliyor (SQLite gerçekte
    tz bilgisi SAKLAMAZ, bu sütun tipi sadece SQLAlchemy seviyesinde bir
    soyutlama) - canlı testte doğrulandı. Bu proje tüm datetime'ları HER ZAMAN
    UTC olarak üretir (bkz. src/timezone_utils.py > to_turkey_time'daki AYNI
    savunmacı normalizasyon) - o yüzden burada da tz-naive gelirse UTC kabul
    edilip tz-aware'e çevrilir; aksi halde çağıran taraf tz-aware `datetime.now()`
    ile çıkarma yaparken `TypeError: can't subtract offset-naive and
    offset-aware datetimes` alır (gerçek bir hatayla yakalandı, bkz. testler)."""
    if session is not None:
        result = session.query(func.max(NewsRecord.published_at)).scalar()
    else:
        with get_session() as s:
            result = s.query(func.max(NewsRecord.published_at)).scalar()
    if result is not None and result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


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


def upsert_economic_calendar_events(events: list[dict[str, Any]]) -> None:
    """Verilen olay listesini (bkz. src/economic_calendar.py > fetch_calendar_events
    döndürdüğü sözlük şekli) `economic_calendar_events` tablosuna UPSERT eder -
    (event_time, country_code, event_name) eşleşirse SADECE değişebilecek
    alanları (previous_value/actual_value/importance/reference_period)
    GÜNCELLER, `id` korunur. `actual_value` YENİ gelen değer None ise
    (henüz açıklanmadı) ESKİ değer SİLİNMEZ - bir olay bir kez açıklandıktan
    sonra bir sonraki taramada "henüz açıklanmadı" durumuna GERİ DÜŞMEMELİ
    (bkz. worker.py > _economic_calendar_watch_job, aynı olayı defalarca
    tarayabilir)."""
    now = datetime.now(timezone.utc)
    with get_session() as session:
        for ev in events:
            row = (
                session.query(EconomicCalendarEvent)
                .filter_by(
                    event_time=ev["event_time"],
                    country_code=ev["country_code"],
                    event_name=ev["event_name"],
                )
                .one_or_none()
            )
            if row is None:
                session.add(
                    EconomicCalendarEvent(
                        event_time=ev["event_time"],
                        country_code=ev["country_code"],
                        country_name=ev["country_name"],
                        event_name=ev["event_name"],
                        importance=ev["importance"],
                        previous_value=ev.get("previous_value"),
                        actual_value=ev.get("actual_value"),
                        reference_period=ev.get("reference_period"),
                        updated_at=now,
                    )
                )
            else:
                row.importance = ev["importance"]
                row.previous_value = ev.get("previous_value") or row.previous_value
                row.actual_value = ev.get("actual_value") or row.actual_value
                row.reference_period = ev.get("reference_period") or row.reference_period
                row.updated_at = now
        session.commit()


def get_upcoming_calendar_events(limit: int = 8) -> list["EconomicCalendarEvent"]:
    """Ana sayfadaki "Yaklaşan Ekonomik Takvim" kutusu için (bkz.
    src/web/app.py > dashboard()) - şu andan (UTC) itibaren gelecekteki VEYA
    son 2 saat içinde açıklanmış (yeni açıklanan bir değeri kısa süre daha
    görünür tutmak için) olayları, zamana göre artan sırayla döner."""
    since = datetime.now(timezone.utc) - timedelta(hours=2)
    with get_session() as session:
        return (
            session.query(EconomicCalendarEvent)
            .filter(EconomicCalendarEvent.event_time >= since)
            .order_by(EconomicCalendarEvent.event_time.asc())
            .limit(limit)
            .all()
        )


def get_pending_calendar_events_count(window_minutes: int = 3) -> int:
    """worker.py > _economic_calendar_watch_job için - açıklanma saati
    GEÇMİŞ (son `window_minutes` dakika içinde) AMA `actual_value` HÂLÂ
    NULL olan olay sayısı. 0 dönerse çağıran taraf GEREKSİZ bir Trading
    Economics isteği ATMAZ (nezaket kuralı, kullanıcı isteği 2026-08-18)."""
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=window_minutes)
    with get_session() as session:
        return (
            session.query(EconomicCalendarEvent)
            .filter(EconomicCalendarEvent.event_time >= window_start)
            .filter(EconomicCalendarEvent.event_time <= now)
            .filter(EconomicCalendarEvent.actual_value.is_(None))
            .count()
        )


def get_all_subscriber_chat_ids(session: Session | None = None) -> list[str]:
    """Bildirim gönderilecek tüm chat_id'leri döner."""
    if session is not None:
        return [chat_id for (chat_id,) in session.query(Subscriber.chat_id).all()]
    with get_session() as s:
        return [chat_id for (chat_id,) in s.query(Subscriber.chat_id).all()]


def get_subscriber_chat_ids_for_score(score: int, is_kap: bool = False) -> list[str]:
    """Verilen önem skorunu ALACAK abonelerin chat_id'lerini döner: yani
    kendi `importance_threshold` değeri `score`'a eşit veya küçük olanlar
    (ör. skor 3 olan bir haber, eşiği 3 VEYA daha düşük olan herkese gider,
    eşiği 4 olanlara gitmez). Bkz. Telegram /esik komutu, src/notifier.py.

    `is_kap=True` verilirse (bildirim KAP kaynaklıysa) `kap_only_until`
    filtresi UYGULANMAZ - "sadece KAP" modundaki abone zaten KAP bildirimi
    almaya devam etmeli. `is_kap=False` (varsayılan, KAP-dışı bir haber)
    olduğunda ise, `kap_only_until` alanı SÜRESİ DOLMAMIŞ (şimdiden büyük)
    olan aboneler sonuçtan ÇIKARILIR - bkz. Telegram /kap_sadece komutu,
    Subscriber.kap_only_until alanındaki lazy-expiry notu (süresi geçmiş bir
    değer otomatik olarak "normal mod" sayılır, ekstra bir sorgu/temizlik
    gerekmez)."""
    now = datetime.now(timezone.utc)
    with get_session() as session:
        query = session.query(Subscriber.chat_id).filter(Subscriber.importance_threshold <= score)
        if not is_kap:
            query = query.filter(or_(Subscriber.kap_only_until.is_(None), Subscriber.kap_only_until <= now))
        return [chat_id for (chat_id,) in query.all()]


def set_subscriber_kap_only(chat_id: str | int, until: datetime | None) -> bool:
    """Bir abonenin "sadece KAP" modunu ayarlar (`until` = bu zamana KADAR
    aktif, UTC) veya kaldırır (`until=None`, bkz. /kap_bitir komutu). Döner:
    abone kayıtlıysa ve güncellendiyse True, abone değilse False (bkz.
    set_subscriber_threshold ile AYNI desen)."""
    chat_id_str = str(chat_id)
    with get_session() as session:
        existing = session.query(Subscriber).filter_by(chat_id=chat_id_str).one_or_none()
        if existing is None:
            return False
        existing.kap_only_until = until
        logger.info("Abone 'sadece KAP' modu güncellendi: chat_id=%s, kap_only_until=%s", chat_id_str, until)
        return True


def get_subscriber_kap_only(chat_id: str | int) -> datetime | None:
    """Bir abonenin AKTİF "sadece KAP" bitiş zamanını (UTC) döner - süresi
    geçmişse (lazy-expiry, bkz. Subscriber.kap_only_until notu) None döner,
    yani çağıran taraf için "normal mod" ile ayırt edilemez (bu bilinçli -
    /kap_durum komutu bu iki durumu zaten aynı şekilde göstermeli). Bu
    salt-okunur bir sorgudur, süresi geçmiş eski değeri DB'de SİLMEZ/temizlemez."""
    chat_id_str = str(chat_id)
    with get_session() as session:
        existing = session.query(Subscriber).filter_by(chat_id=chat_id_str).one_or_none()
        if existing is None or existing.kap_only_until is None:
            return None
        until = existing.kap_only_until
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if until <= datetime.now(timezone.utc):
            return None
        return until


def touch_subscriber_last_active(chat_id: str | int) -> None:
    """Bir abonenin `last_active_at` alanını ŞİMDİ (UTC) olarak günceller -
    bkz. src/telegram_bot.py > _touch_last_active (TÜM gelen Telegram
    update'lerinde, komut olsun olmasın, düşük öncelikli bir handler ile
    tetiklenir), admin görünümü (bkz. src/web/app.py > /admin/subscribers,
    get_all_subscribers_overview). Abone henüz DB'de yoksa (ör. bu update
    TAM /start'ın kendisiyse ve add_subscriber henüz çalışmadıysa) sessizce
    hiçbir şey yapmaz - kritik olmayan, "nice-to-have" bir alan, hata
    fırlatmaz."""
    chat_id_str = str(chat_id)
    with get_session() as session:
        existing = session.query(Subscriber).filter_by(chat_id=chat_id_str).one_or_none()
        if existing is None:
            return
        existing.last_active_at = datetime.now(timezone.utc)


def get_all_subscribers_overview() -> list[dict[str, Any]]:
    """Admin görünümü (bkz. /admin/subscribers) için TÜM abonelerin özet
    bilgisini (en son abone olandan en eskiye doğru) döner: temel Subscriber
    alanları + o abonenin KeywordSubscription tablosundaki takip ettiği
    kelimeler (varsa). Sunum/biçimlendirme (ör. Türkiye saatine çevirme)
    BİLEREK burada yapılmaz - bu katmanın işi değil (bkz. src/web/app.py >
    admin_subscribers route'u, diğer _record_to_view gibi view-model
    fonksiyonlarıyla AYNI ayrım)."""
    now = datetime.now(timezone.utc)
    with get_session() as session:
        subscribers = session.query(Subscriber).order_by(Subscriber.subscribed_at.desc()).all()

        keywords_by_chat: dict[str, list[str]] = {}
        for chat_id, keyword in session.query(KeywordSubscription.chat_id, KeywordSubscription.keyword).all():
            keywords_by_chat.setdefault(chat_id, []).append(keyword)

        result: list[dict[str, Any]] = []
        for sub in subscribers:
            kap_until = sub.kap_only_until
            if kap_until is not None and kap_until.tzinfo is None:
                kap_until = kap_until.replace(tzinfo=timezone.utc)
            result.append(
                {
                    "chat_id": sub.chat_id,
                    "username": sub.username,
                    "first_name": sub.first_name,
                    "importance_threshold": sub.importance_threshold,
                    "kap_only_until": kap_until,
                    "kap_only_active": kap_until is not None and kap_until > now,
                    "subscribed_at": sub.subscribed_at,
                    "last_active_at": sub.last_active_at,
                    "keywords": sorted(keywords_by_chat.get(sub.chat_id, [])),
                }
            )
        return result


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


def upsert_push_subscription(
    endpoint: str, p256dh: str, auth: str, filters: dict[str, Any] | None = None
) -> None:
    """Bir tarayıcının Web Push aboneliğini kaydeder/günceller (bkz.
    src/web_push.py). Aynı `endpoint` zaten varsa (ör. kullanıcı filtresini
    güncelledi/tarayıcı yeniden subscribe oldu) satır GÜNCELLENİR, yeni bir
    kayıt oluşturulmaz - `filters` verilmezse (Faz 1: henüz filtre seçilmedi)
    mevcut değer (varsa) KORUNUR, None'a sıfırlanmaz."""
    now = datetime.now(timezone.utc)
    with get_session() as session:
        existing = session.query(PushSubscription).filter_by(endpoint=endpoint).one_or_none()
        if existing is None:
            session.add(
                PushSubscription(
                    endpoint=endpoint,
                    p256dh=p256dh,
                    auth=auth,
                    filters=json.dumps(filters, ensure_ascii=False) if filters is not None else None,
                    created_at=now,
                    last_seen_at=now,
                )
            )
            logger.info("Yeni Web Push aboneliği: endpoint=...%s", endpoint[-24:])
        else:
            existing.p256dh = p256dh
            existing.auth = auth
            if filters is not None:
                existing.filters = json.dumps(filters, ensure_ascii=False)
            existing.last_seen_at = now


def remove_push_subscription(endpoint: str) -> bool:
    """Bir Web Push aboneliğini siler (kullanıcı bildirimleri kapattığında
    VEYA push servisi 404/410 "artık geçerli değil" dediğinde - bkz.
    src/web_push.py > send_push_notification). Döner: silindiyse True."""
    with get_session() as session:
        existing = session.query(PushSubscription).filter_by(endpoint=endpoint).one_or_none()
        if existing is None:
            return False
        session.delete(existing)
        logger.info("Web Push aboneliği silindi: endpoint=...%s", endpoint[-24:])
        return True


def get_all_push_subscriptions() -> list[PushSubscription]:
    """Eşleşme kontrolü için TÜM tarayıcı push aboneliklerini döner (bkz.
    src/web_push.py > notify_matching_push_subscriptions)."""
    with get_session() as session:
        return session.query(PushSubscription).all()


def was_push_notified(endpoint: str, group_key: str) -> bool:
    """Bu (endpoint, group_key) çiftine daha önce push bildirimi gönderilip
    gönderilmediğini kontrol eder (tekrar bildirim önleme)."""
    with get_session() as session:
        return (
            session.query(PushNotification)
            .filter_by(endpoint=endpoint, group_key=group_key)
            .one_or_none()
            is not None
        )


def mark_push_notified(endpoint: str, group_key: str) -> None:
    """(endpoint, group_key) çiftini "bildirildi" olarak işaretler (idempotent)."""
    with get_session() as session:
        existing = session.query(PushNotification).filter_by(endpoint=endpoint, group_key=group_key).one_or_none()
        if existing is not None:
            return
        session.add(
            PushNotification(
                endpoint=endpoint,
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


# --------------------------------------------------------------------------
# Genel/dış API anahtarları (bkz. src/web/api_v1.py) - dashboard'un iç
# kullandığı hiçbir mekanizmayla İLİŞKİLİ DEĞİL.
# --------------------------------------------------------------------------


def create_api_key(label: str, tier: str = "standard") -> str:
    """Yeni bir API anahtarı üretir (kriptografik olarak güvenli, URL-safe,
    43 karakter - `secrets.token_urlsafe(32)`), veritabanına kaydeder ve
    ÜRETİLEN HAM DEĞERİ döner - bu, anahtarın açık metin olarak görülebildiği
    TEK andır, bir daha hiçbir yerden okunamaz/gösterilemez (yalnızca
    `X-API-Key` header'ıyla karşılaştırılır, bkz. get_api_key_by_value)."""
    key = secrets.token_urlsafe(32)
    with get_session() as session:
        session.add(
            ApiKey(
                key=key,
                label=label,
                tier=tier,
                created_at=datetime.now(timezone.utc),
                total_request_count=0,
                is_active=True,
            )
        )
    return key


def get_api_key_by_value(key: str) -> ApiKey | None:
    """Bir `X-API-Key` header değerini doğrular - aktif VE var olan bir
    anahtarsa `ApiKey` satırını, değilse None döner (bkz.
    src/web/api_v1.py > require_api_key)."""
    with get_session() as session:
        return session.query(ApiKey).filter_by(key=key, is_active=True).one_or_none()


def record_api_key_usage(key: str) -> None:
    """Bir API anahtarının `last_used_at`/`total_request_count` alanlarını
    günceller - rate-limit KARARI için DEĞİL (o, süreç-içi/in-memory bir
    sayaçla veriliyor, bkz. src/web/api_v1.py), sadece kalıcı kullanım
    istatistiği için. Hiçbir durumda exception fırlatmaz - bu kayıt
    başarısız olsa bile GERÇEK API isteği engellenmemeli."""
    try:
        with get_session() as session:
            row = session.query(ApiKey).filter_by(key=key).one_or_none()
            if row is None:
                return
            row.last_used_at = datetime.now(timezone.utc)
            row.total_request_count = (row.total_request_count or 0) + 1
    except Exception:  # noqa: BLE001 - kullanım istatistiği asla gerçek isteği engellemesin
        logger.exception("API anahtarı kullanım istatistiği güncellenemedi.")
