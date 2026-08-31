"""Gemini veya Anthropic (Claude) API kullanarak haber özetleme + önem
skorlama. Hangi sağlayıcının kullanılacağı `config.yaml > summarizer.llm_provider`
ile seçilir (varsayılan: "gemini"); tek satır değiştirerek "anthropic"a
geri dönülebilir - her iki sağlayıcı da aynı prompt'u ve aynı JSON çıktı
şemasını kullanır.

Gereksinim #3: Özetler kesinlikle haberin birebir kopyası olmamalı, model
kendi cümleleriyle (telif hakkına dikkat ederek) 2-4 cümlelik bir özet ve
varsa madde madde "önemli noktalar" üretmelidir.

Önem skorlama: Ekstra bir API çağrısı yapmadan, AYNI çağrıda modelden 1-5
arası bir önem skoru ve kısa bir gerekçe de istenir (tek JSON yanıtı içinde).
Bu skor, config.yaml > importance.threshold değerini geçen haberlerin
Telegram'a bildirilmesi için kullanılır (bkz. src/notifier.py, src/main.py).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from src.models import NewsGroup
from src.tradingview import find_valid_bist_symbol, validate_symbol as validate_tradingview_symbol

logger = logging.getLogger(__name__)

# src/fetchers/kap_fetcher.py > KAP_SOURCE_NAME ile AYNI değer olmalıdır - bir
# grubun KAP kaynaklı olup olmadığına (bkz. summarize_group, _build_user_prompt)
# karar vermede kullanılır. src/notifier.py > _KAP_SOURCE_NAME'deki AYNI desen:
# yerel bir sabit olarak tutulur, bu modülün kap_fetcher'a bağımlı olmasını
# gerektirmez.
_KAP_SOURCE_NAME = "KAP"

# Google'ın Gemini ücretsiz katman GÜNLÜK (RPD) kotası Pasifik saatine göre
# resetlenir (gerçek çalıştırmada gözlemlendi/doğrulandı, bkz. README) -
# bu yüzden "gün" sınırı UTC/yerel saat değil, bu saat dilimine göre hesaplanır.
_QUOTA_RESET_TZ = ZoneInfo("America/Los_Angeles")
_QUOTA_STATE_FILENAME = "gemini_daily_quota_state.json"

SYSTEM_PROMPT = """\
Sen finansal haberleri özetleyen ve önemini değerlendiren bir asistansın. \
Sana bir veya birden fazla kaynaktan gelen, aynı konudaki haber \
başlık(lar)ı ve kısa açıklama metinleri verilecek.

Özetleme kuralları:
- Kesinlikle kaynak metni birebir/kelimesi kelimesine kopyalama. Her zaman \
kendi cümlelerinle, parafraze ederek yaz (telif hakkı nedeniyle bu zorunludur).
- Özet SADECE "ne oldu"yu değil, metinden çıkarılabiliyorsa "NEDEN önemli" \
ve "olası SONUÇLARI/ETKİLERİ"ni de kısaca içersin - ör. bir faiz kararı \
haberinde sadece "faiz sabit tutuldu" değil, bu kararın piyasalar/döviz/\
kredi maliyetleri üzerindeki olası etkisine de kısaca değin. Metinde bu \
tür bir bağlam/etki hiç YOKSA bunu UYDURMA - sadece olguyu özetle.
- Eğer metinden anlaşılıyorsa, haberin daha büyük bir trendin/gelişmenin \
parçası olup olmadığını (ör. "art arda üçüncü ayda görülen bir düşüş", \
"şirketin son bir yıldır süren yeniden yapılanmasının bir parçası" gibi) \
kısaca belirt - metinde böyle bir bağlam yoksa UYDURMA, atla.
- Özet UZUNLUĞU önem skoruna (aşağıdaki importance_score) göre ölçeklenir:
  - Skor 4-5: 4-6 cümle - konuyu GERÇEKTEN anlamaya yetecek derinlikte \
(ne oldu + neden önemli + olası etki/bağlam).
  - Skor 3: 3-4 cümle.
  - Skor 1-2: 2-3 cümle - kısa/öz kalsın, rutin bir haberi GEREKSİZ YERE \
UZATMA.
- Türkçe yaz.
- "Önemli noktalar" (key_points) listesinde SOMUT/SPESİFİK detayları \
(rakamlar, yüzdeler, tarihler, kişi/şirket/kurum isimleri, karar metinleri) \
ÖNCELİKLENDİR - "şirket büyüme kaydetti" gibi muğlak ifadeler YERİNE \
"gelir %12 artışla 4.2 milyar dolara ulaştı" gibi somut ifadeler kullan. \
Metinde böyle somut detaylar yoksa boş liste döndür, uydurma.
- Sadece verilen metne dayan; uydurma bilgi ekleme.

Önem skorlama kuralları (importance_score, 1-5 arası tam sayı):
- 5 = Piyasayı doğrudan ve güçlü şekilde etkileyebilecek gelişme: merkez \
bankası faiz kararı, beklenmedik enflasyon/işsizlik verisi, büyük bir döviz/\
piyasa şoku, savaş/jeopolitik kriz, sistemik önemde bir şirketin iflası vb.
- 4 = Önemli ama 5 kadar acil olmayan gelişme: büyük bir şirketin çeyreklik \
sonuçları beklentilerden ciddi sapıyorsa, önemli bir düzenleyici karar, \
büyük çaplı birleşme/satın alma, kritik bir emtia (petrol, altın vb.) \
fiyatında sert hareket.
- 3 = Orta düzeyde ilgi çekici, piyasa katılımcılarının bilmesi faydalı ama \
acil aksiyon gerektirmeyen haber.
- 2 = Rutin şirket/sektör haberi, küçük ölçekli gelişme.
- 1 = Genel/ilgisiz/finansal önemi düşük haber (yaşam tarzı, spor, dizi/\
film vb. finans sitesinde görünse bile).
- importance_reason: skoru 1 kısa cümleyle (Türkçe) gerekçelendir.

Bölge etiketleme kuralları (regions, bir liste):
- Haberin hangi bölge(ler)i ilgilendirdiğine, SADECE İÇERİĞİNE bakarak karar \
ver — haberin geldiği KAYNAĞA göre değil (ör. CNBC-e'de yayınlanmış bir ABD \
şirketi haberi "abd" olarak etiketlenmeli, "turkiye" değil).
- Şu kategorilerden bir veya birden fazlasını seç: "turkiye" (Türkiye \
ekonomisi/piyasaları/şirketleri/hükümeti ile ilgiliyse), "abd" (ABD \
ekonomisi/Fed/şirketleri/piyasaları ile ilgiliyse), "avrupa" (AB/Euro \
Bölgesi/İngiltere/Avrupa ülkeleri ile ilgiliyse), "asya" (Çin/Japonya/\
Hindistan vb. Asya ülkeleri ile ilgiliyse), "diger" (yukarıdakilerin \
hiçbirine net şekilde girmiyorsa veya küresel/genel bir haberse).
- Haber birden fazla bölgeyi ilgilendiriyorsa (ör. hem ABD hem Avrupa \
piyasalarını etkileyen bir haberse) birden fazla etiket seç.
- Liste asla boş olmasın; hiçbiri net değilse ["diger"] kullan.

Duygu (sentiment) etiketleme kuralları:
- Haberin piyasa/ekonomi açısından etkisinin GENEL YÖNÜNÜ değerlendir: \
"pozitif" (ör. güçlü kâr açıklaması, faiz indirimi, olumlu veri, anlaşma/\
ortaklık), "negatif" (ör. faiz artışı, kötü veri, kriz, iflas, zarar \
açıklaması, jeopolitik gerilim) veya "notr" (rutin bir duyuru, net bir \
yön belirtmeyen haber) - üçünden sadece birini seç.

Sektör etiketleme kuralları (sector, bir liste):
- Haberin hangi sektör(ler)le ilgili olduğuna karar ver. Şu kategorilerden bir \
veya birden fazlasını seç: "teknoloji", "enerji", "finans" (bankacılık, \
sigorta, yatırım fonları vb.), "otomotiv", "perakende", "saglik", "savunma", \
"gayrimenkul", "tarim", "kripto" (Bitcoin/Ethereum gibi kripto paralar, \
blockchain, borsalar - genel "finans"tan AYRI, çünkü kullanıcı bu haberleri \
özel bir sekmede ayrıca listeliyor), "diger" (yukarıdakilerin hiçbirine net \
şekilde girmiyorsa veya genel/makroekonomik bir haberse).
- Haber birden fazla sektörü ilgilendiriyorsa (ör. hem enerji hem otomotiv \
şirketlerini etkileyen bir haberse) birden fazla etiket seç.
- Liste asla boş olmasın; hiçbiri net değilse ["diger"] kullan.

Piyasa Etkisi (market_impact) kuralları:
- Haberin finans piyasalarına yansımasını bir finans uzmanı/analist gözüyle 1 cümlelik beklenti veya yorumla belirt. Eğer haber piyasa için çok anlamsızsa boş bırak (""). Örn: "Fed'in faiz şahin tutumu nedeniyle altın tarafında kısa vadede sert bir satış baskısı görebiliriz."
- ÖNEMLİ: Gelen haber İngilizce veya başka yabancı bir dilde olsa bile çıktılarını KESİNLİKLE Türkçe olarak oluştur. Herhangi bir yabancı dilde sonuç döndürme.

Üst kategori etiketleme kuralları (top_category, TEK bir değer - `sector`/
`regions`'tan FARKLI ve BAĞIMSIZ bir sınıflandırma eksenidir):
- "makro": Haberin ANA konusu makroekonomi/para politikasıysa - merkez \
bankası (Fed, TCMB, ECB vb.) faiz kararı, enflasyon/işsizlik/büyüme verisi, \
döviz kuru rejimi, bütçe/maliye politikası gibi GENEL ekonomiyi ilgilendiren \
(belirli TEK bir şirketle ilgili OLMAYAN) haberler.
- "sirket": Haberin ANA konusu BELİRLİ bir şirket/marka ise (ör. Tesla, \
Turkcell, bir bankanın çeyreklik kâr açıklaması, bir CEO değişikliği, bir \
birleşme/satın alma) - şirket ismi/hissesi haberin merkezindeyse bunu seç.
- "siyasi": Haberin ANA konusu siyasi/jeopolitik bir gelişmeyse (ör. \
ABD-İran gerilimi, seçimler, yaptırımlar, savaş/çatışma, uluslararası \
anlaşmalar) - ekonomik etkisi olsa bile TEMEL doğası siyasi/jeopolitikse.
- "diger": Yukarıdaki üçüne de NET şekilde girmiyorsa (ör. genel piyasa \
özeti, emtia fiyat hareketi, teknoloji/bilim haberi, spor/yaşam tarzı).
- Yalnızca EN BASKIN/ANA temayı yansıtan TEK bir değer seç (liste değil).

Borsa/Ticker tespiti (company_ticker, TEK bir string - SADECE top_category \
"sirket" olan haberlerde anlamlıdır):
- Haberin ANA konusu olan, halka açık/borsada işlem gören BELİRLİ bir şirket \
net şekilde belirtiliyorsa, o şirketin işlem gördüğü borsa kodu ve ticker \
sembolünü "BORSA: SEMBOL" formatında ver - ör. "NASDAQ: TSLA", "NYSE: XOM", \
"BIST: THYAO", "LSE: BP".
- Haberde birden fazla şirket geçiyorsa, haberin ASIL konusu olan/en öne \
çıkan şirketi seç.
- Şirket borsada işlem görmüyorsa, net bir şirket/ticker belirlenemiyorsa \
veya haber "sirket" kategorisinde değilse boş string ("") döndür - ASLA \
tahmin/uydurma bir sembol üretme, emin değilsen boş bırak.

TradingView sembolü tespiti (trading_view_symbol, TEK bir string):
- Haberin/bildirimin konusunu dashboard'da bir "Teknik Görünüm" butonuyla \
TradingView'e bağlamak için, haberin en doğru TradingView sembolünü \
TradingView'in KENDİ format kurallarına uygun "BORSA:SEMBOL" biçiminde ver.
- Bu alan company_ticker'dan (SADECE "sirket" haberlerinde anlamlı) DAHA \
GENİŞ kapsamlıdır - haberin konusu NE olursa olsun (bir şirket, bir döviz \
çifti, bir emtia, bir endeks, bir tahvil/faiz konusu) en doğru sembolü bul:
  - Belirli bir şirket/hisse haberiyse: o şirketin borsa:ticker'ı, ör. \
"NASDAQ:NFLX", "NYSE:XOM", "BIST:THYAO".
  - İki ülke/para birimi arasındaki bir ilişkiyi (ör. tahvil getirisi, \
ticaret, faiz farkı) konu alan bir haberse: ilgili döviz paritesi, ör. \
"ABD-Japon tahvil piyasası" haberinde "FX:USDJPY".
  - Bir emtia (altın, petrol, bakır, doğalgaz vb.) fiyat hareketiyle \
ilgiliyse: o emtianın sembolü, ör. altın için "TVC:GOLD" veya \
"OANDA:XAUUSD", ham petrol için "TVC:USOIL".
  - Bir borsa endeksi/piyasa geneliyle ilgiliyse: ilgili endeks sembolü, \
ör. "NASDAQ" endeksi için "NASDAQ:IXIC", S&P 500 için "SP:SPX".
- Haber GENEL/SOYUT bir konuysa (belirli TEK bir sembolle net şekilde \
ilişkilendirilemiyorsa - ör. genel bir makroekonomi yorumu, bir siyasi \
gelişme, birden fazla farklı varlığı aynı anda ilgilendiren dağınık bir \
haber) boş string ("") döndür.
- ASLA tahmin/uydurma bir sembol üretme - GERÇEKTEN var olduğundan emin \
olmadığın bir sembolü YAZMA, emin değilsen boş bırak.

Başlık çevirisi (title_tr):
- Yukarıdaki bloklardan İLKİNİN "Başlık:" alanı, bu haberin dashboard'da/\
Telegram'da GÖSTERİLECEK ana/temsili başlığıdır.
- Bu başlık ZATEN Türkçe İSE title_tr'yi boş string ("") bırak - gereksiz \
çeviri YAPMA.
- Bu başlık Türkçe DEĞİLSE (İngilizce veya başka bir dilde), title_tr \
alanına o başlığın DOĞAL/akıcı, gazetecilik diline uygun Türkçe çevirisini \
yaz - kelimesi kelimesine birebir çeviri değil, anlamı doğru aktaran bir \
çeviri olsun.

Yanıtını SADECE aşağıdaki JSON şemasına uygun, başka hiçbir açıklama \
olmadan döndür:

{"summary": "...", "key_points": ["...", "..."], "importance_score": 3, "importance_reason": "...", "regions": ["turkiye"], "sector": ["finans"], "sentiment": "notr", "market_impact": "...", "top_category": "makro", "company_ticker": "", "title_tr": "", "trading_view_symbol": ""}
"""

# KAP (Kamuyu Aydınlatma Platformu) özel durum açıklamaları için AYRI bir
# önem skorlama rubriği (2026-08, kullanıcı isteği): SYSTEM_PROMPT'taki genel
# ölçütler (Fed faiz kararı, savaş/jeopolitik kriz, büyük emtia hareketi vb.)
# bir şirket özel durum açıklamasına doğrudan uygulanamıyordu - ör. bir
# "temettü dağıtımı" veya "sermaye artırımı" kararının genel rubrikte hangi
# puana denk geldiği BELİRSİZDİ, KAP kayıtları da genel haberlerle AYNI
# ölçütle (ve dolayısıyla genelde düşük/rastgele skorlarla) değerlendiriliyordu.
#
# KAP_SYSTEM_PROMPT, SYSTEM_PROMPT'un KENDİSİ üzerinde bir .replace() ile
# üretilir - bu YENİ bir string döndürür, SYSTEM_PROMPT'u DEĞİŞTİRMEZ. Böylece
# özetleme/bölge/sektör/duygu/top_category/ticker/title_tr kuralları ve JSON
# şeması BİREBİR AYNI kalır, SADECE "Önem skorlama kuralları" bölümü KAP'a
# özgü, yatırımcı-kritikliğine göre sıralanmış bir rubrikle değiştirilir (bkz.
# summarize_group > system_prompt seçimi). Genel/KAP-dışı haberlerin
# skorlaması bu değişiklikten HİÇ ETKİLENMEZ (bkz. scratch_kap_prompt_test.py
# regresyon kontrolü: SYSTEM_PROMPT nesnesi bu dosyada başka hiçbir yerde
# mutasyona uğramaz).
_GENERAL_IMPORTANCE_RUBRIC = """Önem skorlama kuralları (importance_score, 1-5 arası tam sayı):
- 5 = Piyasayı doğrudan ve güçlü şekilde etkileyebilecek gelişme: merkez \
bankası faiz kararı, beklenmedik enflasyon/işsizlik verisi, büyük bir döviz/\
piyasa şoku, savaş/jeopolitik kriz, sistemik önemde bir şirketin iflası vb.
- 4 = Önemli ama 5 kadar acil olmayan gelişme: büyük bir şirketin çeyreklik \
sonuçları beklentilerden ciddi sapıyorsa, önemli bir düzenleyici karar, \
büyük çaplı birleşme/satın alma, kritik bir emtia (petrol, altın vb.) \
fiyatında sert hareket.
- 3 = Orta düzeyde ilgi çekici, piyasa katılımcılarının bilmesi faydalı ama \
acil aksiyon gerektirmeyen haber.
- 2 = Rutin şirket/sektör haberi, küçük ölçekli gelişme.
- 1 = Genel/ilgisiz/finansal önemi düşük haber (yaşam tarzı, spor, dizi/\
film vb. finans sitesinde görünse bile).
- importance_reason: skoru 1 kısa cümleyle (Türkçe) gerekçelendir."""

_KAP_IMPORTANCE_RUBRIC = """Önem skorlama kuralları — KAP (Kamuyu Aydınlatma Platformu) özel durum \
açıklaması (importance_score, 1-5 arası tam sayı):
- 5 = Şirketin gidişatını temelden değiştirebilecek açıklama: iflas/\
tasfiye/konkordato, halka arz (IPO) onayı/kesinleşmesi, büyük çaplı \
birleşme/devralma kesinleşmesi, önemli bir soruşturma/yaptırım/kayyum \
ataması, şirket cirosunun büyük kısmını etkileyecek ölçekte kritik \
sözleşme/ihale kazanımı-kaybı, CEO/YK başkanı gibi üst düzey ani istifa/\
görevden alma.
- 4 = Yatırımcı kararını doğrudan etkileyebilecek önemli açıklama: bedelli/\
bedelsiz sermaye artırımı, temettü dağıtım kararı (özellikle ilk kez veya \
beklenmedik), önemli bir stratejik ortaklık/yatırım anlaşması, beklenmedik \
ölçüde büyük finansal sonuç sapması, kredi derecelendirme değişikliği, \
önemli bir yönetim kurulu üyesi değişikliği.
- 3 = Bilgilendirici ama acil olmayan: rutin yönetim kurulu kararları, \
orta ölçekli sözleşme/ihale kazanımları, olağan genel kurul toplantı \
sonuçları (sürpriz içermeyen), şube açılışı/kapanışı.
- 2 = Prosedürel/rutin şirket işlemi: unvan değişikliği tescili, imza \
sirküleri, bağımsız denetim firması seçimi/onayı, standart bilgilendirme \
güncellemesi.
- 1 = Tamamen prosedürel/gündem niteliğinde, yatırımcı için pratik bilgi \
taşımayan: toplantı çağrısı/gündemi, teknik/idari düzeltme bildirimi.
- importance_reason: skoru 1 kısa cümleyle (Türkçe) gerekçelendir."""

assert _GENERAL_IMPORTANCE_RUBRIC in SYSTEM_PROMPT, (
    "SYSTEM_PROMPT metni beklenenden farklı - _GENERAL_IMPORTANCE_RUBRIC "
    "bulunamadı, KAP_SYSTEM_PROMPT türetilemez."
)
KAP_SYSTEM_PROMPT = SYSTEM_PROMPT.replace(_GENERAL_IMPORTANCE_RUBRIC, _KAP_IMPORTANCE_RUBRIC, 1)

# KAP bildirim türü/kategori sınıflandırması (2026-08, kullanıcı isteği - bkz.
# VALID_KAP_CATEGORIES). Bu bölüm SADECE KAP_SYSTEM_PROMPT'a eklenir (bir
# sonraki .replace() ile) - genel SYSTEM_PROMPT'a HİÇ dokunulmaz. Kategori
# ataması ÖNCELİKLE kural tabanlıdır (bkz. _rule_based_kap_category, KAP'ın
# kendi `subject` taksonomisine dayanır - ~%77'yi ücretsiz/güvenilir şekilde
# kapsar, 3 aylık gerçek veriyle doğrulandı). Bu blok, kural eşleşmediğinde
# (ör. subject="Özel Durum Açıklaması (Genel)" gibi KAP'ın kendisinin de
# genel bir kovaya attığı ~%23'lük kesim, ya da Birleşme/Yeni İş İlişkisi
# gibi ayrı bir kova AÇILMAYIP "diger"e dahil edilen az sayıdaki tür)
# devreye giren LLM YEDEĞİ içindir - AYNI çağrıda, ek bir API isteği YOK.
_KAP_CATEGORY_INSTRUCTIONS = """KAP bildirim türü sınıflandırma kuralları (kap_category, TEK bir değer):
- "sermaye_artirimi": Sermaye artırımı/azaltımı, bedelli/bedelsiz sermaye \
artırımı, ihraç tavanı, kayıtlı sermaye tavanı ile ilgili bildirimler.
- "temettu": Kâr payı/temettü dağıtım kararı veya dağıtılmama kararı \
bildirimleri.
- "genel_kurul": Genel kurul toplantı çağrısı, gündemi veya sonuçlarına \
ilişkin bildirimler.
- "finansal_rapor": Finansal rapor, faaliyet raporu, sorumluluk beyanı, \
sürdürülebilirlik raporu gibi periyodik/rutin raporlama bildirimleri.
- "borclanma_araci": Tahvil/bono gibi pay dışı sermaye piyasası araçlarının \
ihracı, kupon ödemesi, itfası ile ilgili bildirimler.
- "hukuki": Dava açılması/gelişmeleri, kayyım ataması/kaldırılması, tedbir \
kararı, faaliyetin durdurulması, soruşturma/yaptırım gibi hukuki/idari \
süreçler.
- "yonetim_kurumsal": Yönetim kurulu üye/komite değişiklikleri, esas \
sözleşme tadili, bağımsız denetim kuruluşu seçimi, unvan/merkez değişikliği \
gibi kurumsal yapı bildirimleri.
- "diger": Yukarıdakilerin hiçbirine net şekilde girmiyorsa (ör. birleşme/\
devralma, yeni iş ilişkisi/sözleşme, ihale, pay alım-satımı, kredi \
derecelendirmesi, olağandışı fiyat hareketi gibi daha az sık görülen veya \
karma içerikli bildirimler)."""

_JSON_SCHEMA_LINE = (
    '{"summary": "...", "key_points": ["...", "..."], "importance_score": 3, '
    '"importance_reason": "...", "regions": ["turkiye"], "sector": ["finans"], '
    '"sentiment": "notr", "market_impact": "...", "top_category": "makro", '
    '"company_ticker": "", "title_tr": "", "trading_view_symbol": ""}'
)
_KAP_JSON_SCHEMA_LINE = (
    '{"summary": "...", "key_points": ["...", "..."], "importance_score": 3, '
    '"importance_reason": "...", "regions": ["turkiye"], "sector": ["finans"], '
    '"sentiment": "notr", "market_impact": "...", "top_category": "makro", '
    '"company_ticker": "", "title_tr": "", "trading_view_symbol": "", '
    '"kap_category": "diger", "short_summary": ""}'
)

for _label, _needle in (("_JSON_SCHEMA_LINE", _JSON_SCHEMA_LINE),):
    assert _needle in SYSTEM_PROMPT, f"SYSTEM_PROMPT metni beklenenden farklı - {_label} bulunamadı."

KAP_SYSTEM_PROMPT = KAP_SYSTEM_PROMPT.replace(
    "\nBölge etiketleme kuralları", f"\n{_KAP_CATEGORY_INSTRUCTIONS}\n\nBölge etiketleme kuralları", 1
)
KAP_SYSTEM_PROMPT = KAP_SYSTEM_PROMPT.replace(_JSON_SCHEMA_LINE, _KAP_JSON_SCHEMA_LINE, 1)

# KAP özetlerinde somut sayısal veri kuralı (2026-08-18, kullanıcı isteği):
# gerçek örnekte gözlemlendi - LLM "rutin bir bildirimde bulunmuştur" gibi
# soyut ifadelere kaçıyordu, oysa KAP metninde "4.102.410 TL nominal tutar,
# sermaye payı %8,9613'ten %8,5924'e düştü" gibi somut rakamlar VARDI. Genel
# SYSTEM_PROMPT'taki "Önemli noktalar (key_points)" kuralı SADECE key_points
# listesi için somutluk istiyordu, `summary` metninin kendisi için DEĞİL -
# bu blok özellikle `summary`'nin somutluğunu KAP'a özgü hedefliyor (genel
# haberlerde böyle bir zorunluluk YOK, SYSTEM_PROMPT'a dokunulmadı).
_KAP_NUMERIC_DETAIL_INSTRUCTION = """KAP özetlerinde somut sayısal veri kuralı (summary için):
- Metinde işlem tutarı, önce/sonra oranı (sermaye payı, oy hakkı vb.), \
tarih, hisse adedi, fiyat gibi SOMUT sayısal veriler varsa bunları özete \
MUTLAKA DAHİL ET - "rutin bir bildirimde bulunmuştur" gibi genel/soyut \
ifadelerle GEÇİŞTİRME.
- Örnek istenen format: "[Şirket], [hedef şirket] hisselerinde [tarih] \
tarihinde [tutar] TL'lik satış yaptı; sermaye payı %X'ten %Y'ye, oy hakkı \
%A'dan %B'ye düştü."
- Bu tür sayısal veriler metinde YOKSA uydurma - sadece gerçekten mevcutsa \
dahil et."""

KAP_SYSTEM_PROMPT = KAP_SYSTEM_PROMPT.replace(
    "Sadece verilen metne dayan; uydurma bilgi ekleme.\n\n",
    f"Sadece verilen metne dayan; uydurma bilgi ekleme.\n\n{_KAP_NUMERIC_DETAIL_INSTRUCTION}\n\n",
    1,
)

# short_summary alanı (2026-08-18, kullanıcı isteği): dashboard KAP
# kartlarının vurgulu/ana satırı için, uzun resmi başlık YERİNE kısa bir
# "TICKER: somut eylem/sonuç" özeti. Ticker'ı LLM TAHMİN ETMEZ - kullanıcı
# metnine (bkz. _build_user_prompt) "Ticker (short_summary için kullan): X"
# satırı olarak zaten gömülüyor, model sadece o değeri harfiyen kullanıp
# özet cümlesini kurar. Genel SYSTEM_PROMPT'a dokunulmadı - bu alan SADECE
# KAP_SYSTEM_PROMPT'ta var (bkz. _KAP_JSON_SCHEMA_LINE).
_KAP_SHORT_SUMMARY_INSTRUCTION = """short_summary alanı kuralı:
- Format KESİNLİKLE: "[TICKER]: [somut eylem/sonuç]" - büyük harf ticker, \
iki nokta, tek boşluk, sonra kısa özet cümlesi (nokta İLE bitirme).
- Ticker için kullanıcı metnindeki "Ticker (short_summary için kullan): X" \
satırındaki X değerini HARFİYEN kullan - kendi tahminini ÜRETME. Birden \
fazla ticker satırı YOKTUR, her zaman TEK bir ana ticker verilir.
- Bu satır metinde YOKSA (ticker çözülemedi) short_summary'yi boş string \
("") bırak.
- Özet kısmı en fazla 10-15 kelime, somut/sayısal (varsa tutar, oran, \
tarih) - "rutin bir bildirimde bulunmuştur" gibi soyut ifade KULLANMA.
- Şirket adını TEKRAR ETME (ticker zaten onu temsil ediyor) - doğrudan \
eylem/sonuçla başla.
- Örnekler: "NETAS: 15 milyon dolar tutarında sipariş aldı", \
"GOKNR: Kayyım kararı kaldırıldı", "YKB: Sermaye payı %8,96'dan %8,59'a \
düştü\""""

KAP_SYSTEM_PROMPT = KAP_SYSTEM_PROMPT.replace(
    "Sadece verilen metne dayan; uydurma bilgi ekleme.\n\n",
    f"Sadece verilen metne dayan; uydurma bilgi ekleme.\n\n{_KAP_SHORT_SUMMARY_INSTRUCTION}\n\n",
    1,
)

# Modelden istenebilecek geçerli bölge etiketleri (bkz. SYSTEM_PROMPT).
VALID_REGIONS = ("turkiye", "abd", "avrupa", "asya", "diger")

# KAP bildirim türü kategorileri (bkz. _KAP_CATEGORY_INSTRUCTIONS,
# src/models.py > NewsGroup.kap_category) - kullanıcı kararı (2026-08),
# 3 aylık gerçek KAP verisi analiziyle belirlendi.
VALID_KAP_CATEGORIES = (
    "sermaye_artirimi",
    "temettu",
    "genel_kurul",
    "finansal_rapor",
    "borclanma_araci",
    "hukuki",
    "yonetim_kurumsal",
    "diger",
)

KAP_CATEGORY_LABELS = {
    "sermaye_artirimi": "💰 Sermaye Artırımı",
    "temettu": "💵 Temettü/Kâr Payı",
    "genel_kurul": "🏛️ Genel Kurul",
    "finansal_rapor": "📊 Finansal/Faaliyet Raporu",
    "borclanma_araci": "📄 Borçlanma Aracı/Tahvil",
    "hukuki": "⚖️ Hukuki/Yaptırım",
    "yonetim_kurumsal": "🏢 Yönetim/Kurumsal Yapı",
    "diger": "📋 Diğer",
}

# KAP'ın KENDİ `subject` taksonomisinden (bkz. src/fetchers/kap_fetcher.py >
# kap_subject) bizim 8 kategorimize KURAL TABANLI eşleme - 3 aylık gerçek
# veri analiziyle (2026-08-17) belirlendi, ~%77'lik kesimi ücretsiz/%100
# tutarlı şekilde kapsar. Burada OLMAYAN bir subject (ör. "Özel Durum
# Açıklaması (Genel)", "Yeni İş İlişkisi", "Birleşme İşlemlerine İlişkin
# Bildirim") KASITLI OLARAK eşlenmez - bunlar için LLM'in kap_category
# yanıtına düşülür (bkz. _rule_based_kap_category, summarize_group).
_KAP_SUBJECT_CATEGORY_MAP = {
    "Sermaye Artırımı - Azaltımı İşlemlerine İlişkin Bildirim": "sermaye_artirimi",
    "İhraç Tavanına İlişkin Bildirim": "sermaye_artirimi",
    "Kayıtlı Sermaye Tavanı İşlemlerine İlişkin Bildirim": "sermaye_artirimi",
    "Kar Payı Dağıtım İşlemlerine İlişkin Bildirim": "temettu",
    "Genel Kurul İşlemlerine İlişkin Bildirim": "genel_kurul",
    "Genel Kurul Bildirimi": "genel_kurul",
    "Finansal Rapor": "finansal_rapor",
    "Sorumluluk Beyanı (Konsolide Olmayan)": "finansal_rapor",
    "Sorumluluk Beyanı (Konsolide)": "finansal_rapor",
    "Faaliyet Raporu (Konsolide Olmayan)": "finansal_rapor",
    "Faaliyet Raporu (Konsolide)": "finansal_rapor",
    "TSRS Uyumlu Sürdürülebilirlik Raporu": "finansal_rapor",
    "Pay Dışında Sermaye Piyasası Aracı İşlemlerine İlişkin Bildirim (Faiz İçeren)": "borclanma_araci",
    "Pay Dışında Sermaye Piyasası Aracı İşlemlerine İlişkin Bildirim (Faizsiz)": "borclanma_araci",
    "Ortaklık Aleyhine Dava Açılması veya Davaya İlişkin Gelişmeler": "hukuki",
    "Faaliyetlerin Kısmen veya Tamamen Durdurulması ya da İmkansız Hale Gelmesi": "hukuki",
    "Sermaye Piyasası Kurulu Tedbir Kararı": "hukuki",
    "Bağımsız Denetim Kuruluşunun Belirlenmesi": "yonetim_kurumsal",
    "Yönetim Kurulu Komiteleri": "yonetim_kurumsal",
    "Esas Sözleşme Tadili": "yonetim_kurumsal",
    "Kurumsal Yönetim İlkelerine Uyum Derecelendirmesi": "yonetim_kurumsal",
    "Şirket Merkezi Değişikliği": "yonetim_kurumsal",
}


def _rule_based_kap_category(subject: str) -> str | None:
    """`_KAP_SUBJECT_CATEGORY_MAP`'te tam eşleşme varsa kategoriyi döner,
    yoksa None (çağıran taraf LLM'in kap_category yanıtına düşer)."""
    return _KAP_SUBJECT_CATEGORY_MAP.get((subject or "").strip())


# Türkçeye özgü harfler (ğ/ş/ı/İ - Almanca/başka dillerde neredeyse hiç
# geçmez, ö/ü'den FARKLI olarak) - herhangi biri geçiyorsa metin neredeyse
# kesin Türkçe. Kısa/az karakterli başlıklarda bu tek başına yetersiz
# kalabileceğinden (bkz. altındaki not), yaygın Türkçe işlev kelimeleriyle
# birlikte kullanılır.
_TURKISH_CHARS_RE = re.compile(r"[ğşıİÇç]")
_TURKISH_STOPWORDS = {
    "ve", "bir", "bu", "da", "de", "ile", "için", "olan", "olarak", "kadar",
    "göre", "sonra", "önce", "gibi", "ise", "ama", "çok", "daha", "en",
    "yeni", "büyük", "türkiye", "şirket", "şirketi", "hakkında", "başladı",
    "açıkladı", "arttı", "düştü", "milyon", "milyar",
}


def _looks_turkish(text: str) -> bool:
    """`title_tr` (2026-08-18, kullanıcı isteği): LLM'in kendi "başlık zaten
    Türkçeyse title_tr'yi boş bırak" talimatına (bkz. SYSTEM_PROMPT) HER ZAMAN
    uymadığı gözlemlendi (bazı zaten-Türkçe başlıklarda gereksiz bir
    "çeviri" üretiyordu) - bu, LLM'in yanıtından BAĞIMSIZ, orijinal başlık
    üzerinde çalışan deterministik bir ikinci kontrol katmanı (bkz.
    summarize_group çağrısı) - `title_tr` LLM ne dönerse dönsün, orijinal
    başlık zaten Türkçe GÖRÜNÜYORSA sessizce bastırılır.

    Ağır bir dil tespiti kütüphanesi (langdetect vb.) BİLİNÇLİ OLARAK
    eklenmedi - haber başlıkları kısa olduğundan basit bir imza (ğ/ş/ı/İ/ç
    harfleri VEYA yaygın Türkçe işlev kelimeleri) pratikte yeterli, %100
    isabet gerekmiyor (tek amaç gereksiz/bariz tekrarı azaltmak)."""
    if not text:
        return False
    if _TURKISH_CHARS_RE.search(text):
        return True
    words = re.findall(r"[a-zçğıöşü]+", text.lower())
    return sum(1 for w in words if w in _TURKISH_STOPWORDS) >= 2


def _kap_primary_ticker_code(stock_codes: str) -> str | None:
    """KAP API'sinin OTORİTER `stockCodes` alanından ("YKB, YKBNK" gibi
    virgülle ayrılmış, bazen birden fazla kod) SADECE ilk/ana kodu, çıplak
    (borsa öneki YOK - bkz. Summarizer._kap_company_ticker_from_stock_codes'un
    "BIST: YKB" formatından FARKLI) döner - `short_summary` alanı (2026-08-18,
    kullanıcı isteği) için prompt'a gömülür, LLM ayrıca tahmin ETMEZ."""
    codes = [c.strip() for c in (stock_codes or "").split(",") if c.strip()]
    return codes[0] if codes else None

# Bölge etiketlerinin dashboard'da gösterilecek Türkçe karşılıkları.
REGION_LABELS = {
    "turkiye": "Türkiye",
    "abd": "ABD",
    "avrupa": "Avrupa",
    "asya": "Asya",
    "diger": "Diğer",
}

# Modelden istenebilecek geçerli sektör etiketleri (bkz. SYSTEM_PROMPT).
VALID_SECTORS = (
    "teknoloji",
    "enerji",
    "finans",
    "otomotiv",
    "perakende",
    "saglik",
    "savunma",
    "gayrimenkul",
    "tarim",
    "kripto",
    "diger",
)

# Sektör etiketlerinin dashboard'da gösterilecek Türkçe karşılıkları.
SECTOR_LABELS = {
    "teknoloji": "Teknoloji",
    "enerji": "Enerji",
    "finans": "Finans",
    "otomotiv": "Otomotiv",
    "perakende": "Perakende",
    "saglik": "Sağlık",
    "savunma": "Savunma",
    "gayrimenkul": "Gayrimenkul",
    "tarim": "Tarım",
    "kripto": "Kripto",
    "diger": "Diğer",
}

# Modelden istenebilecek geçerli duygu etiketleri (bkz. SYSTEM_PROMPT).
VALID_SENTIMENTS = ("pozitif", "negatif", "notr")

# Duygu etiketlerinin dashboard'da gösterilecek Türkçe karşılıkları (emoji + metin).
SENTIMENT_LABELS = {
    "pozitif": "📈 Pozitif",
    "negatif": "📉 Negatif",
    "notr": "➖ Nötr",
}

# "Detaylı İnceleme" sayfasındaki (bkz. src/web/app.py) kategori sekmelerinin
# geçerli değerleri (bkz. SYSTEM_PROMPT > top_category). `sector`/`regions`'tan
# BAĞIMSIZ bir eksen - TEK bir değer (liste değil).
VALID_TOP_CATEGORIES = ("makro", "sirket", "siyasi", "diger")

TOP_CATEGORY_LABELS = {
    "makro": "🏛️ Makro",
    "sirket": "🏢 Şirket",
    "siyasi": "🌍 Siyasi",
    "diger": "📌 Diğer",
}

# Günlük özet için: bir önceki 24 saatte toplanan haberler arasından
# gerçekten değerli/aksiyon alınabilir 5-10 tanesini seçtirmek üzere ayrı bir
# sistem promptu (bkz. Summarizer.select_daily_highlights, src/daily_digest.py).
DAILY_DIGEST_SYSTEM_PROMPT = """\
Sen bir finans haber editörüsün. Sana, son 24 saatte toplanmış bir haber \
listesi verilecek; her satırda sıra numarası (index), önem skoru, başlık ve \
kısa özet bulunuyor.

Görevin: bunlar arasından GERÇEKTEN piyasa/yatırımcı için değerli, aksiyon \
alınabilir, önemli olan 5 ile 10 arası haberi seçmek. Sadece önem skoruna \
göre sıralama YETERLİ DEĞİL - aynı skorda çok haber olabilir; içeriğe bakarak \
gerçekten öne çıkanları seç. Rutin/tekrar eden/önemsiz haberleri eleme. Aday \
sayısı 5'ten azsa sadece uygun olanları seç (asla var olmayan bir haber \
uydurma). Her seçim için 1 kısa cümlelik (Türkçe) bir gerekçe yaz.

Yanıtını SADECE aşağıdaki JSON şemasına uygun, başka hiçbir açıklama \
olmadan döndür:

{"selections": [{"index": 3, "reason": "..."}, {"index": 7, "reason": "..."}]}
"""

# Sesli Günlük Özet için (bkz. src/voice_digest.py, src/tts.py): gece
# (18:00-08:00) penceresinde toplanmış, önem skoru eşiği geçen haberlerden
# TEK bir akıcı, KONUŞMA DİLİNE uygun Türkçe metin ürettirmek üzere ayrı bir
# sistem promptu. DAILY_DIGEST_SYSTEM_PROMPT'tan FARKI: burada "en önemliyi
# seç" işi ZATEN yapılmış (basit skor eşiği ile, bkz. src/voice_digest.py) -
# bu prompt sadece verilen haberleri TEK bir doğal anlatıya dönüştürür,
# madde madde/liste formatı DEĞİL (TTS ile okunacağı için).
#
# `generate_night_digest_script`, haber listesinden ÖNCE isteğe bağlı bir
# YÖNERGE bloğu (kategoriye göre değişen uzunluk/detay talimatı, bkz. o
# metod) ve/veya bir EKONOMİK TAKVİM bloğu (SADECE "genel" kategoride,
# bugün için planlanmış olaylar - bkz. src/voice_digest.py > _today_calendar_lines)
# ekleyebilir; bu prompt aşağıda o iki bloğun VARSA nasıl ele alınacağını
# tarif eder.
NIGHT_DIGEST_SYSTEM_PROMPT = """\
Sen bir finans radyosu spikerisin. Sana, gece boyunca toplanmış ÖNEMLİ \
finans haberlerinin bir listesi verilecek; her satırda önem skoru, başlık \
ve kısa özet bulunuyor. Bu listeden ÖNCE, uyulması GEREKEN bir YÖNERGE \
bloğu ve/veya bir EKONOMİK TAKVİM bloğu da verilebilir.

Görevin: bu haberlerden, SESLİ olarak okunacak, akıcı ve doğal konuşma \
diline uygun TEK bir Türkçe anlatı yazmak - "gece boyunca şunlar oldu..." \
tarzında, sanki dinleyiciye sabah haberlerini özetliyormuşsun gibi. MADDE \
MADDE LİSTE YAZMA, numaralandırma yapma, başlık tekrarlama - doğal cümlelerle \
bağla (ör. "Bu arada...", "Öte yandan...", "Gece saatlerinde de..."). \
Kısaltmaları (ör. "%", "TL", ticker kodları) sesli okunduğunda anlaşılır \
olacak şekilde yaz (ör. "yüzde beş" yerine "%5" yazman sorun değil, TTS bunu \
zaten doğru okur - sadece garip/anlaşılmaz kısaltmalardan kaçın).

Uzunluk/detay seviyesi: YÖNERGE bloğu bunu AÇIKÇA belirtmediği sürece, en \
fazla 1-2 dakikalık bir konuşma uzunluğunda tut (yaklaşık 150-280 kelime) - \
ÇOK UZUN OLMASIN - ve haberleri kısaca, başlık düzeyinde geç. YÖNERGE bloğu \
"daha detaylı" isterse, ORADAKİ talimata uy (kelime sayısı/detay seviyesi \
dahil) - bu durumda varsayılan 150-280 kelime sınırını YOK SAY. Her durumda: \
verilen haberlerin TAMAMINI değil, gerçekten önemli olanları öne çıkar, \
önemsiz/tekrar eden detayları kısaca geç veya atla.

Eğer sana bir EKONOMİK TAKVİM bloğu verildiyse: ana haber anlatısını \
bitirdikten SONRA, "Bugün ayrıca şu ekonomik veriler açıklanacak:" gibi \
doğal bir geçiş cümlesiyle başlayan KISA bir ek paragraf ekle ve o bloktaki \
olayları (saat, ülke, olay adı, varsa önceki değer) doğal cümlelerle say - \
madde madde listeleme. EKONOMİK TAKVİM bloğu verilMEDİYSE bu paragrafı HİÇ \
EKLEME.

Kısa bir karşılama cümlesiyle başla (ör. "Günaydın, gece boyunca piyasalarda \
şunlar yaşandı:") ve kısa bir kapanış cümlesiyle bitir (ör. "İyi günler \
dilerim.").

Yanıtını SADECE aşağıdaki JSON şemasına uygun, başka hiçbir açıklama \
olmadan döndür:

{"script": "..."}
"""

# Şirket/hisse profili sayfası için (bkz. src/company_profile.py, web
# dashboard'daki "Şirket Profili" sayfası): kullanıcının aradığı şirketle
# ilgili son 30 günün haberlerinden TEK bir genel görünüm (outlook) paragrafı
# ürettirmek üzere ayrı bir sistem promptu.
COMPANY_PROFILE_SYSTEM_PROMPT = """\
Sen bir finans analistisin. Sana bir şirket/varlık adı ve o şirketle ilgili \
son 30 günde toplanmış haberlerin başlık+özetleri verilecek.

Görevin: TÜM bu haberlere bakarak, bu şirket hakkında son 30 günün GENEL \
GÖRÜNÜMÜNÜ (outlook) özetleyen 3-5 cümlelik TEK bir paragraf (Türkçe) \
yazmak - hangi konular/temalar öne çıktı, genel duygu/yön ne (olumlu/olumsuz/\
karışık), varsa en dikkat çekici gelişme(ler) neydi. Objektif ve dengeli bir \
üslup kullan, yatırım tavsiyesi verme.

Yanıtını SADECE aşağıdaki JSON şemasına uygun, başka hiçbir açıklama \
olmadan döndür:

{"summary": "..."}
"""

COMMODITY_WEEKLY_SYSTEM_PROMPT = """\
Sen bir emtia piyasaları ve hisse senedi etki analistisin. Sana bir emtianın \
adı, geçen haftaki fiyat değişimi (% ve mutlak) verilecek.

Görevin: bu fiyat hareketinin, YALNIZCA ABD borsalarında (NYSE/NASDAQ) işlem \
gören şirket/sektör türlerini nasıl etkileyebileceğini, SOMUT ÖRNEK ŞİRKET \
İSİMLERİYLE (ör. bakır için "Freeport-McMoRan, Southern Copper" gibi gerçek \
ABD borsa şirketleri) 2-4 cümlede (Türkçe) açıklamak. Hangi sektörlerin \
maliyet baskısı/fayda görebileceğini somutlaştır (ör. "girdi maliyeti \
artan üreticiler" vs "üretici/madenci şirketler için gelir artışı"). \
Kesinlik iddia etme, "olabilir/muhtemelen" gibi temkinli bir üslup kullan - \
yatırım tavsiyesi verme.

Ayrıca, metinde adı geçen şirketlerin GERÇEK borsa ticker'larını (SADECE \
NYSE/NASDAQ'ta gerçekten işlem gören, GERÇEKTEN var olan şirketler - \
uydurma/tahmini ticker YAZMA, bilmiyorsan o şirketi listeye ekleme) ayrı bir \
yapılandırılmış listede de ver - bu, dashboard/Telegram'ın metinden şirket \
adı ayrıştırmaya çalışmadan doğrudan güncel fiyat gösterebilmesi için. En \
fazla 4 şirket, metinde bahsettiğin sırayla.

Yanıtını SADECE aşağıdaki JSON şemasına uygun, başka hiçbir açıklama \
olmadan döndür:

{"analysis": "...", "companies": [{"name": "Freeport-McMoRan", "ticker": "FCX", "exchange": "NYSE"}]}
"""

# "Haber-KAP Bağlantı Haritası" özelliği için (bkz. src/news_links.py):
# aynı company_ticker + yakın zaman penceresiyle bulunmuş bir (KAP bildirimi,
# genel haber) aday çiftinin GERÇEKTEN aynı olayı anlatıp anlatmadığını
# doğrulamak üzere ayrı, küçük bir sistem promptu.
NEWS_LINK_SYSTEM_PROMPT = """\
Sen bir finans haber editörüsün. Sana AYNI şirketle ilgili, yakın zamanda \
yayınlanmış iki ayrı kayıt verilecek: biri KAP (Kamuyu Aydınlatma Platformu) \
resmi bildirimi, diğeri genel basında çıkmış bir haber.

Görevin: bu ikisinin GERÇEKTEN aynı olayı/gelişmeyi anlatıp anlatmadığına \
karar vermek. Örnek "EVET" durumu: KAP'a "büyük sözleşme imzalandı" \
bildirimi yapılmış VE haber de AYNI sözleşmeden bahsediyor. Örnek "HAYIR" \
durumu: ikisi şirketle ilgili ama FARKLI/alakasız konular (ör. biri sermaye \
artırımı, diğeri ayrı bir iş ortaklığı duyurusu). Sadece aynı şirketten \
bahsetmeleri YETERLİ DEĞİL - aynı SOMUT olayı anlatmaları gerekir.

Yanıtını SADECE aşağıdaki JSON şemasına uygun, başka hiçbir açıklama \
olmadan döndür:

{"same_event": true, "reason": "..."}

`reason`: kararının 1 kısa cümlelik (Türkçe) gerekçesi.
"""

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')

# gemini-2.5-flash Google tarafından yeni kullanıcılara kapatıldı ("no longer
# available to new users" hatası). İlk denemede gemini-3.6-flash'a geçildi,
# ANCAK gerçek bir çalıştırmada bunun ücretsiz katımda GÜNLÜK sadece 20 istek
# (RPD) ile sınırlı olduğu ortaya çıktı (hata gövdesinde
# "GenerateRequestsPerDayPerProjectPerModel-FreeTier", quotaValue: 20) - bu,
# ~90 haberlik bir tarama için yetersiz. gemini-2.5-flash-lite de yeni
# kullanıcılara kapatılmış durumda. gemini-3.5-flash-lite ("en hızlı, en
# uygun maliyetli 3.5 modeli" olarak tanıtılıyor) hem çalışıyor hem de "lite"
# modeller yüksek hacimli/ücretsiz kullanım için tasarlandığından çok daha
# yüksek bir günlük kotaya sahip - gerçek bir çalıştırmayla doğrulandı
# (2026-07, bkz. README > Rate Limit Koruması).
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    # Model bazen ```json ... ``` gibi kod bloğu döndürebilir; temizle.
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    match = _JSON_BLOCK_RE.search(text)
    candidate = match.group(0) if match else text
    try:
        # strict=False: Gemini'nin `response_mime_type="application/json"`
        # çıktısı, çok paragraflı/uzun bir metin alanında (bkz.
        # NIGHT_DIGEST_SYSTEM_PROMPT) ARA SIRA kaçışsız (escape edilmemiş) ham
        # newline karakteri bırakıyor - katı JSON'a göre geçersiz bir kontrol
        # karakteri (gerçek bir denemede doğrulandı: "Invalid control
        # character" hatası), ama strict=False bunu (RFC'ye aykırı olsa da,
        # Python json modülünün desteklediği bir esneme) sorunsuz kabul eder.
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        return None


def _extract_retry_delay_seconds(exc: Exception) -> float | None:
    """429 (RESOURCE_EXHAUSTED) yanıtındaki `retryDelay` alanını (ör. "38s")
    okuyup saniyeye çevirir. Google'ın hata gövdesi genelde şu şekildedir:

        {"error": {..., "details": [..., {"@type": "...RetryInfo", "retryDelay": "38s"}]}}

    Tam iç içe yapıyı ayrıştırmak yerine, hatanın (varsa) `details`
    alanını ya da metnini regex ile taramak, küçük yapı farklılıklarına karşı
    daha dayanıklıdır."""
    payload = getattr(exc, "details", None)
    text = json.dumps(payload) if payload is not None else str(exc)
    match = _RETRY_DELAY_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _is_daily_quota_error(exc: Exception) -> bool:
    """Bir 429 hatasının dakika-başına (RPM) geçici bir limitten mi, yoksa
    günlük (RPD - "requests per day") bir kotanın tamamen tükenmesinden mi
    kaynaklandığını ayırt eder. Google'ın quotaId alanı bunu açıkça belirtir,
    ör. "GenerateRequestsPerDayPerProjectPerModel-FreeTier". Bu ayrım önemli:
    günlük kota tükendiğinde `retryDelay` kadar (birkaç saniye/dakika)
    beklemek işe yaramaz - kota ancak ertesi gün sıfırlanır. Bu durumda
    gereksiz yere tekrar denemek yerine hemen vazgeçip fallback'e düşmek
    (bkz. _call_model_with_retry) hem zaman kazandırır hem de kotayı daha
    fazla tüketmez."""
    payload = getattr(exc, "details", None)
    text = json.dumps(payload) if payload is not None else str(exc)
    return "perday" in text.lower().replace(" ", "").replace("-", "").replace("_", "")


class Summarizer:
    def __init__(
        self,
        summarizer_cfg: dict[str, Any],
        api_key: str,
        provider: str = "gemini",
        output_dir: str = "data",
    ):
        if provider not in ("gemini", "anthropic"):
            raise ValueError(f"Bilinmeyen llm_provider: {provider!r} ('gemini' veya 'anthropic' olmalı)")

        self.provider = provider
        self.max_output_tokens = summarizer_cfg.get("max_output_tokens", 900)
        self.language = summarizer_cfg.get("language", "tr")
        self.effort = summarizer_cfg.get("effort", "low")

        # Günlük kota koruması (bkz. modül başındaki _QUOTA_RESET_TZ notu).
        self.daily_quota_guard_enabled = summarizer_cfg.get("daily_quota_guard_enabled", True)
        self._quota_state_path = Path(output_dir) / "state" / _QUOTA_STATE_FILENAME

        # Yedek sağlayıcı (2026-08-21, kullanıcı isteği: "Gemini günlük kotası
        # tükenince Anthropic'e otomatik geçiş") - Gemini'nin günlük kotası
        # tükendiğinde, placeholder'a düşmeden ÖNCE Anthropic (ANTHROPIC_API_KEY
        # .env'de tanımlıysa) denenir (bkz. summarize_group > _quota_exhausted_today
        # dalı, _get_fallback_summarizer). Anthropic'in KENDİ kredisi/kotası da
        # tükenmişse (ör. "credit balance too low") bu SESSİZCE normal
        # placeholder davranışına düşer - hiçbir zaman çalışmayı DURDURMAZ,
        # sadece "bir sağlayıcı daha denendi, o da olmadı" ek bir katman.
        # `self._summarizer_cfg`/`self._output_dir` ikincil Summarizer'ı
        # (provider="anthropic") tembel (lazy) kurmak için saklanıyor - HER
        # grup için değil, İLK ihtiyaç anında bir kez.
        self.quota_fallback_provider_enabled = summarizer_cfg.get("quota_fallback_provider_enabled", True)
        self._summarizer_cfg = summarizer_cfg
        self._output_dir = output_dir
        self._fallback_summarizer: "Summarizer | None" = None
        # Anthropic da başarısız olursa (ör. kredi yok) bu ÇALIŞTIRMA (process
        # ömrü) boyunca bir daha DENENMEZ - aksi halde kota tükenmiş her grup
        # için (potansiyel yüzlerce) boşuna bir Anthropic çağrısı/log satırı
        # üretilirdi. Her yeni tarama turu (bkz. src/main.py > summarize_groups,
        # HER seferinde TAZE bir Summarizer kurar) bu bayrağı sıfırlar - yani
        # Anthropic'e sonradan kredi eklenirse EN GEÇ bir sonraki tarama
        # turunda (varsayılan 15 dk) kendiliğinden fark edilir, süreç yeniden
        # başlatılmasına gerek YOKTUR.
        self._fallback_unavailable = False

        # Rate limit koruması: sağlayıcının dakika başına istek (RPM) limitini
        # aşmamak için istekler arasına otomatik bekleme eklenir (gereksinim:
        # Gemini ücretsiz katmanının düşük RPM limiti - varsayılan 5). 429
        # (RESOURCE_EXHAUSTED) alınırsa ayrıca otomatik tekrar denenir (bkz.
        # _call_model_with_retry). config.yaml > summarizer altından
        # ayarlanabilir/kapatılabilir.
        self.rate_limit_enabled = summarizer_cfg.get("rate_limit_enabled", True)
        self.requests_per_minute = summarizer_cfg.get("requests_per_minute", 5)
        self.max_retries = summarizer_cfg.get("max_retries", 3)
        # %5'lik küçük bir güvenlik payı + sabit 0.5 sn: tam sınırda (ör. 12.00
        # sn) bazen yine de 429 alınabiliyor, hafif bir marj bunu engeller.
        self._min_interval_seconds = (
            (60.0 / self.requests_per_minute) + 0.5 if self.requests_per_minute > 0 else 0.0
        )
        self._last_call_started_at: float | None = None

        if provider == "anthropic":
            self.model = summarizer_cfg.get("anthropic_model", DEFAULT_ANTHROPIC_MODEL)
            self.client = anthropic.Anthropic(api_key=api_key)
        else:
            self.model = summarizer_cfg.get("gemini_model", DEFAULT_GEMINI_MODEL)
            self.client = genai.Client(api_key=api_key)

    def _build_user_prompt(self, group: NewsGroup) -> str:
        parts: list[str] = []
        for item in group.items:
            snippet = item.raw_text.strip()
            # Aşırı uzun ham metinleri kırp (token/maliyet kontrolü)
            if len(snippet) > 1500:
                snippet = snippet[:1500] + "..."
            # KAP kayıtlarında raw_text, KAP'ın kendi RESMİ taksonomi kategori
            # metnini taşır (bkz. src/fetchers/kap_fetcher.py > subject) - bu
            # serbest metin bir "açıklama" değil, bildirimin resmi türü. Genel
            # "Metin:" etiketi bunu ayırt etmiyordu; LLM'e (özellikle
            # KAP_SYSTEM_PROMPT'taki rubrikle) daha net bir sinyal vermek için
            # ayrı bir etiket kullanılır (kullanıcı isteği, 2026-08).
            text_label = (
                "Bildirim Konusu (KAP taksonomisi)" if item.source == _KAP_SOURCE_NAME and snippet else "Metin"
            )
            entry = f"Kaynak: {item.source}\nBaşlık: {item.title}\n{text_label}: {snippet or '(açıklama yok)'}"
            # short_summary alanı için (2026-08-18, kullanıcı isteği) - ticker'ı
            # LLM'e TAHMİN ETTİRMİYORUZ, KAP'ın kendi otoriter stockCodes
            # alanından (bkz. _kap_primary_ticker_code) burada doğrudan
            # veriyoruz; KAP_SYSTEM_PROMPT bu satırı short_summary'nin
            # "[TICKER]: ..." formatında kullanmasını ister (bkz. altındaki
            # _KAP_SHORT_SUMMARY_INSTRUCTION). Ticker çözülemezse (nadir)
            # satır hiç eklenmez - LLM o durumda short_summary'yi boş bırakır.
            if item.source == _KAP_SOURCE_NAME:
                primary_ticker = _kap_primary_ticker_code(item.kap_stock_codes)
                if primary_ticker:
                    entry += f"\nTicker (short_summary için kullan): {primary_ticker}"
            parts.append(entry)
        return "\n\n---\n\n".join(parts)

    def _get_fallback_summarizer(self) -> "Summarizer | None":
        """Gemini günlük kotası tükendiğinde denenecek ikincil (Anthropic)
        Summarizer'ı döner - bkz. __init__'teki not. Şu durumlarda None
        döner (hiçbir zaman exception fırlatmaz):
          - Bu Summarizer'ın kendisi zaten Anthropic ise (yedek yedeklemez,
            sonsuz döngü riski yok çünkü hiç çağrılmaz).
          - `quota_fallback_provider_enabled: false` ise (config.yaml).
          - ANTHROPIC_API_KEY .env'de tanımlı değilse.
          - Bu çalıştırmada Anthropic daha önce denenip başarısız olduysa
            (bkz. _fallback_unavailable)."""
        if self.provider != "gemini" or not self.quota_fallback_provider_enabled:
            return None
        if self._fallback_unavailable:
            return None
        if self._fallback_summarizer is not None:
            return self._fallback_summarizer

        from src.config import get_anthropic_api_key

        try:
            api_key = get_anthropic_api_key()
        except RuntimeError:
            self._fallback_unavailable = True
            return None

        self._fallback_summarizer = Summarizer(
            self._summarizer_cfg, api_key, provider="anthropic", output_dir=self._output_dir
        )
        return self._fallback_summarizer

    def summarize_group(self, group: NewsGroup) -> None:
        """`group.summary`, `group.key_points`, `group.importance_score` ve
        `group.importance_reason` alanlarını TEK bir API çağrısıyla doldurur
        (ekstra bir "skorlama" çağrısı yapılmaz).

        Herhangi bir hata durumunda exception fırlatmaz; hatayı loglar ve
        başlıktan türetilmiş kısa bir yedek (fallback) özet bırakır (önem
        skoru None kalır -> bu haber Telegram eşiğini asla geçmez), böylece
        bir haberin özetlenmesindeki hata tüm çalıştırmayı durdurmaz.
        """
        if self._quota_exhausted_today():
            # Bugün zaten GÜNLÜK kota tükendiği tespit edilmişti (bkz.
            # _mark_quota_exhausted_today) - boşuna ağ çağrısı/429 denemesi
            # yapmadan ÖNCE yedek sağlayıcı (Anthropic) denenir (bkz.
            # _get_fallback_summarizer, 2026-08-21 eklendi) - başarılı olursa
            # bu grup GERÇEK bir skorla döner, günlük ~21 saatlik "kapalı"
            # pencere ortadan kalkar. Yedek yoksa/o da başarısızsa (ör.
            # Anthropic'in kendi kredisi de yoksa) SESSİZCE eski davranışa
            # (ertelenmiş fallback) düşülür - importance_score None kalır,
            # kota resetlenince bir sonraki taramada otomatik tekrar denenir.
            fallback = self._get_fallback_summarizer()
            if fallback is not None:
                logger.info(
                    "Günlük Gemini kotası tükenmiş, '%s' için yedek sağlayıcı (%s) deneniyor.",
                    group.representative.title,
                    fallback.provider,
                )
                fallback.summarize_group(group)
                if group.importance_score is not None:
                    return
                # Yedek de başarısız oldu (ör. Anthropic kredisi de yok) - bu
                # ÇALIŞTIRMA boyunca tekrar denenmesin (bkz. __init__'teki not).
                self._fallback_unavailable = True
                logger.warning(
                    "Yedek sağlayıcı (%s) de başarısız oldu - bu taramanın kalanında "
                    "tekrar denenmeyecek, ertelenmiş fallback'e düşülüyor.",
                    fallback.provider,
                )

            logger.info(
                "Günlük Gemini kotası bugün için tükenmiş durumda, '%s' özetlemesi "
                "erteleniyor (bkz. data/state/%s).",
                group.representative.title,
                _QUOTA_STATE_FILENAME,
            )
            self._apply_fallback(group, quota_deferred=True)
            return

        user_prompt = self._build_user_prompt(group)
        # Grup KAP kaynaklıysa (bkz. _KAP_SOURCE_NAME) KAP'a özgü önem
        # skorlama rubriğini kullanan KAP_SYSTEM_PROMPT seçilir; aksi halde
        # (KAP-dışı TÜM haberler) davranış AYNEN ÖNCEKİ GİBİDİR - varsayılan
        # SYSTEM_PROMPT (bkz. _call_model_with_retry'nin varsayılan parametresi).
        # group.sources (bkz. src/models.py) kaynak listesidir - bir grup
        # nadiren birden fazla kaynaktan birleşmiş olabilir (bkz.
        # deduplicator.py), bu yüzden tam eşitlik yerine "içeriyor mu"
        # kontrolü yapılır (notifier.py > _is_kap_record ile AYNI desen).
        is_kap = _KAP_SOURCE_NAME in group.sources
        system_prompt = KAP_SYSTEM_PROMPT if is_kap else SYSTEM_PROMPT
        try:
            raw_text = self._call_model_with_retry(user_prompt, system_prompt=system_prompt)
        except Exception as exc:  # noqa: BLE001
            logger.error("Özetleme API çağrısı başarısız (%s): %s", group.representative.title, exc)
            self._apply_fallback(group)
            return

        parsed = _extract_json(raw_text)
        if not parsed or "summary" not in parsed:
            logger.warning(
                "Özet yanıtı beklenen JSON formatında değildi (%s), ham metin kullanılıyor.",
                group.representative.title,
            )
            group.summary = raw_text or group.representative.title
            group.key_points = []
            group.importance_score = None
            group.importance_reason = ""
            group.regions = []
            group.sectors = []
            group.sentiment = None
            group.top_category = None
            group.company_ticker = None
            group.title_tr = None
            group.kap_category = None
            group.short_summary = None
            group.trading_view_symbol = None
            group.trading_view_symbol_valid = None
            return

        group.summary = str(parsed.get("summary", "")).strip()
        key_points = parsed.get("key_points", [])
        if isinstance(key_points, list):
            group.key_points = [str(p).strip() for p in key_points if str(p).strip()]
        else:
            group.key_points = []

        group.importance_score = self._parse_importance_score(parsed.get("importance_score"))
        group.importance_reason = str(parsed.get("importance_reason", "")).strip()
        group.regions = self._parse_regions(parsed.get("regions"))
        group.sectors = self._parse_sectors(parsed.get("sector"))
        group.sentiment = self._parse_sentiment(parsed.get("sentiment"))
        group.market_impact = str(parsed.get("market_impact", "")).strip() or None
        group.top_category = self._parse_top_category(parsed.get("top_category"))
        group.company_ticker = self._parse_company_ticker(parsed.get("company_ticker"))
        group.trading_view_symbol = self._parse_trading_view_symbol(parsed.get("trading_view_symbol"))
        group.title_tr = str(parsed.get("title_tr", "")).strip() or None
        # bkz. _looks_turkish notu - LLM'in kendi "zaten Türkçeyse boş
        # bırak" talimatına HER ZAMAN uymadığı gözlemlendi, bu deterministik
        # ikinci kontrol orijinal başlık zaten Türkçe görünüyorsa title_tr'yi
        # LLM ne dönerse dönsün bastırır.
        if group.title_tr and _looks_turkish(group.representative.title):
            group.title_tr = None

        if is_kap:
            # 1) Kategori: ÖNCE KAP'ın kendi `subject` taksonomisine kural
            #    tabanlı eşleme denenir (ücretsiz, %100 tutarlı) - eşleşmezse
            #    (ör. "Özel Durum Açıklaması (Genel)") LLM'in AYNI çağrıda
            #    döndürdüğü kap_category'e düşülür (bkz. _KAP_CATEGORY_INSTRUCTIONS).
            group.kap_category = _rule_based_kap_category(
                group.representative.kap_subject
            ) or self._parse_kap_category(parsed.get("kap_category"))

            # 2) Ticker: KAP API'sinin OTORİTER stockCodes alanı, LLM'in
            #    tahminini (yukarıda zaten atanmış company_ticker) EZER -
            #    bkz. _kap_company_ticker_from_stock_codes. Boşsa (nadir/
            #    savunmacı durum) LLM'in tahmini korunur.
            kap_ticker = self._kap_company_ticker_from_stock_codes(
                group.representative.kap_stock_codes
            )
            if kap_ticker:
                group.company_ticker = kap_ticker

            # 3) short_summary (bkz. _KAP_SHORT_SUMMARY_INSTRUCTION, src/models.py
            #    > NewsGroup.short_summary) - format doğrulaması YAPILMAZ (LLM
            #    çıktısı güveniliyor, aynı summary/key_points gibi) - boşsa
            #    (ticker çözülemediyse) None kalır, kart eski davranışa
            #    (uzun başlık) düşer.
            group.short_summary = str(parsed.get("short_summary", "")).strip() or None

            # 4) trading_view_symbol: KAP kayıtlarında da OTORİTER stockCodes
            #    alanından deterministik türetilir, LLM'in tahminini EZER -
            #    KAP her zaman Borsa İstanbul'da işlem gören bir şirketle
            #    ilgilidir ve TradingView BIST sembollerini HER ZAMAN
            #    "BIST:TICKER" formatında kullanır - bu yüzden LLM'e güvenip
            #    yanlış borsa/format riski almaya gerek yok (bkz.
            #    _kap_primary_ticker_code, şirket ticker'ı için zaten aynı
            #    otoriter kaynak kullanılıyor).
            #    2026-08-19 (kullanıcı geri bildirimi): KAP'ın stockCodes'taki
            #    İLK kod TradingView'de her zaman GERÇEKTEN var OLMUYORDU
            #    (ör. "YKB" yok ama AYNI kaydın listesindeki "YKBNK" var,
            #    GERÇEK testle bulundu - ~%32 sembol TradingView'de yoktu).
            #    find_valid_bist_symbol artık TÜM kodları SIRAYLA dener, İLK
            #    GEÇERLİ olanı kullanır (bkz. src/tradingview.py) - bu hem
            #    hatalı kod seçimini düzeltir hem gerçekten var olmayan
            #    sembollerde valid=False/None bırakıp butonun dashboard'da
            #    HİÇ gösterilmemesini sağlar (bkz. src/web/app.py).
            symbol, valid = find_valid_bist_symbol(group.representative.kap_stock_codes)
            if symbol:
                group.trading_view_symbol = symbol
                group.trading_view_symbol_valid = valid
        else:
            group.short_summary = None
            # Genel (KAP-dışı) haberlerde trading_view_symbol LLM'in
            # tahminidir (bkz. yukarıda _parse_trading_view_symbol) - KAP'ın
            # AKSİNE denenecek "alternatif kod" listesi YOK (LLM tek bir
            # tahmin verir), bu yüzden SADECE o tek sembol doğrulanır
            # (2026-08-19, kullanıcı geri bildirimi - bkz. src/tradingview.py).
            if group.trading_view_symbol:
                group.trading_view_symbol_valid = validate_tradingview_symbol(group.trading_view_symbol)

    def _current_quota_day(self) -> str:
        return datetime.now(_QUOTA_RESET_TZ).strftime("%Y-%m-%d")

    def _quota_exhausted_today(self) -> bool:
        """data/state/gemini_daily_quota_state.json'da, o günün Gemini GÜNLÜK
        kotasının daha önce (bu süreç ya da önceki bir süreç tarafından)
        tükendiği işaretlenmiş mi kontrol eder. Dosya yoksa/bozuksa/başka bir
        güne aitse False döner (tükenmemiş sayılır)."""
        if not self.daily_quota_guard_enabled or self.provider != "gemini":
            return False
        try:
            state = json.loads(self._quota_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return state.get("exhausted_quota_day") == self._current_quota_day()

    def _mark_quota_exhausted_today(self) -> None:
        """GÜNLÜK kota tükendi sinyali (429 + quotaId=...PerDay...) alınınca
        çağrılır: bugünün tarihini (Pasifik saatine göre) diske kalıcı olarak
        yazar - süreç yeniden başlasa bile aynı gün içinde tekrar boşuna
        Gemini'ye istek atılmaz."""
        if not self.daily_quota_guard_enabled:
            return
        try:
            self._quota_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._quota_state_path.write_text(
                json.dumps({"exhausted_quota_day": self._current_quota_day(), "model": self.model}),
                encoding="utf-8",
            )
        except OSError as exc:  # noqa: BLE001
            logger.warning("Günlük kota durumu diske yazılamadı: %s", exc)

    def _throttle(self) -> None:
        """Sağlayıcının dakika başına istek (RPM) limitini aşmamak için, bir
        önceki çağrının üzerinden `_min_interval_seconds` geçmediyse bekler.
        `rate_limit_enabled: false` ise hiçbir şey yapmaz."""
        if not self.rate_limit_enabled or self._min_interval_seconds <= 0:
            return
        if self._last_call_started_at is not None:
            elapsed = time.monotonic() - self._last_call_started_at
            wait = self._min_interval_seconds - elapsed
            if wait > 0:
                logger.info(
                    "Rate limit koruması: sonraki %s çağrısından önce %.1f sn bekleniyor "
                    "(requests_per_minute=%s).",
                    self.provider,
                    wait,
                    self.requests_per_minute,
                )
                time.sleep(wait)
        self._last_call_started_at = time.monotonic()

    def _classify_rate_limit_error(self, exc: Exception) -> tuple[bool, float | None]:
        """Verilen exception'ın bir 429 (rate limit) hatası olup olmadığını
        ve varsa sağlayıcının önerdiği bekleme süresini (saniye) döner."""
        if self.provider == "gemini":
            if isinstance(exc, genai_errors.ClientError) and getattr(exc, "code", None) == 429:
                return True, _extract_retry_delay_seconds(exc)
            return False, None

        # anthropic
        if isinstance(exc, anthropic.RateLimitError):
            retry_after: float | None = None
            try:
                retry_after = float(exc.response.headers.get("retry-after", ""))
            except (AttributeError, ValueError, TypeError):
                retry_after = None
            return True, retry_after
        return False, None

    def _call_model_with_retry(
        self, user_prompt: str, system_prompt: str = SYSTEM_PROMPT, max_output_tokens: int | None = None
    ) -> str:
        """`_call_model`'i rate-limit koruması (istekler arası bekleme) ve
        429 (RESOURCE_EXHAUSTED) hatasında otomatik tekrar deneme ile sarar.

        429 dışındaki hatalar hiç yakalanmadan olduğu gibi yukarı fırlatılır
        (`summarize_group` bunları yakalayıp fallback'e düşer). 429'da,
        sağlayıcının döndürdüğü `retryDelay` (Gemini) / `retry-after`
        (Anthropic) kadar beklenip en fazla `max_retries` kez tekrar denenir;
        hepsi tükenirse son hata olduğu gibi fırlatılır.

        `system_prompt`: varsayılan olarak haber özetleme/skorlama promptu
        (SYSTEM_PROMPT); günlük özet seçimi gibi farklı görevler için
        (bkz. `select_daily_highlights`) DAILY_DIGEST_SYSTEM_PROMPT geçilir -
        rate-limit/retry altyapısı aynı kalır, sadece görev talimatı değişir.

        `max_output_tokens`: verilmezse `self.max_output_tokens` (config.yaml >
        summarizer.max_output_tokens, TÜM görevlerin varsayılanı) kullanılır.
        Sesli özet gibi normalden UZUN bir yanıt beklenen görevler (bkz.
        `generate_night_digest_script`) daha yüksek bir değer geçebilir -
        aksi halde model iç "düşünme" (thinking_budget) + uzun JSON yanıtı
        birlikte varsayılan bütçeyi aşıp yanıtı MAX_TOKENS'ta yarıda kesebilir
        (gerçek bir denemede doğrulandı, bkz. NIGHT_DIGEST_SYSTEM_PROMPT notu)."""
        attempt = 0
        while True:
            self._throttle()
            try:
                return self._call_model(user_prompt, system_prompt, max_output_tokens)
            except Exception as exc:  # noqa: BLE001
                is_rate_limit, retry_delay = self._classify_rate_limit_error(exc)
                attempt += 1
                if not is_rate_limit:
                    raise
                if _is_daily_quota_error(exc):
                    # Günlük kota tükenmiş: retryDelay kadar beklemek işe
                    # yaramaz (kota ancak yarın sıfırlanır). Tekrar denemeden
                    # direkt vazgeçip fallback'e düşülüyor. Ayrıca bu durumu
                    # diske kalıcı olarak işaretle (bkz. _mark_quota_exhausted_today)
                    # ki AYNI süreç/gün içindeki SONRAKİ tüm gruplar (ve süreç
                    # yeniden başlasa bile) boşuna 429 denemesi yapmasın.
                    logger.error(
                        "%s: '%s' modelinin ücretsiz GÜNLÜK istek kotası tükendi. "
                        "Bu dakika-başına bir limit değil; beklemek/tekrar denemek "
                        "yardımcı olmaz. config.yaml > summarizer.gemini_model "
                        "alanından daha yüksek günlük kotalı bir modele geçmeyi "
                        "veya ertesi gün tekrar denemeyi düşünün.",
                        self.provider,
                        self.model,
                    )
                    self._mark_quota_exhausted_today()
                    raise
                if attempt > self.max_retries:
                    raise
                wait = retry_delay if retry_delay is not None else (self._min_interval_seconds or 15.0)
                logger.warning(
                    "%s: 429 (rate limit) hatası alındı, %.1f sn beklenip tekrar denenecek "
                    "(deneme %d/%d).",
                    self.provider,
                    wait,
                    attempt,
                    self.max_retries,
                )
                time.sleep(wait)
                # Yeniden deneme öncesi zaten bekledik; _throttle'ın normal
                # aralığı bir kez daha uygulamaması için sayacı şimdi güncelle.
                self._last_call_started_at = time.monotonic()

    def _call_model(
        self, user_prompt: str, system_prompt: str = SYSTEM_PROMPT, max_output_tokens: int | None = None
    ) -> str:
        """Seçili sağlayıcıya tek bir çağrı yapar ve modelin ham metin
        yanıtını döner. Herhangi bir hata olduğunda exception'ı olduğu gibi
        yukarı fırlatır - `_call_model_with_retry` / `summarize_group` bunu
        yakalayıp sırasıyla tekrar dener ya da fallback'e düşer."""
        if self.provider == "anthropic":
            return self._call_anthropic(user_prompt, system_prompt, max_output_tokens)
        return self._call_gemini(user_prompt, system_prompt, max_output_tokens)

    def _call_anthropic(
        self, user_prompt: str, system_prompt: str = SYSTEM_PROMPT, max_output_tokens: int | None = None
    ) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_output_tokens or self.max_output_tokens,
            thinking={"type": "disabled"},
            output_config={"effort": self.effort},
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text_blocks = [b.text for b in response.content if getattr(b, "type", None) == "text"]
        return "\n".join(text_blocks).strip()

    def _call_gemini(
        self, user_prompt: str, system_prompt: str = SYSTEM_PROMPT, max_output_tokens: int | None = None
    ) -> str:
        # Not: `thinking_config=ThinkingConfig(thinking_budget=0)` (tam kapatma)
        # gemini-3.6-flash'ta 400 INVALID_ARGUMENT ile reddediliyor - bu model
        # düşünmeyi tamamen sıfıra çekmeyi kabul etmiyor (test edilerek
        # doğrulandı). thinking_config'i hiç göndermemek de (model varsayılan/
        # dinamik davranışına düşer) güvenilir değil: gerçek bir test
        # çalıştırmasında model iç "düşünme"ye ~700 token harcayıp
        # max_output_tokens'ı tükettiğinden, görünür JSON yanıtı yarıda
        # kesiliyordu (finish_reason=MAX_TOKENS). Sınırlı, küçük bir pozitif
        # bütçe (200-500 arası test edildi, ikisi de güvenilir tamamlanıyor)
        # hem 400 hatasını önlüyor hem de görünür yanıt için yeterli token
        # bırakıyor.
        response = self.client.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=max_output_tokens or self.max_output_tokens,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=400),
                # Model zaten SYSTEM_PROMPT'ta salt JSON döndürmesi için
                # talimatlandırılıyor; bunu ayrıca zorunlu kılmak ayrıştırmayı
                # (bkz. _extract_json) daha güvenilir hale getirir.
                response_mime_type="application/json",
            ),
        )
        return (response.text or "").strip()

    @staticmethod
    def _parse_importance_score(raw_value: Any) -> int | None:
        try:
            score = int(raw_value)
        except (TypeError, ValueError):
            return None
        # Modelin ürettiği değer aralık dışıysa (nadiren olabilir) sınırlara çek.
        return max(1, min(5, score))

    @staticmethod
    def _parse_regions(raw_value: Any) -> list[str]:
        """Modelin döndürdüğü `regions` listesini VALID_REGIONS ile sınırlar,
        bilinmeyen/hatalı değerleri sessizce eler, tekrarları kaldırır. Hiç
        geçerli değer kalmazsa ["diger"] döner (liste asla tamamen boş
        kalmasın diye - bkz. SYSTEM_PROMPT)."""
        if not isinstance(raw_value, list):
            return []
        regions: list[str] = []
        for item in raw_value:
            region = str(item).strip().lower()
            if region in VALID_REGIONS and region not in regions:
                regions.append(region)
        return regions or ["diger"]

    @staticmethod
    def _parse_sectors(raw_value: Any) -> list[str]:
        """Modelin döndürdüğü `sector` listesini VALID_SECTORS ile sınırlar,
        bilinmeyen/hatalı değerleri sessizce eler, tekrarları kaldırır. Hiç
        geçerli değer kalmazsa ["diger"] döner (bkz. _parse_regions - aynı
        yaklaşım)."""
        if not isinstance(raw_value, list):
            return []
        sectors: list[str] = []
        for item in raw_value:
            sector = str(item).strip().lower()
            if sector in VALID_SECTORS and sector not in sectors:
                sectors.append(sector)
        return sectors or ["diger"]

    @staticmethod
    def _parse_sentiment(raw_value: Any) -> str | None:
        """Modelin döndürdüğü `sentiment` değerini VALID_SENTIMENTS ile
        sınırlar; bilinmeyen/hatalı bir değer gelirse None döner (haber
        duygu etiketsiz kalır, hiçbir yerde emoji gösterilmez)."""
        value = str(raw_value).strip().lower() if raw_value is not None else ""
        return value if value in VALID_SENTIMENTS else None

    @staticmethod
    def _parse_top_category(raw_value: Any) -> str | None:
        """Modelin döndürdüğü `top_category` değerini VALID_TOP_CATEGORIES ile
        sınırlar; bilinmeyen/hatalı bir değer gelirse None döner ("Detaylı
        İnceleme" sayfasındaki sekmelerin hiçbirinde görünmez, bkz.
        src/web/app.py)."""
        value = str(raw_value).strip().lower() if raw_value is not None else ""
        return value if value in VALID_TOP_CATEGORIES else None

    @staticmethod
    def _parse_company_ticker(raw_value: Any) -> str | None:
        """Modelin döndürdüğü `company_ticker` değerini ("BORSA: SEMBOL" \
        formatında bir string bekleniyor) sadeleştirir. Boş/eksik/aşırı \
        uzun (muhtemelen hatalı) bir değer gelirse None döner - kart üzerinde \
        hiçbir borsa/ticker etiketi gösterilmez (bkz. src/web/app.py)."""
        if raw_value is None:
            return None
        value = str(raw_value).strip()
        if not value or len(value) > 40:
            return None
        return value

    # TradingView'in kendi format kuralı: "BORSA:SEMBOL" - iki nokta üst
    # üste ile ayrılmış, boşluksuz, sadece harf/rakam/nokta/tire (ör.
    # "NASDAQ:NFLX", "FX:USDJPY", "TVC:GOLD", "BIST:THYAO"). company_ticker'ın
    # "BORSA: SEMBOL" (boşluklu) formatından FARKLI - bu, TradingView URL'ine
    # DOĞRUDAN gömüleceğinden (bkz. src/web/app.py > _build_tradingview_url)
    # sıkı bir biçim kontrolü burada yapılır.
    _TRADING_VIEW_SYMBOL_RE = re.compile(r"^[A-Z0-9]+:[A-Z0-9._-]+$")

    @staticmethod
    def _parse_trading_view_symbol(raw_value: Any) -> str | None:
        """Modelin döndürdüğü `trading_view_symbol` değerini doğrular.
        Boş/eksik/format DIŞI (boşluk içeren, iki nokta üst üste eksik/\
        fazla vb.) bir değer gelirse None döner - kart üzerinde hiçbir \
        "Teknik Görünüm" butonu gösterilmez (bkz. src/web/app.py). Bu, \
        sembolün GERÇEKTEN var olduğunu DOĞRULAMAZ (öyle bir kontrol ek bir \
        ağ çağrısı gerektirir) - sadece TradingView'in beklediği biçimde mi \
        diye bakar; içerik doğruluğu prompt talimatına (bkz. SYSTEM_PROMPT >
        "ASLA tahmin/uydurma bir sembol üretme") bırakılmıştır."""
        if raw_value is None:
            return None
        value = str(raw_value).strip().upper()
        if not value or len(value) > 40:
            return None
        if not Summarizer._TRADING_VIEW_SYMBOL_RE.match(value):
            return None
        return value

    @staticmethod
    def _parse_kap_category(raw_value: Any) -> str:
        """Modelin döndürdüğü `kap_category` değerini VALID_KAP_CATEGORIES ile
        sınırlar - bilinmeyen/eksik bir değer gelirse "diger" döner (None
        DEĞİL - top_category'nin aksine, kap_category SADECE KAP gruplarında
        istendiğinden her zaman anlamlı bir değere sahip olmalı)."""
        value = str(raw_value).strip().lower() if raw_value is not None else ""
        return value if value in VALID_KAP_CATEGORIES else "diger"

    @staticmethod
    def _kap_company_ticker_from_stock_codes(stock_codes: str) -> str | None:
        """KAP API'sinin OTORİTER `stockCodes` alanından ("YKB, YKBNK" gibi
        virgülle ayrılmış, bazen birden fazla kod) LLM'in tahminini EZEN bir
        company_ticker string'i üretir - "BIST: YKB, BIST: YKBNK" formatında.
        Boşsa None döner (LLM'in kendi tahmini korunur - bkz. summarize_group,
        3 aylık canlı testte bu alan KAP'ın ODA/FR/CA kapsamında HER ZAMAN
        dolu geliyordu ama savunmacı olarak yine de None fallback'i var)."""
        codes = [c.strip() for c in (stock_codes or "").split(",") if c.strip()]
        if not codes:
            return None
        return ", ".join(f"BIST: {c}" for c in codes)

    def select_daily_highlights(self, records: list[Any]) -> list[dict[str, Any]]:
        """Verilen (son 24 saatteki) `NewsRecord` listesi arasından GERÇEKTEN
        önemli 5-10 tanesini seçtirmek için Gemini/Claude'a TEK bir ek çağrı
        yapar (bkz. DAILY_DIGEST_SYSTEM_PROMPT, src/daily_digest.py).

        Döner: [{"index": <records listesindeki sıra>, "reason": "..."}, ...]
        Herhangi bir hata/ayrıştırma sorununda boş liste döner (exception
        fırlatmaz) - çağıran taraf (src/daily_digest.py) bu durumda önem
        skoruna göre basit bir geri dönüşe (fallback) düşer.
        """
        if not records:
            return []

        lines = []
        for i, r in enumerate(records):
            score = r.importance_score if r.importance_score is not None else "?"
            summary = (r.summary or "").strip()[:300]
            lines.append(f"{i}. [Önem: {score}/5] {r.title}\nÖzet: {summary or '(özet yok)'}")
        user_prompt = "\n\n".join(lines)

        try:
            raw_text = self._call_model_with_retry(user_prompt, system_prompt=DAILY_DIGEST_SYSTEM_PROMPT)
        except Exception:  # noqa: BLE001 - günlük özet seçimi başarısız olursa fallback'e düşülsün
            logger.exception("Günlük özet için haber seçimi (LLM çağrısı) başarısız oldu.")
            return []

        parsed = _extract_json(raw_text)
        if not parsed or not isinstance(parsed.get("selections"), list):
            logger.warning("Günlük özet seçim yanıtı beklenen JSON formatında değildi.")
            return []

        selections: list[dict[str, Any]] = []
        seen_indices: set[int] = set()
        for item in parsed["selections"]:
            try:
                index = int(item.get("index"))
            except (TypeError, ValueError, AttributeError):
                continue
            if index in seen_indices or not (0 <= index < len(records)):
                continue
            seen_indices.add(index)
            reason = str(item.get("reason", "")).strip()
            selections.append({"index": index, "reason": reason})

        return selections[:10]

    def summarize_company_profile(self, company_name: str, records: list[Any]) -> str:
        """Verilen şirket adı ve (son 30 günlük) `NewsRecord` listesi için,
        Gemini/Claude'a TEK bir ek çağrı ile genel görünüm (outlook) paragrafı
        ürettirir (bkz. COMPANY_PROFILE_SYSTEM_PROMPT, src/company_profile.py).

        Herhangi bir hata/ayrıştırma sorununda boş string döner (exception
        fırlatmaz) - çağıran taraf bu durumda otomatik özeti göstermeden
        sadece haber listesini gösterir."""
        if not records:
            return ""

        lines = [f"Şirket/Varlık: {company_name}\n"]
        for r in records:
            summary = (r.summary or "").strip()[:300]
            lines.append(f"- {r.title}\n  Özet: {summary or '(özet yok)'}")
        user_prompt = "\n".join(lines)

        try:
            raw_text = self._call_model_with_retry(user_prompt, system_prompt=COMPANY_PROFILE_SYSTEM_PROMPT)
        except Exception:  # noqa: BLE001 - profil özeti başarısız olursa çağıran taraf sessizce atlasın
            logger.exception("Şirket profili özeti (LLM çağrısı) başarısız oldu: %s", company_name)
            return ""

        parsed = _extract_json(raw_text)
        if not parsed or not isinstance(parsed.get("summary"), str):
            logger.warning("Şirket profili özeti yanıtı beklenen JSON formatında değildi: %s", company_name)
            return ""

        return parsed["summary"].strip()

    def generate_night_digest_script(
        self, records: list[Any], category: str = "genel", calendar_lines: list[str] | None = None
    ) -> str:
        """Verilen (gece penceresindeki, önem eşiğini geçen) `NewsRecord`
        listesinden Sesli Günlük Özet için TEK bir akıcı Türkçe konuşma
        metni ürettirir (bkz. NIGHT_DIGEST_SYSTEM_PROMPT, src/voice_digest.py).

        `category`: "genel" (KAP dışı haberler) veya "kap" (SADECE KAP özel
        durum açıklamaları, bkz. src/voice_digest.py > İKİ AYRI özet -
        genel/KAP). İkisi de user prompt'a bir YÖNERGE bloğu ekler:
        - "kap": sadece bağlam ipucu (KAP'a özgü açılış kurabilsin diye).
        - "genel": DAHA DETAYLI/UZUN bir konuşma iste (kullanıcı isteği,
          2026-08-31 - varsayılan 150-280 kelime/başlık-düzeyi yerine
          250-450 kelime/"ne oldu, neden önemli" bağlamı).

        `calendar_lines`: SADECE "genel" kategoride kullanılır - bugün için
        planlanmış ekonomik takvim olaylarının (bkz.
        src/voice_digest.py > _today_calendar_lines) önceden formatlanmış
        satırları. Verilirse, üretilen anlatının SONUNA kısa bir ek paragraf
        olarak eklenir (bkz. NIGHT_DIGEST_SYSTEM_PROMPT). Boş/None ise hiç
        eklenmez - ayrı bir ses dosyası/mesaj DEĞİL, TEK metnin bir parçası.

        Herhangi bir hata/ayrıştırma sorununda boş string döner (exception
        fırlatmaz) - çağıran taraf bu durumda sesli özeti atlar."""
        if not records:
            return ""

        prompt_blocks: list[str] = []
        if category == "kap":
            prompt_blocks.append(
                "YÖNERGE: Bu liste SADECE KAP (Kamuyu Aydınlatma Platformu) özel "
                "durum açıklamalarından oluşuyor."
            )
        else:
            prompt_blocks.append(
                "YÖNERGE: Bu segment DAHA DETAYLI olsun - varsayılan 150-280 "
                "kelime/başlık-düzeyi sınırını YOK SAY, bunun yerine yaklaşık "
                "250-450 kelimelik bir konuşma yaz. Her önemli haber için "
                "sadece başlığı tekrar etme; 'ne oldu, neden önemli/piyasayı "
                "nasıl etkileyebilir' şeklinde en az bir-iki cümlelik somut "
                "bağlam ver."
            )

        if calendar_lines:
            prompt_blocks.append(
                "EKONOMİK TAKVİM (bugün için planlanmış, anlatının SONUNA "
                "kısaca eklenecek):\n" + "\n".join(calendar_lines)
            )

        lines = []
        for r in records:
            score = r.importance_score if r.importance_score is not None else "?"
            summary = (r.summary or "").strip()[:300]
            lines.append(f"[Önem: {score}/5] {r.title}\nÖzet: {summary or '(özet yok)'}")
        prompt_blocks.append("\n\n".join(lines))

        user_prompt = "\n\n".join(prompt_blocks)

        # "genel" kategori artık DAHA UZUN/detaylı bir anlatı istiyor (+ olası
        # Ekonomik Takvim eki) - varsayılan max_output_tokens (config.yaml >
        # summarizer.max_output_tokens, TÜM diğer görevler için ayarlı) model
        # iç "düşünme" bütçesiyle (thinking_budget=400, bkz. _call_gemini)
        # birlikte bunu YARIDA KESEBİLİR (gerçek bir denemede doğrulandı -
        # finish_reason=MAX_TOKENS, bozuk/eksik JSON). Bu yüzden burada özel
        # olarak daha yüksek bir bütçe istenir - "kap" için de zararsız,
        # sadece bir TAVAN, hedef uzunluğu ETKİLEMEZ.
        try:
            raw_text = self._call_model_with_retry(
                user_prompt, system_prompt=NIGHT_DIGEST_SYSTEM_PROMPT, max_output_tokens=3000
            )
        except Exception:  # noqa: BLE001 - sesli özet metni başarısız olursa çağıran taraf atlasın
            logger.exception("Sesli özet metni (LLM çağrısı) başarısız oldu.")
            return ""

        parsed = _extract_json(raw_text)
        if not parsed or not isinstance(parsed.get("script"), str):
            logger.warning("Sesli özet metni yanıtı beklenen JSON formatında değildi.")
            return ""

        return parsed["script"].strip()

    def check_same_event(self, record_a: Any, record_b: Any) -> tuple[bool, str]:
        """"Haber-KAP Bağlantı Haritası" özelliği için (bkz.
        src/news_links.py): verilen iki `NewsRecord`'un (biri KAP, biri
        genel haber - aynı company_ticker + yakın zaman penceresiyle
        bulunmuş bir aday çift) GERÇEKTEN aynı olayı anlatıp anlatmadığını
        TEK bir küçük LLM çağrısıyla sorar (bkz. NEWS_LINK_SYSTEM_PROMPT).

        `summarize_company_profile` İLE AYNI desen: hata/ayrıştırma
        sorununda (False, "") döner (exception fırlatmaz) - temkinli
        varsayılan (bağlantı YOK) - çağıran taraf bu durumda bir bağlantı
        KURMAZ."""
        summary_a = (record_a.summary or "").strip()[:400]
        summary_b = (record_b.summary or "").strip()[:400]
        user_prompt = (
            f"Kayıt 1 ({record_a.sources}):\nBaşlık: {record_a.title}\nÖzet: {summary_a or '(özet yok)'}\n\n"
            f"Kayıt 2 ({record_b.sources}):\nBaşlık: {record_b.title}\nÖzet: {summary_b or '(özet yok)'}"
        )

        try:
            raw_text = self._call_model_with_retry(user_prompt, system_prompt=NEWS_LINK_SYSTEM_PROMPT)
        except Exception:  # noqa: BLE001 - bir çiftin doğrulaması başarısız olursa hook'un geri kalanını durdurmasın
            logger.exception(
                "Bağlantı doğrulama (LLM çağrısı) başarısız oldu: '%s' <-> '%s'", record_a.title, record_b.title
            )
            return False, ""

        parsed = _extract_json(raw_text)
        if not parsed or "same_event" not in parsed:
            logger.warning(
                "Bağlantı doğrulama yanıtı beklenen JSON formatında değildi: '%s' <-> '%s'",
                record_a.title,
                record_b.title,
            )
            return False, ""

        same_event = bool(parsed.get("same_event"))
        reason = str(parsed.get("reason") or "").strip()[:300]
        return same_event, reason

    def analyze_commodity_weekly(
        self, label: str, pct_change: float, abs_change: float, unit: str
    ) -> tuple[str, list[dict[str, str]]]:
        """Haftalık emtia raporu (bkz. src/commodity_report.py) için TEK bir
        ek çağrı ile "bu fiyat hareketi hangi ABD borsası şirket/sektörlerini
        nasıl etkiler" analizi VE bahsedilen şirketlerin yapılandırılmış
        ticker listesini ürettirir (bkz. COMMODITY_WEEKLY_SYSTEM_PROMPT).
        Dönen ticker listesi, dashboard'daki tıklanabilir şirket
        kartları/Telegram'daki anlık fiyat eki için kullanılır (bkz.
        src/web/market_data.py > get_quotes_for_company_tickers - "BORSA:
        SEMBOL" formatına burada, çağıran tarafta çevrilir).

        `summarize_company_profile` ile AYNI desen: hata/ayrıştırma
        sorununda ("", []) döner (exception fırlatmaz) - çağıran taraf bu
        durumda o emtia için LLM analizi/şirket listesi olmadan devam eder."""
        direction = "yükseldi" if pct_change >= 0 else "düştü"
        user_prompt = (
            f"Emtia: {label}\n"
            f"Geçen haftaki değişim: %{pct_change:+.2f} ({direction})\n"
            f"Mutlak değişim: {abs_change:+.4f} {unit}"
        )
        try:
            raw_text = self._call_model_with_retry(user_prompt, system_prompt=COMMODITY_WEEKLY_SYSTEM_PROMPT)
        except Exception:  # noqa: BLE001 - bir emtianın analizi başarısız olursa raporun geri kalanını durdurmasın
            logger.exception("Emtia haftalık analizi (LLM çağrısı) başarısız oldu: %s", label)
            return "", []

        parsed = _extract_json(raw_text)
        if not parsed or not isinstance(parsed.get("analysis"), str):
            logger.warning("Emtia haftalık analizi yanıtı beklenen JSON formatında değildi: %s", label)
            return "", []

        analysis = parsed["analysis"].strip()
        companies = self._parse_commodity_companies(parsed.get("companies"))
        return analysis, companies

    @staticmethod
    def _parse_commodity_companies(raw_value: Any) -> list[dict[str, str]]:
        """Modelin döndürdüğü `companies` listesini ({"name","ticker",
        "exchange"} sözlükleri) doğrular/temizler. Geçersiz veya eksik alanlı
        girişler sessizce atlanır (raporun geri kalanını etkilemez) - `name`
        boş/aşırı uzunsa, `ticker` basit bir borsa sembolü şekline
        uymuyorsa (harf/rakam/nokta/tire, en fazla 10 karakter) o giriş
        elenir. `exchange` normalize edilip bilinmeyen bir değerse (model
        promptta SADECE NYSE/NASDAQ istendi ama garanti değil) "NASDAQ"
        varsayılır - bkz. src/web/market_data.py > _EXCHANGE_YAHOO_SUFFIX
        (NASDAQ/NYSE için zaten aynı, sonek eklenmiyor)."""
        if not isinstance(raw_value, list):
            return []

        result: list[dict[str, str]] = []
        for item in raw_value[:4]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            ticker = str(item.get("ticker", "")).strip().upper()
            if not name or len(name) > 80:
                continue
            if not re.match(r"^[A-Z0-9.\-]{1,10}$", ticker):
                continue
            exchange = str(item.get("exchange", "")).strip().upper()
            if exchange not in ("NASDAQ", "NYSE", "AMEX", "NYSEAMERICAN"):
                exchange = "NASDAQ"
            result.append({"name": name, "ticker": ticker, "exchange": exchange})
        return result

    def _apply_fallback(self, group: NewsGroup, quota_deferred: bool = False) -> None:
        rep = group.representative
        fallback = rep.raw_text.strip()[:280] or rep.title
        prefix = "(Günlük Gemini kotası doldu, yarın otomatik özetlenecek)" if quota_deferred else "(Otomatik özet üretilemedi)"
        group.summary = f"{prefix} {fallback}"
        group.key_points = []
        group.importance_score = None
        group.importance_reason = ""
        group.regions = []
        group.sectors = []
        group.sentiment = None
        group.market_impact = None
        group.top_category = None
        group.company_ticker = None
        group.title_tr = None
        group.kap_category = None
        group.trading_view_symbol = None
        group.trading_view_symbol_valid = None
