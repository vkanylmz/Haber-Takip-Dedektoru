# Finansal Haber Toplayıcı ve Özetleyici

Belirlenen finansal haber kaynaklarından güncel haberleri çeker, Google Gemini
(varsayılan sağlayıcı) veya Anthropic (Claude) API ile kendi cümleleriyle kısa
özetler ve 1-5 arası bir **önem skoru** üretir, SQLite'a kaydeder, önem skoru
bir eşiği geçen haberler için **Telegram bildirimi** gönderir ve hepsini bir
**web dashboard**'da (+ terminal + Markdown rapor) sunar.

**LLM sağlayıcısı:** Varsayılan olarak **Gemini** (`gemini-3.5-flash-lite`)
kullanılır — `GEMINI_API_KEY` gerekir, Google AI Studio'dan ücretsiz alınır:
https://aistudio.google.com/apikey. Anthropic'e (Claude) geri dönmek isterseniz
`config.yaml` içinde tek satırı değiştirmeniz yeterlidir:

```yaml
summarizer:
  llm_provider: anthropic   # "gemini" -> "anthropic"
```

Bu durumda `ANTHROPIC_API_KEY` gerekir (bkz. aşağıdaki "Eksik API Anahtarları" bölümü).
Her iki sağlayıcı da **aynı prompt'u ve aynı JSON çıktı şemasını** kullanır; kod
tarafında başka hiçbir değişiklik gerekmez.

> **Not (2026-07):** `gemini-2.5-flash` ve `gemini-2.5-flash-lite` Google
> tarafından yeni kullanıcılara kapatıldı ("models/... is no longer available
> to new users" hatası). İlk denemede `gemini-3.6-flash`'a geçildi, ancak
> gerçek bir çalıştırmada bu modelin ücretsiz katımda **günde sadece 20
> istek** ile sınırlı olduğu ortaya çıktı (flagship modeller genelde çok
> düşük ücretsiz günlük kotaya sahip oluyor) — ~90 haberlik bir tarama için
> yetersiz. Varsayılan, "en hızlı, en uygun maliyetli" olarak tanıtılan ve
> çok daha yüksek bir günlük kotaya sahip olan `gemini-3.5-flash-lite` olarak
> güncellendi; gerçek bir API anahtarıyla, art arda birden fazla haberde
> hiç 429 almadan test edildi.

**Rate limit koruması:** Gemini'nin ücretsiz katmanının hem dakika-başına
(RPM) hem de **günlük** (RPD) istek limitleri vardır — hangisinin bağlayıcı
olduğu modele göre değişir (bkz. yukarıdaki not). Onlarca haberi art arda
hızlı göndermek `429 RESOURCE_EXHAUSTED` hatasına yol açar. Bunu önlemek için
`summarizer.py`, istekler arasına otomatik bir bekleme koyar; 429 alınırsa
sağlayıcının önerdiği `retryDelay` süresi kadar bekleyip otomatik tekrar
dener; hata bir GÜNLÜK kota tükenmesiyse (retryDelay beklemek yardımcı
olmayacağından) tekrar denemeden hemen vazgeçip ham metin fallback'ine düşer
— bkz. aşağıdaki "Rate Limit Koruması" bölümü.

## Proje Yapısı

```
finans-haber-toplayici/
├── config.yaml           # Kaynak listesi ve tüm ayarlar
├── .env.example          # API key/token şablonları (kopyalayıp .env yapın)
├── requirements.txt
├── main.py                # TEK KOMUTLA başlatma: worker + web dashboard birlikte
├── worker.py              # Periyodik tarama (APScheduler) — main.py bunu kullanır,
│                          #   tek başına da çalıştırılabilir
├── src/
│   ├── config.py          # config.yaml + .env yükleme
│   ├── config_setup.py    # eksik API anahtarı/token'ları sorma+doğrulama+.env'e kaydetme
│   ├── models.py          # NewsItem / NewsGroup veri modelleri (+ önem skoru alanları)
│   ├── logging_setup.py   # konsol + dosya loglama
│   ├── deduplicator.py    # farklı kaynaklardaki aynı haberi gruplama
│   ├── summarizer.py      # Gemini/Claude ile özetleme + önem skorlama (tek API çağrısında)
│   ├── db.py               # SQLite/SQLAlchemy: kalıcılık + "bildirildi mi" durumu + abone (subscribers) tablosu
│   ├── telegram_bot.py     # Botun gelen mesajlarını dinler: /start -> abone ol, /stop -> ayrıl
│   ├── notifier.py         # Telegram bildirimi (sadece eşiği geçen haberler, TÜM abonelere)
│   ├── main.py             # tek bir çekim+özetleme+skorlama+kayıt+bildirim turu
│   ├── fetchers/
│   │   ├── base.py                  # robots.txt kontrolü + rate-limit (ortak)
│   │   ├── rss_fetcher.py           # RSS/Atom kaynakları
│   │   ├── scrape_fetcher.py        # RSS'i olmayan kaynaklar için scraping iskeleti
│   │   └── licensed_aggregator.py   # NewsAPI.ai üzerinden lisanslı Reuters/Bloomberg erişimi
│   ├── output/
│   │   ├── cli_output.py      # terminal çıktısı
│   │   └── markdown_output.py # data/reports/*.md raporu
│   └── web/
│       ├── app.py              # FastAPI dashboard
│       └── templates/
│           └── dashboard.html  # Jinja2 şablonu
└── data/
    ├── logs/              # dönen (rotating) log dosyaları
    ├── reports/           # zaman damgalı Markdown raporlar
    ├── state/             # lisanslı kaynağın kota-korumalı önbelleği (bkz. ilgili bölüm)
    └── finans_haber.db    # SQLite veritabanı (haberler, özetler, skorlar, bildirim durumu)
```

## Kurulum

```powershell
cd finans-haber-toplayici
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Bu kadar — `.env` dosyasını elle oluşturmanıza gerek yok. `python main.py` ilk
çalıştığında eksik olan anahtarları sizden isteyecektir (aşağıdaki bölüme
bakın). İsterseniz yine de `copy .env.example .env` ile şablonu kopyalayıp
anahtarları elle de girebilirsiniz.

## Eksik API Anahtarları/Token'lar (İlk Çalıştırmada Otomatik Sorulur)

`python main.py` her başlatıldığında (`src/config_setup.py`), projenin
ihtiyaç duyduğu kimlik bilgilerinin `.env`'de olup olmadığını kontrol eder:

| Ortam değişkeni | Hangi özellik için | Nereden alınır |
|---|---|---|
| `GEMINI_API_KEY` | Haber özetleme + önem skorlama (`summarizer.py`), **varsayılan sağlayıcı** (`config.yaml → summarizer.llm_provider: gemini`) | https://aistudio.google.com/apikey |
| `ANTHROPIC_API_KEY` | Haber özetleme + önem skorlama, **yalnızca** `summarizer.llm_provider: anthropic` ise sorulur | https://console.anthropic.com |
| `EVENTREGISTRY_API_KEY` | Reuters/Bloomberg lisanslı erişimi (`licensed_aggregator.py`) | https://www.newsapi.ai/register |
| `TELEGRAM_BOT_TOKEN` | Telegram bot dinleyicisi + bildirimleri (`telegram_bot.py`, `notifier.py`) | Telegram'da @BotFather |
| `TELEGRAM_CHAT_ID` | Yalnızca kurulumu yapan kişinin **ilk abone** olarak otomatik eklenmesi için (bkz. Telegram bölümü) — yeni abonelikler artık botla `/start` yazarak, `.env`'siz yapılır | `getUpdates` (bkz. Telegram bölümü) — yalnızca `TELEGRAM_BOT_TOKEN` mevcutsa sorulur |

`GEMINI_API_KEY` ile `ANTHROPIC_API_KEY`'den yalnızca biri sorulur — hangisi
olduğu `config.yaml → summarizer.llm_provider` tarafından belirlenir
(varsayılan `gemini`).

**Davranış:**

1. **Zaten `.env`'de varsa** (elle girilmiş veya daha önce bu akışla kaydedilmiş)
   → hiç sorulmaz, sessizce kullanılır.
2. **Yoksa VE etkileşimli bir terminaldeyseniz** (`python main.py`'yi doğrudan
   bir terminalde çalıştırıyorsanız): ne işe yaradığını ve nereden
   alınacağını açıklayan bir mesajla birlikte terminalde sorulur, örnek:

   ```
   ⚠  GEMINI_API_KEY bulunamadı (haber özetleme ve önem skorlama için gerekli).
      Buradan alabilirsiniz: https://aistudio.google.com/apikey
      Şimdi girin (boş bırakırsanız Özetleme ve önem skorlama (Gemini) devre dışı kalır): _
   ```

   Bir değer girerseniz, kaydetmeden önce **gerçek bir API çağrısıyla
   doğrulanır** (Gemini/Anthropic: ücretsiz `models.list()`; Telegram bot token:
   `getMe()`; Telegram chat_id: gerçek bir onay mesajı gönderme denemesi;
   EventRegistry: minimal 1 makalelik bir sorgu). Geçersizse **en fazla 3
   deneme** hakkınız olur; 3'ünde de başarısız olursa veya boş bırakırsanız
   ilgili özellik devre dışı bırakılır (net bir uyarı loglanır) ve **uygulama
   çökmeden** devam eder. Geçerli bir değer girdiğinizde `.env` dosyasına
   otomatik yazılır — bir sonraki çalıştırmada tekrar sorulmaz.
3. **Yoksa VE etkileşimli DEĞİLSENİZ** (ör. çıktısı bir dosyaya
   yönlendirilmiş, bir servis olarak çalıştırılmış): hiçbir şey **sorulmaz**
   (uygulama `input()` üzerinde asla takılı kalmaz) — eksik anahtar sadece
   loglanır, ilgili özellik devre dışı kalır, uygulama normal çalışmaya devam
   eder.

Bu akış yalnızca `python main.py` başlangıcında çalışır; `python -m src.main`
veya `python worker.py` ile doğrudan çalıştırırsanız bu soru akışı devreye
girmez (yalnızca eksik anahtarı loglayıp o özelliği atlar) — istediğiniz
zaman `.env` dosyasını elle düzenleyebilir ya da `python main.py`'yi bir kez
çalıştırıp eksikleri tamamlayabilirsiniz.

## Tek Komutla Başlatma

```powershell
.\.venv\Scripts\python.exe main.py
```

Bu komut **aynı anda**:
1. **Worker**'ı arka planda başlatır: hemen bir ilk tarama yapar, sonra
   `config.yaml → worker.interval_minutes` (varsayılan 30 dk) aralıklarla
   otomatik olarak haberleri çeker, gruplar, özetler, önem skorlar,
   veritabanına kaydeder ve eşiği geçen haberler için Telegram bildirimi gönderir.
2. **Telegram bot dinleyicisini** (`TELEGRAM_BOT_TOKEN` tanımlıysa) ayrı bir
   arka plan thread'inde başlatır: bota `/start` yazan (veya ilk mesajı
   gönderen) herkesi otomatik abone yapar, `/stop` yazanı çıkarır — worker'ı
   ve web sunucusunu bloklamaz (bkz. Telegram Bildirimi bölümü).
3. **Web dashboard**'unu `http://localhost:8000` adresinde başlatır (bkz. aşağıdaki
   bölüm) — worker'ın topladığı haberleri anlık olarak gösterir.

Ctrl+C ile hepsi düzgünce durur.

**Alternatif çalıştırma şekilleri** (ayrı ayrı da kullanılabilir):

| Komut | Ne yapar |
|---|---|
| `python -m src.main` | Tek seferlik bir tur (çek → grupla → özetle/skorla → kaydet → bildir → yazdır), sonra çıkar. Geliştirme/test için kullanışlı. |
| `python worker.py` | Sadece periyodik tarama (web dashboard olmadan). |
| `python -m uvicorn src.web.app:app --host 0.0.0.0 --port 8000` | Sadece web dashboard'u (worker olmadan) — daha önce toplanmış verileri incelemek için. |

Seçili sağlayıcının API anahtarı (varsayılan `GEMINI_API_KEY`, `llm_provider: anthropic`
ise `ANTHROPIC_API_KEY`) tanımlı değilse program **çökmez** — haberleri çeker, gruplar
ve özet yerine ham metni gösterip devam eder; önem skoru `None` kalır (yani bu haberler
asla Telegram eşiğini geçemez). Böylece kurulumun geri kalanını API anahtarı olmadan
da test edebilirsiniz.

## Docker ile Çalıştırma (alternatif)

`python main.py` yerine, projeyi Docker container'ı içinde de çalıştırabilirsiniz
— bu, iş/ev bilgisayarı arasında taşımayı veya bir sunucuya deploy etmeyi
kolaylaştırır. Proje bir `Dockerfile` + `docker-compose.yml` ile paketlenmiştir.

**Ön koşul:** [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(Windows/Mac) veya Docker Engine + Compose (Linux) kurulu olmalı.

1. `.env` dosyanızı proje kök dizininde elle hazırlayın (`.env.example`'ı
   kopyalayıp API anahtarlarınızı/Telegram bot token'ınızı doldurun).
   **Önemli:** container **etkileşimli değildir** — normal `python main.py`
   çalıştırmasının aksine, eksik anahtarları sizden terminalde SORMAZ (bkz.
   `src/config_setup.py > ensure_all_credentials`, `sys.stdin.isatty()`
   kontrolü); `.env` içinde tanımlı olmayan bir anahtar, ilgili özelliği
   (ör. özetleme veya Telegram) sessizce devre dışı bırakır.
2. Container'ı başlatın:

   ```bash
   docker-compose up --build
   ```

   (Arka planda çalıştırmak isterseniz: `docker-compose up --build -d`)
3. Dashboard'a `http://localhost:8000` adresinden erişin.
4. Durdurmak için: `docker-compose down` (Ctrl+C, ön planda çalıştırıyorsanız).

**Kalıcılık:** `docker-compose.yml`, `./data` klasörünü ve `./.env` dosyasını
container'ın DIŞINDA (host'ta) tutacak şekilde volume olarak bağlar — veritabanı,
loglar, raporlar ve yedekler (`data/backups/`) container yeniden oluşturulduğunda
(ör. `docker-compose up --build` ile imaj güncellendiğinde) KAYBOLMAZ. `.env` ve
`data/` bilerek imajın İÇİNE kopyalanmaz (bkz. `.dockerignore`) — aksi halde
gerçek API anahtarlarınız/abone verileriniz imajın içine gömülürdü.

**Not:** Worker, Telegram bot dinleyicisi ve web dashboard'un hepsi TEK bir
container'da, `python main.py` (bkz. `Dockerfile > CMD`) ile birlikte çalışır —
yerel çalıştırmadaki mimari birebir aynıdır, sadece paketleme farklıdır.

## Fly.io'ya Deploy

Projeyi kendi bilgisayarınızda Docker kurulu OLMADAN da bir sunucuya deploy
edebilirsiniz: Fly.io, `Dockerfile`'ınızı KENDİ build sunucusunda inşa eder —
yerelde yalnızca küçük bir CLI aracı (`flyctl`) yeterlidir.

> **ÖNEMLİ - "ücretsiz katman" ve deneme süresi hakkında dürüst bir not
> (2026-07'de resmi Fly.io dokümanlarından araştırıldı, bkz. kaynaklar altta):**
> Fly.io, Ekim 2024'ten itibaren YENİ hesaplar için kalıcı bir ücretsiz plan
> sunmuyor. Yeni bir hesap, **"2 VM-saati VEYA 7 gün, hangisi önce dolarsa"**
> şeklinde çok sınırlı bir deneme kotası alır. Bu kota (makineler + 20GB'a
> kadar disk + 10 makineye kadar) tükendiğinde **uygulamanız DURDURULUR** -
> devam etmek için ödeme yöntemi (kredi kartı) eklemeniz gerekir; kartı
> eklediğiniz ANDAN İTİBAREN kullanım faturalandırılmaya başlar (bkz. altta
> "Deneme Süresi Dolunca Ne Olur?").
>
> **BU PROJE İÇİN KRİTİK BİR DETAY:** `fly.toml` içindeki
> `auto_stop_machines = "off"` + `min_machines_running = 1` ayarları
> (worker'ın/Telegram bot'un sürekli çalışması için ZORUNLU - bkz. dosya
> başındaki not) makinenin 7 gün DEĞİL, kesintisiz çalıştığı anlamına gelir.
> Yani **"2 VM-saati" kotanız, deploy'dan sonra yaklaşık 2 SAAT İÇİNDE
> tükenecektir** - 7 günlük pencere bu senaryoda pratikte geçerli olmaz.
> Kısa süreli bir "canlıya alma" denemesi için bu yeterli olabilir; daha
> uzun süre açık tutmak isterseniz kart eklemeniz (ve gerçek, düşük de olsa
> ücretlendirilmeniz) gerekecektir. Ücretlendirmeden tamamen kaçınmak
> isterseniz aşağıdaki **"Çıkış Planı"** bölümündeki adımlarla süre dolmadan
> her şeyi silebilirsiniz.
>
> İyi haber: bu projenin ölçeği (tek küçük makine, 1GB disk) için, kart
> eklendikten sonraki maliyet de ÇOK DÜŞÜK - aşağıdaki "Maliyet Tahmini"
> bölümüne bakın (~4-5 USD/ay, saatlik kesirlerle orantılı). Eğer Ekim 2024
> öncesinden kalma eski bir Fly.io hesabınız varsa, o hesapta hâlâ geçerli
> olan eski ücretsiz kotalar (3 adet shared-cpu-1x 256mb VM + 3GB disk) bu
> projeyi TAMAMEN ücretsiz karşılayabilir.

### 1) Fly CLI (flyctl) kurulumu

**Windows (PowerShell):**
```powershell
pwsh -Command "iwr https://fly.io/install.ps1 -useb | iex"
```
(Kurulumdan sonra YENİ bir terminal açın ki `fly` komutu PATH'e eklenmiş olsun.)

**Mac/Linux:**
```bash
curl -L https://fly.io/install.sh | sh
```

Kurulumu doğrulayın:
```bash
fly version
```

### 2) Hesap açma / giriş

```bash
fly auth signup    # yeni hesap (e-posta yeterli - deneme süresince kart eklemeniz ŞART DEĞİL, bkz. yukarıdaki uyarı)
# veya zaten hesabınız varsa:
fly auth login
```

Kart eklemeden signup olabilirsiniz; deneme kotanız (2 VM-saati/7 gün) bitene
kadar kart istenmez. Kartı EKLEDİĞİNİZ andan itibaren gerçek ücretlendirme
başlar (bkz. yukarıdaki uyarı ve aşağıdaki "Deneme Süresi Dolunca Ne Olur?").

### 3) Uygulamayı başlatma (`fly launch`)

Proje kök dizininde (bu `fly.toml`'ın bulunduğu klasörde):

```bash
fly launch --no-deploy
```

- `--no-deploy`: hemen deploy etmeden önce `fly.toml`'ı gözden geçirmenize
  izin verir (bu proje için `fly.toml` ZATEN hazır - bkz. proje kökü).
- Fly, mevcut `fly.toml`'ı bulup "bu ayarları kullanmak ister misiniz?" diye
  soracaktır - **Evet** deyin (uygulama adı `finans-haber-toplayici` GLOBAL
  olarak alınmışsa farklı bir isim önerilecek/sorulacaktır - kabul edin veya
  `fly.toml` içindeki `app = "..."` satırını elle değiştirin).
- Bölge sorusunda `fly.toml`'daki `fra` (Frankfurt) varsayılanını
  kullanabilir veya `fly platform regions` ile listeleyip başka birini
  seçebilirsiniz.
- **"Would you like to set up a Postgres/Redis database?"** sorularına
  **Hayır** deyin - bu proje SQLite kullanıyor, ek bir veritabanı servisi
  gerekmiyor.

### 4) Kalıcı disk (volume) oluşturma

`fly.toml` içindeki `[[mounts]]` bölümü, veritabanının/logların/yedeklerin
(`data/` klasörünün TAMAMI) her deploy'da SIFIRLANMAMASI için bir kalıcı disk
tanımlar. Bu volume'u İLK deploy'dan ÖNCE elle oluşturun:

```bash
fly volumes create finans_haber_data --region fra --size 1
```

(`--region`, `fly.toml > primary_region` ile AYNI olmalı; `--size 1` = 1GB,
bu projenin veri boyutu için fazlasıyla yeterli - istenirse sonradan
`fly volumes extend` ile büyütülebilir.)

### 5) Gizli bilgileri (secrets) ayarlama

**ASLA** gerçek API anahtarlarınızı/bot token'ınızı `fly.toml`'a veya koda
yazmayın/commit etmeyin - Fly'ın şifreli "secrets" mekanizmasını kullanın
(bu değerler yalnızca çalışan makineye ortam değişkeni olarak enjekte edilir,
Fly panelinde bile düz metin görüntülenmez):

```bash
fly secrets set GEMINI_API_KEY="gerçek-anahtarınız"
fly secrets set TELEGRAM_BOT_TOKEN="gerçek-bot-tokenınız"
fly secrets set TELEGRAM_CHAT_ID="gerçek-chat-id'niz"

# Yalnızca kullanıyorsanız gerekli (boş bırakılırsa ilgili özellik sessizce
# devre dışı kalır, uygulama çökmez - bkz. .env.example):
fly secrets set ANTHROPIC_API_KEY="gerçek-anahtarınız"
fly secrets set EVENTREGISTRY_API_KEY="gerçek-anahtarınız"
```

Tek komutla hepsini birden de ayarlayabilirsiniz:
```bash
fly secrets set GEMINI_API_KEY="..." TELEGRAM_BOT_TOKEN="..." TELEGRAM_CHAT_ID="..."
```

Ayarlanan secret'ları (değerlerini DEĞİL, sadece isimlerini) doğrulamak için:
```bash
fly secrets list
```

### 6) Deploy

```bash
fly deploy
```

Bu komut, kod tabanınızı Fly'ın uzak build sunucusuna yükler, `Dockerfile`'ı
orada inşa eder ve makineyi başlatır - yerel Docker kurulumu GEREKMEZ. İlk
deploy birkaç dakika sürebilir (bağımlılıkların kurulumu dahil).

### 7) Deploy sonrası kontrol

```bash
fly status           # makine çalışıyor mu, sağlıklı mı (health check durumu)
fly logs             # canlı log akışı (worker taramaları, Telegram bot, hatalar)
fly open             # dashboard'u varsayılan tarayıcıda açar (https://<app-adı>.fly.dev)
```

`fly status` çıktısında health check'in (bkz. `fly.toml > http_service.checks`,
`/health` endpoint'ini kontrol eder) **"passing"** göstermesi beklenir. İlk
birkaç dakika `pending`/`warning` görünmesi normaldir (uygulama başlarken).

Sorun giderme: `fly logs` içinde bir hata görürseniz (ör. eksik bir secret
yüzünden bir özelliğin devre dışı kaldığına dair bir uyarı - bu ZARARSIZDIR
ve uygulamayı çökertmez, bkz. yukarıdaki "gizli bilgiler" notu), `fly ssh console`
ile makineye bağlanıp `cat data/logs/*.log` gibi komutlarla daha ayrıntılı
inceleyebilirsiniz.

### Maliyet Tahmini (yeni/pay-as-you-go hesap için, 2026-07 itibarıyla)

| Kalem | Yaklaşık maliyet |
|---|---|
| 1x shared-cpu-1x, 512mb, 7/24 açık | ~4 USD/ay |
| 1GB kalıcı disk | ~0.15 USD/ay |
| Giden trafik (bu ölçekte - 10 kullanıcı, düşük hacim) | Aylık ücretsiz kotanın (bölgeye göre) büyük olasılıkla altında kalır |
| **Toplam (yaklaşık)** | **~4-5 USD/ay** |

Bellek boyutunu `fly.toml > [[vm]] > memory` alanından `256mb`'ye düşürerek
maliyeti biraz daha azaltabilirsiniz (~2 USD/ay) - ama bu proje TEK bir
süreçte FastAPI + SQLAlchemy + APScheduler + python-telegram-bot + LLM
SDK'larını birlikte çalıştırdığından (bkz. main.py), 256mb'de bellek
yetersizliği (OOM) riski biraz daha yüksektir; `fly status`/`fly logs` ile
izleyip gerekirse `fly scale memory 512` ile büyütebilirsiniz.

**10 kullanıcılık bu ölçek için yeterli mi?** Evet, rahatlıkla - bu ölçekte
darboğaz Fly.io kaynakları değil, LLM sağlayıcısının (Gemini/Claude) kendi
ücretsiz/ücretli kotasıdır (bkz. yukarıdaki "Rate Limit Koruması" bölümü).
1 makine + 1GB disk, 10 kullanıcılık abone/haber verisi için fazlasıyla
yeterlidir.

### Deneme Süresi Dolunca Ne Olur?

`fly.toml` bilerek makineyi 7/24 açık tuttuğundan (bkz. dosya başındaki not),
**2 VM-saatlik deneme kotanız deploy'dan yaklaşık 2 saat sonra tükenir** -
7 günlük üst sınır bu senaryoda pratikte devreye girmez. Kota tükendiğinde:

- Makineniz **DURDURULUR** (dashboard erişilemez hale gelir, worker/Telegram
  bot da durur).
- Devam etmek isterseniz: [fly.io/dashboard](https://fly.io/dashboard) ->
  hesabınız -> Billing bölümünden bir kart eklemeniz istenir. Kartı
  eklediğiniz ANDAN İTİBAREN kullanım gerçek olarak faturalandırılmaya
  başlar (bkz. yukarıdaki "Maliyet Tahmini" - bu ölçekte saatte kuruşlarla
  ifade edilebilecek kadar düşük, ama ARTIK ücretsiz değildir).
- Kart eklemek İSTEMİYORSANIZ, hiçbir şey yapmanıza gerek yok - makine zaten
  durmuş durumda kalır, otomatik bir ücretlendirme OLMAZ (bkz. Fly.io resmi
  dokümantasyonu: kart eklenmeden faturalandırma başlamıyor). Yine de temiz
  bir kapanış için aşağıdaki "Çıkış Planı"nı uygulamanız önerilir.

### Çıkış Planı: Ücretlendirmeden Tamamen Kaçınma

Deneme süresi dolmadan (veya kart eklemeden ÖNCE) her şeyi silip
sıfırlamak isterseniz, şu sırayla ilerleyin - **ÖNEMLİ: volume'lar makineden
AYRI faturalandırılır** (Fly'ın resmi belgelerine göre, bir volume DURMUŞ
bir makineye bağlıyken bile ücretlendirilmeye devam eder), bu yüzden sadece
uygulamayı değil, volume'u da AYRICA silmeniz gerekir:

```bash
# 1) Önce volume ID'sini bulun
fly volumes list -a finans-haber-toplayici

# 2) Volume'u silin (YUKARIDAKİ komuttan aldığınız gerçek ID'yi kullanın -
#    bu GERİ ALINAMAZ, veritabanınız/yedekleriniz kalıcı olarak silinir)
fly volumes destroy <volume-id> -a finans-haber-toplayici

# 3) Uygulamanın kendisini (tüm makineleriyle birlikte) tamamen kaldırın
fly apps destroy finans-haber-toplayici
```

Her ikisini de sildikten sonra `fly apps list` ve
[fly.io/dashboard](https://fly.io/dashboard) üzerinden hiçbir kaynağınızın
kalmadığını doğrulayın. Kart eklediyseniz ve organizasyonu TAMAMEN
kapatmak isterseniz, Billing sayfasından kartı da kaldırabilir veya
hesabınızı silebilirsiniz (bkz. [fly.io/docs/about/billing](https://fly.io/docs/about/billing/)).

**Özet - eve geçip Oracle Cloud'a (veya başka bir kalıcı çözüme) geçmeden
önce:** yukarıdaki 3 komutu çalıştırmanız, Fly.io tarafında hiçbir kaynağın
(ve dolayısıyla hiçbir ücretin) kalmamasını garantiler.

*Kaynaklar (2026-07 itibarıyla güncel bilgi için araştırıldı):
[Fly.io Resmi Fiyatlandırma](https://fly.io/docs/about/pricing/),
[Fly.io Yapılandırma Referansı](https://fly.io/docs/reference/configuration/).*

## GitHub'a Yükleme (versiyon kontrolü)

Fly.io deploy'u GitHub'a bağlı DEĞİLDİR (`fly deploy` doğrudan yerel kod
tabanınızdan yükler) - ama versiyon kontrolü/yedekleme için projeyi ayrıca
GitHub'a da yüklemeniz önerilir. Proje zaten bir git deposu ve bir GitHub
remote'u (`origin`) tanımlı - yalnızca commit'leyip push etmeniz yeterli:

```bash
git add -A
git commit -m "Proje güncellemeleri"
git push origin main
```

**Eğer proje henüz bir git deposu DEĞİLSE** (ör. sıfırdan bir kopyayla
başlıyorsanız), önce şu adımları izleyin:

```bash
git init
git add -A
git commit -m "İlk commit"
```

Sonra [github.com/new](https://github.com/new) adresinden BOŞ bir repo
oluşturun (README/`.gitignore` EKLEMEDEN - proje zaten kendi `.gitignore`'ına
sahip), ardından:

```bash
git remote add origin https://github.com/<kullanici-adiniz>/<repo-adi>.git
git branch -M main
git push -u origin main
```

**ÖNEMLİ:** `.env` dosyanız `.gitignore` içinde zaten hariç tutuluyor - GERÇEK
API anahtarlarınızın/bot token'ınızın YANLIŞLIKLA GitHub'a gitmediğinden emin
olmak için `git status`/`git add` sonrası çıktıyı push etmeden önce bir kez
gözden geçirin (bkz. proje genelindeki güvenlik notları).

## Vercel'e Deploy (Salt-Okunur Dashboard - Hibrit Mimari)

Bu, Fly.io bölümünde anlatılan "tüm uygulamayı taşı" yaklaşımından FARKLI bir
model: **worker (RSS taraması) ve Telegram bot dinleyicisi yerel
bilgisayarınızda (`python main.py` ile) çalışmaya DEVAM EDER** - yalnızca
web dashboard'un SALT-OKUNUR bir kopyası Vercel'de, internetten erişilebilir
şekilde barındırılır. İkisi de AYNI paylaşımlı veritabanını (Neon Postgres)
kullanır, böylece Vercel'deki dashboard her zaman güncel veriyi gösterir.

**Neden bu model?** Vercel'in serverless mimarisi (Hobby planda cron en fazla
günde 1 kez, fonksiyon başına en fazla 300 saniye çalışma süresi) bu
projenin hız-sınırlı LLM özetleme hattını (haber başına ~12.5 sn bekleme)
YETİŞTİREMEZ - bu yüzden worker'ı Vercel'e taşımak yerine yerelde bırakıp
sadece dashboard'u taşıyoruz.

**Ön koşul - kod hazır mı?** Evet: `vercel.json`, `api/index.py` ve
`.vercelignore` dosyaları projede zaten mevcut ve yerel olarak test edildi
(gerçek Neon Postgres verisiyle, tüm rotalar 200 döndü). `src/db.py`,
`DATABASE_URL` ortam değişkeni tanımlıysa otomatik olarak Postgres'e bağlanır
- Vercel'de bu değişkeni ayarlamanız YETERLİ, başka hiçbir kod değişikliği
gerekmez.

### 1) GitHub'a push edin

Vercel'in web arayüzünden "Import Project" yapabilmesi için kod GitHub'da
olmalı. Yukarıdaki **"GitHub'a Yükleme"** bölümündeki adımları izleyin (proje
zaten bir git deposu ve `origin` remote'u tanımlı):

```bash
git add -A
git commit -m "Vercel deploy hazırlığı"
git push origin main
```

### 2) Vercel hesabı açın

[vercel.com/signup](https://vercel.com/signup) - GitHub hesabınızla giriş
yapmanız en kolayı (aynı zamanda repo erişimini otomatik yetkilendirir).
Kredi kartı GEREKMEZ (Hobby plan ücretsizdir).

### 3) Projeyi "Import" edin

1. [vercel.com/new](https://vercel.com/new) adresine gidin.
2. GitHub hesabınızı bağladıysanız repo listesinde projenizi
   (`Haber-Takip-Dedektoru` veya push ettiğiniz repo adı) göreceksiniz -
   yanındaki **"Import"** butonuna tıklayın.
3. **Framework Preset:** Vercel `vercel.json`'ı otomatik algılayıp "Other"
   olarak ayarlayacaktır - elle bir şey seçmenize gerek yok.
4. **Root Directory:** değiştirmeyin, proje kökü olarak kalsın (`.`).
5. **Environment Variables** bölümünü AÇIN (deploy'dan ÖNCE, "Deploy"
   butonuna basmadan önce) ve şunu ekleyin:

   | Name | Value |
   |---|---|
   | `DATABASE_URL` | Neon'dan aldığınız bağlantı dizesi (ör. `postgresql://neondb_owner:...@ep-....neon.tech/neondb?sslmode=require`) - yerel `.env` dosyanızdakiyle AYNI değer |

   Bu dashboard **başka HİÇBİR gizli bilgiye ihtiyaç duymaz** - `GEMINI_API_KEY`,
   `TELEGRAM_BOT_TOKEN` vb. eklemenize GEREK YOK (bkz. `api/index.py` -
   `/sirket-profili` gibi LLM tetikleyen rotalar kasıtlı olarak bu Vercel
   uygulamasına dahil edilmedi).
6. **"Deploy"** butonuna basın. İlk deploy birkaç dakika sürebilir
   (bağımlılıkların kurulumu dahil).

### 4) Deploy sonrası kontrol

Vercel size `https://<proje-adi>.vercel.app` gibi bir URL verecektir.

1. `https://<proje-adi>.vercel.app/health` adresini açın - `{"status":"ok"}`
   dönmesi beklenir.
2. Ana sayfayı (`/`) açın - yerel `python main.py`'nizin topladığı GERÇEK
   haberleri görmeniz gerekir (aynı Neon Postgres veritabanından okunuyor).
3. Filtreleri/aramayı deneyin, `/kaynak-sagligi` sayfasına bakın.
4. Kalıcılığı doğrulamak isterseniz: yerelde worker'ın yeni bir haber
   eklemesini bekleyin (1 dakikada bir tarıyor), birkaç dakika sonra Vercel
   URL'sini yenileyin - yeni haberi orada da görmelisiniz.

Sorun giderme: Vercel projenizin **"Deployments"** sekmesinden build/runtime
loglarına bakabilirsiniz - `DATABASE_URL` yanlış/eksikse dashboard boş
görünür ama ÇÖKMEZ (bkz. src/db.py - Postgres bağlantısı başarısız olursa
hata loglanır, sayfa yine de yüklenir).

**Not:** Worker/Telegram bot yerel bilgisayarınızda çalışmaya devam ettiği
sürece (bilgisayar kapanırsa onlar da durur - bkz. Fly.io/Oracle Cloud
alternatifleri kalıcı bir çözüm için) veri akmaya devam eder; Vercel
dashboard'u yalnızca bu veriyi GÖRÜNTÜLEMEK için var, kendi başına hiçbir
veri toplamaz.

## Render'a Deploy (Vercel Alternatifi - Aynı Hibrit Mimari)

Vercel hesabında ikinci bir proje ücretli çıktığı için **Render.com**
kullanılıyor - AYNI hibrit mimari (worker/bot yerelde, salt-okunur dashboard
bulutta, ikisi de aynı Neon Postgres'i paylaşır), sadece barındırma platformu
farklı.

### Neden Render? (araştırma özeti - 2026-07)

Üç platform karşılaştırıldı, resmi dokümantasyonlardan doğrulandı:

| Platform | Sonuç | Neden |
|---|---|---|
| **Netlify** | ❌ Elendi | Netlify Functions Python'u HİÇ desteklemiyor - ne standart Functions API'de ne de Lambda-uyumlu API'de. Resmi dokümantasyonda (`docs.netlify.com`) yalnızca TypeScript/JavaScript/Go listeleniyor. Python desteği eklemek, tüm dashboard'u JS/Go'ya yeniden yazmak anlamına gelirdi - bu, bir barındırma platformuna uydurmak için mantıklı bir bedel değil. |
| **Cloudflare Workers (Python)** | ⚠️ Riskli, elendi | Python desteği var ama **hâlâ beta** ve Pyodide/WASM tabanlı - `psycopg2` gibi C-uzantılı paketlerin (bizim Postgres sürücümüz) bu ortamda çalışıp çalışmayacağı resmi dokümanlarda bile net değil. Kanıtlanmamış/deneysel bir temel üzerine gerçek bir servis kurmak riskli. |
| **Railway** | ⚠️ Belirsiz | Gerçek Python desteği var (container tabanlı) ama ücretsiz kalıcı katman belirsiz: yeni hesaplara $5'lık BİR KEZLİK deneme kredisi veriliyor (kart istemiyor), credit bitince veya süre dolunca (birkaç gün-hafta) durduruluyor; "Free" plan var ama kaynakları (0.5GB RAM) çok kısıtlı ve kart gerekip gerekmediği dokümanlarda belirtilmemiş. |
| **Render** | ✅ Seçildi | Python'u GERÇEK, sürekli çalışan bir web servisi olarak (kısıtlı bir "serverless fonksiyon" ortamı DEĞİL) native destekliyor - `api/index.py` (Vercel için hazırlanmıştı) **hiçbir kod değişikliği olmadan** burada da çalışıyor (yerelde `uvicorn api.index:app --host 0.0.0.0 --port $PORT` ile gerçek Neon Postgres verisiyle test edildi, tüm rotalar 200 döndü). Ücretsiz katman: ayda 750 instance-saati (tek bir servis için 7/24 yeterli), kart gerektirmiyor (çoğunluk kaynak doğrulaması - bkz. altta not). |

**Kabul edilen tek gerçek kısıt:** Render'ın ücretsiz web servisleri, 15
dakika istek almazsa "uykuya" geçer (spin down) - bir sonraki istekte
uyanması ~1 dakika sürer. Bu, kişisel ölçekli bir dashboard için kabul
edilebilir bir değiş tokuş (ilk ziyaretçi ~1 dakika bekler, sonrasında hızlı).
Kalıcı veri (Neon Postgres) bu uyku durumundan ETKİLENMEZ - sadece web
sunucusu süreci durur, veritabanı bağlantısı her zaman ayrı ve kalıcıdır.

**Kart gereksinimi hakkında dürüstlük notu:** Çoğunluk kaynak (2026 itibarıyla)
kart istenmediğini doğruluyor, ama birkaç kullanıcı raporu ("beklenmedik
ücretlendirme" şikayetleri, muhtemelen bant genişliği aşımı gibi kenar
durumlardan) karışıklık yaratıyor. Kayıt sırasında kart istenirse (bu projenin
kapsamı dışında bir sürpriz olur) durdurup bana bildirin.

### Kod hazır mı?

Evet - `render.yaml` projede zaten mevcut ve `api/index.py`'nin AYNEN Vercel
için hazırlanan haliyle çalıştığı yerel olarak doğrulandı (gerçek Neon
Postgres verisiyle, Render'ın kullanacağı TAM komutla:
`uvicorn api.index:app --host 0.0.0.0 --port $PORT`).

### 1) GitHub'a push edin (henüz etmediyseniz)

Yukarıdaki **"GitHub'a Yükleme"** bölümüne bakın - kod zaten GitHub'da
olmalı (Vercel denemesi için zaten push edilmişti).

### 2) Render hesabı açın

[dashboard.render.com/register](https://dashboard.render.com/register) -
GitHub hesabınızla giriş yapmanız repo erişimini otomatik yetkilendirir.

### 3) "Blueprint" ile deploy edin (render.yaml'ı otomatik kullanır)

1. Render panelinde **"New +"** → **"Blueprint"** seçin.
2. GitHub reposunu (`Haber-Takip-Dedektoru`) seçin - Render otomatik olarak
   proje kökündeki `render.yaml`'ı algılayıp servis ayarlarını (Python
   runtime, build/start komutları) otomatik dolduracaktır.
3. `DATABASE_URL` alanı BOŞ görünecektir (`sync: false` olarak işaretli,
   bkz. `render.yaml`) - buraya Neon bağlantı dizenizi (yerel `.env`'deki AYNI
   değer) elle yapıştırın.
4. **"Apply"** / **"Deploy"** butonuna basın.

**Alternatif (Blueprint kullanmadan, elle):**
1. **"New +"** → **"Web Service"** → reponuzu seçin.
2. **Runtime:** Python 3 | **Build Command:** `pip install -r requirements.txt`
   | **Start Command:** `uvicorn api.index:app --host 0.0.0.0 --port $PORT`
3. **Environment** sekmesinden `DATABASE_URL` değişkenini ekleyin.
4. **"Create Web Service"**.

### 4) Deploy sonrası kontrol

Render size `https://<servis-adi>.onrender.com` gibi bir URL verecektir.

1. `/health` adresini açın - `{"status":"ok"}` bekleniyor (ilk istekte
   servis "uykudaysa" ~1 dakika sürebilir).
2. Ana sayfayı (`/`) açın - yerel worker'ınızın topladığı GERÇEK haberleri
   görmeniz gerekir.
3. Kalıcılığı doğrulamak için: yerel worker yeni bir haber ekledikten
   (~1 dakikada bir tarıyor) birkaç dakika sonra sayfayı yenileyin - aynı
   yeni haberi orada da görmelisiniz.

Sorun giderme: Render panelinin **"Logs"** sekmesinden build/runtime
loglarına bakabilirsiniz.

### 5) Render'ın uykuya dalmasını engelleme (UptimeRobot ile)

Render'ın ücretsiz katmanı, servis **15 dakika** boyunca hiç istek almazsa
"uykuya" dalar; bir sonraki ziyaretçi geldiğinde servis yeniden
başlatılırken Render'ın kendi genel "spinning up" ekranını görür (bu
projenin kendi marka/logolu splash sayfası - bkz. `splash-page/index.html`
- bunu daha kullanıcı dostu hale getiriyor ama servisi UYKUYA
GİRMEKTEN alıkoymuyor). Servisi tamamen uyanık tutmanın ücretsiz yolu,
düzenli aralıklarla `/health` adresine ping atan bir "uptime pinger"
kullanmaktır - `/health` KASITLI OLARAK çok hafiftir (veritabanı sorgusu
YAPMAZ, sadece sabit bir `{"status": "ok"}` döner, bkz. `src/web/app.py`),
bu yüzden sık ping atmak Render'ın kısıtlı ücretsiz kaynaklarını
tüketmez.

**UptimeRobot ile adım adım kurulum** (ücretsiz, ~5 dakika sürer):

1. [uptimerobot.com](https://uptimerobot.com) adresine gidip ücretsiz bir
   hesap açın (e-posta ile kayıt yeterli, kredi kartı istenmez).
2. Panelde **"Add New Monitor"** butonuna tıklayın.
3. Monitor ayarlarını şu şekilde doldurun:
   - **Monitor Type**: `HTTP(s)`
   - **Friendly Name**: istediğiniz bir isim (ör. "Finans Haber Dashboard")
   - **URL (or IP)**: `https://finans-haber-dashboard.onrender.com/health`
   - **Monitoring Interval**: `5 minutes` (Render'ın 15 dakikalık uyku
     eşiğinin oldukça altında - servis asla uykuya dalacak kadar boş
     kalmaz; ücretsiz UptimeRobot planı zaten en sık bu aralığı sunar)
4. **"Create Monitor"** (veya "Save") ile kaydedin.

Bu kadar - UptimeRobot artık her 5 dakikada bir `/health`'i ziyaret
edecek, servis hiçbir zaman 15 dakikalık inaktivite eşiğine ulaşmayacak
ve gerçek kullanıcılar HİÇBİR ZAMAN "uyanma" gecikmesiyle
karşılaşmayacaktır. Ek bir bonus: UptimeRobot, servis gerçekten çökerse
(health check başarısız olursa) size e-posta/bildirim de gönderebilir
(monitor ayarlarındaki "Alert Contacts" kısmından açılır).

## Yapılandırma (`config.yaml`)

- `app`: zaman aşımı, rate-limit, User-Agent, tekilleştirme (dedup) eşiği gibi genel ayarlar
- `worker.interval_minutes`: otomatik tarama aralığı
- `summarizer.llm_provider`: `"gemini"` (varsayılan) veya `"anthropic"` — hangi LLM
  sağlayıcısının kullanılacağı; `gemini_model`/`anthropic_model` ilgili sağlayıcının
  modeli (varsayılan sırasıyla `gemini-3.5-flash-lite` / `claude-sonnet-4-6`), ayrıca
  efor seviyesi ve dil ayarları
- `summarizer.rate_limit_enabled` / `requests_per_minute` / `max_retries`: 429
  (rate limit) hatalarını önlemek için istekler arası bekleme ve otomatik
  tekrar deneme — bkz. "Rate Limit Koruması" bölümü
- `importance.threshold`: 1-5 arası, bu ve üzeri skor alan haberler Telegram'a gider (varsayılan **4**)
- `telegram.enabled`: bildirim adımını tamamen açıp kapatmak için (token yoksa zaten otomatik atlanır)
- `database.path`: SQLite dosya yolu
- `web`: `host`, `port`, `refresh_seconds` (dashboard otomatik yenileme aralığı), `max_items`
- `sources`: her kaynak için `name`, `id`, `type` (`rss` / `scrape` / `licensed_aggregator`), `enabled`, ve türe özgü alanlar

Yeni bir RSS kaynağı eklemek için `sources` listesine şunu eklemeniz yeterli:

```yaml
- name: "Kaynak Adı"
  id: kaynak_id
  type: rss
  enabled: true
  url: "https://ornek.com/rss"
```

## Kaynakların Gerçek Durumu (test edildi)

Geliştirme sırasında her kaynağın gerçek RSS/robots.txt durumu (curl ile ve
uygulamanın kendi robots.txt kontrolüyle) doğrulandı:

| Kaynak | Durum | Not |
|---|---|---|
| **Bloomberg HT** | ✅ RSS çalışıyor | `bloombergHT.com/rss` genel bir RSS feed sunuyor |
| **CNBC-e** | ✅ RSS çalışıyor | `cnbce.com/rss` genel bir RSS feed sunuyor |
| **Foreks** | ⚠️ RSS var ama bot koruması var | `foreks.com/rss` içerik olarak çalışıyor, ancak site CloudFront tabanlı bot korumasına sahip ve zaman zaman (yoğun istek sonrası) 403 dönebiliyor. Kod bunu robots.txt üzerinden algılayıp kaynağı **güvenli şekilde atlar**, çalışmayı durdurmaz. |
| **Yahoo Finance** | ✅ RSS çalışıyor | `finance.yahoo.com/news/rssindex` resmi "top stories" feed'i, robots.txt bu yolu yasaklamıyor |
| **New York Times (Business)** | ✅ RSS çalışıyor | `rss.nytimes.com/services/xml/rss/nyt/Business.xml` resmi Business feed'i. NYT'nin ana sitesinde genel scraping'i yasaklayan bir uyarı olsa da, bu ayrı `rss.nytimes.com` alt alan adı bilinçli olarak yayınlanan bir syndication kanalıdır ve kendi robots.txt'i bu yolu engellemez. |
| **Euronews (Business)** | ✅ RSS çalışıyor | `euronews.com/rss?level=theme&name=business` resmi tema bazlı feed, kanal başlığı "Business \| Euronews RSS" olarak doğrulandı |
| **Ekonomim.com (Ekonomi)** | ✅ RSS çalışıyor | `ekonomim.com/robots.txt` `User-agent: *` için `Allow: /` diyor (sadece `/ara` ve `/video-embed` hariç). Kategori bazlı resmi RSS servisleri var (`/rss/ekonomi.xml`, `/rss/finans.xml`, `/rss/sirketler.xml` vb.); projenin finans odağına en uygun olan **Ekonomi** kategorisi seçildi (genel `/rss` feed'i gündem/magazin haberleri de içerdiğinden tercih edilmedi). Test edildi (2026-07): 200 OK, `feedparser` `bozo=False`, 25 güncel haber. |
| **Channel News Asia (CNA)** | ⚠️ Devre dışı (kota koruması) | RSS'in kendisi çalışıyor: `channelnewsasia.com/robots.txt` genel `Disallow: /api/*` kuralına rağmen `/api/v1/rss-outbound-feed` yolunu özel olarak `Allow` ediyor (daha spesifik kural kazanır, RFC 9309) — hem `curl` hem `urllib.robotparser` (projenin kullandığı kütüphane) ile doğrulandı. Business/Asia'ya özel bir feed varyantı **yok** (`?category=business` 0 öğe döndürüyor, `?type=business` genel feed'in aynısı, `/business/rss.xml` 404) — bu yüzden resmi genel "Latest News" feed'i kullanılıyor. Test edildi (2026-07): 200 OK, `feedparser` `bozo=False`, gerçek `fetch_rss()` çağrısıyla 15 güncel haber (Endonezya merkez bankası başkanının istifası, iş anlaşmaları vb.). **2026-07-29'da `enabled: false` yapıldı** — yüksek hacimli genel feed olması Gemini'nin günlük ücretsiz kotasını (RPD) hızla tükettiği ve kullanıcı için kritik önemde olmadığı için (bkz. "Eklenip Sonradan Kapatılan Kaynaklar"). |
| **Business Insider** | ⚠️ Devre dışı (kota koruması) | RSS'in kendisi çalışıyor: finansa özel bir varyant yok, resmi genel `/rss` feed'i kullanılıyor; robots.txt bu yolu genel User-Agent için yasaklamıyor. Test edildi (2026-07): 200 OK, `feedparser` `bozo=False`. **2026-07-29'da `enabled: false` yapıldı** (aynı kota gerekçesi) — içeriğine hâlâ lisanslı `licensed_reuters_bloomberg` kaynağı üzerinden erişiliyor. |
| **City A.M.** | ⚠️ Devre dışı (kota koruması) | RSS'in kendisi çalışıyor: "London's Business Newspaper" — tüm site zaten finans/iş odaklı olduğundan genel `/feed/` kullanılıyor (Bloomberg HT/CNBC-e ile aynı desen). robots.txt tamamen açık (`Disallow:` boş, isim isim AI bot yasağı yok). Test edildi (2026-07): 200 OK, geçerli WordPress RSS. **2026-07-29'da `enabled: false` yapıldı** (aynı kota gerekçesi). |
| **South China Morning Post (Business)** | ⚠️ Devre dışı (kota koruması) | RSS'in kendisi çalışıyor: robots.txt genel User-Agent için açık (isim isim AI bot yasağı yok). Resmi RSS listeleme sayfasındaki (`scmp.com/rss`) "Business" bölüm feed'i (`/rss/92/feed`) kullanılıyor. Test edildi (2026-07): 200 OK, kanal başlığı "Business - South China Morning Post", 50 güncel haber. **2026-07-29'da `enabled: false` yapıldı** (aynı kota gerekçesi — 50 haberlik yüksek hacim). |
| **The Straits Times (Business)** | ⚠️ Devre dışı (kota koruması) | RSS'in kendisi çalışıyor: robots.txt genel User-Agent için açık (isim isim AI bot yasağı yok). Resmi business kategori feed'i (`/news/business/rss.xml`) kullanılıyor. Test edildi (2026-07): 200 OK, 50 güncel haber (kanal başlığı şablon nedeniyle genel görünse de içerik net şekilde business). **2026-07-29'da `enabled: false` yapıldı** (aynı kota gerekçesi — 50 haberlik yüksek hacim). |
| **MarketWatch** | ❌ Varsayılan olarak kapalı | RSS teknik olarak veri döndürüyor (200 OK) ancak robots.txt `User-agent: *` için siteyi **tamamen** yasaklıyor (`Disallow: /`) ve yalnızca Google/Bing/ChatGPT-User gibi adı geçen botlara izin veriyor; ayrıca dosyanın başında "otomatik toplama, Dow Jones'tan yazılı izin olmadan yasaktır" şeklinde açık bir hukuki uyarı var. Reuters ile aynı gerekçeyle **kasıtlı olarak kapalı** tutuldu (kullanıcı kararıyla `enabled: false`). |
| **Reuters** | ✅ Sadece lisanslı yolla dahil | Reuters'ın genel erişime açık, izinsiz kullanılabilecek resmi bir RSS feed'i yok ve robots.txt'i genel botları tamamen kapatıyor. Google hesabıyla login olup scraping yapmak hem güvenlik riski (kimlik bilgilerinin bir otomasyona verilmesi) hem de kullanım şartları ihlali olacağından **bu yol projede hiç kullanılmadı**. Bunun yerine, Reuters içeriğine **NewsAPI.ai (Event Registry)** adlı lisanslı bir aracı API üzerinden erişiliyor — ayrıntılar için aşağıdaki "Lisanslı Kaynak" bölümüne bakın. |
| **Bloomberg (global)** | ⚠️ Doğrudan kapalı, lisanslı yolla açık | `bloomberg.com` robots.txt'i genel bir bot için çoğu sayfaya izin verse de, site tamamen JavaScript ile render ediliyor (React SPA) ve içerik büyük ölçüde paywall arkasında; bu yüzden doğrudan scrape girişi (`bloomberg` kaynağı) varsayılan olarak kapalı. Bloomberg içeriğine asıl erişim yolu da, Reuters gibi, aynı lisanslı NewsAPI.ai kaynağıdır. |

Şu anda **7 doğrudan RSS kaynağı aktif (`enabled: true`)**: Bloomberg HT, CNBC-e,
Foreks (ara sıra bot korumasına takılabilir), Yahoo Finance, New York Times
(Business), Euronews (Business), Ekonomim.com (Ekonomi) — artı tek bir lisanslı
kaynak (NewsAPI.ai üzerinden Reuters, Bloomberg, Investing.com, CNBC, Forbes,
MarketWatch, Financial Times, WSJ, Business Insider, Barron's, Fortune,
Economist — 12 site, tek `sourceUri` sorgusunda birleşik, bkz. aşağıdaki
"Lisanslı Kaynaklar" bölümü).

MarketWatch (doğrudan RSS/scrape) ve Bloomberg (doğrudan scrape) `enabled: false`
ile projede duruyor; robots.txt izin verecek şekilde değişirse veya resmi izin
alırsanız tek satır değiştirip açabilirsiniz.

#### Eklenip Sonradan Kapatılan Kaynaklar (kota koruması)

Channel News Asia (CNA), Business Insider, City A.M., South China Morning Post
(Business) ve The Straits Times (Business) 2026-07-29'da eklenmiş, ama gerçek
çalıştırmada topluca yüksek hacim üretip (bazıları 50 haber/tarama) Gemini'nin
günlük ücretsiz kotasını (RPD) hızla tükettikleri ve kullanıcı için kritik
önemde olmadıkları ortaya çıktığı için aynı gün `enabled: false` yapılarak
devre dışı bırakıldı. RSS URL'leri doğru ve test edilmiş durumda config.yaml'da
kalmaya devam ediyor — tekrar açmak isterseniz ilgili kaynağın
`enabled: false` satırını `enabled: true` yapmanız yeterli.

### Eklenmeyen Kaynaklar

Aşağıdaki kaynaklar **hiç eklenmedi** (config.yaml'da hiçbir satırları yok, `enabled: false` bile değiller) çünkü robots.txt/erişim kontrolü net bir şekilde otomatik erişimi engelliyor:

| Kaynak | Neden eklenmedi |
|---|---|
| **Investing.com** (`investing.com`, `tr.investing.com`) | Kullanıcının belirttiği resmi RSS duyuru sayfası (`tr.investing.com/webmaster-tools/rss`) kontrol edildi ve kendisi erişilebilir olsa da, **robots.txt'in kendisi projenin bot User-Agent'ına 403 Forbidden döndürdü** — hatta standart bir tarayıcı User-Agent'ı ile bile aynı sonuç alındı (`www.investing.com` ayrıca Cloudflare JS bot-koruma sayfası gösteriyor). `src/fetchers/base.py`'nin kendi politikası gereği (401/403 → "tamamen yasak" olarak yorumlanır, bkz. kod içi yorum), bu kaynak robots.txt seviyesinde net şekilde engellenmiş sayılır. Bu yüzden **eklenmedi**. Site ileride bot erişimine izin verecek şekilde değişirse (veya resmi bir API/lisanslı erişim yolu bulunursa) yeniden değerlendirilebilir.
| **Handelsblatt** (`handelsblatt.com`) | Resmi RSS feed'leri var (`feeds.cms.handelsblatt.com/finanzen` — 200 OK, çalışıyor) ve o alt alan adının kendi robots.txt'i bile yok. Ancak ana domainin (`www.handelsblatt.com`) robots.txt'i, `anthropic-ai`, `ClaudeBot`, `Claude-Web`, `Claude-SearchBot` gibi AI botlarını **isim isim** `Disallow: /` ile tüm siteden yasaklıyor. Teknik olarak farklı bir alt alan adı/User-Agent üzerinden erişim mümkün olsa da, yayıncının AI botlarına karşı **açık niyeti** göz önünde bulundurularak (kullanıcı kararıyla) bu kaynak **eklenmedi**. |
| **The Telegraph (Business)** (`telegraph.co.uk`) | robots.txt, `ClaudeBot`, `Claude-Web`, `Claude-SearchBot`, `Claude-User`, `anthropic-ai` gibi AI botlarını **her biri ayrı bir grupta** `Disallow: /` ile tüm siteden yasaklıyor — Handelsblatt'takinden de açık bir engelleme. Aynı gerekçeyle (kullanıcı kararıyla) **eklenmedi**. |

## Lisanslı Kaynaklar (NewsAPI.ai)

Reuters ve Bloomberg'in kendi sitelerinden **resmi/izinsiz** bir şekilde haber
çekmenin bir yolu yok (bkz. yukarıdaki tablo). Bu ikisine erişmek için, bu
kaynakları resmi olarak lisanslayıp bir API üzerinden sunan üçüncü taraf bir
servis kullanıldı. Bu bölüm, hangi servisin neden seçildiğini ve nasıl
kurulacağını anlatır.

### Kapsanan Siteler

`licensed_reuters_bloomberg` kaynağı, **tek bir `sourceUri` sorgusuyla**
(bkz. aşağıdaki "Kota Koruması" bölümü — kaynak sayısı arttıkça sorgu
büyür ama gerçek API çağrısı SAYISI artmaz) aşağıdaki 12 siteyi kapsar.
Her biri için önce `er.getNewsSourceUri(<domain>)` GERÇEK API key'imizle
tek tek sorgulanıp EventRegistry'nin veritabanında gerçekten var olduğu
doğrulandı (uydurma/var olmayan bir domain ile sağlama yapıldı, o `None`
döndü — yani bu doğrulama yöntemi güvenilir); sonra hepsi TEK bir birleşik
sorguda (izole bir test ortamında, üretime dokunmadan) gerçekten test
edildi:

| Site | `sourceUri` doğrulandı mı | Test sorgusunda örnek makale geldi mi |
|---|---|---|
| Reuters (`reuters.com`) | ✅ | ✅ (12 makale) |
| Bloomberg (`bloomberg.com`) | ✅ | ✅ (12 makale) |
| Investing.com (`investing.com`) | ✅ | ✅ (22 makale) |
| Forbes (`forbes.com`) | ✅ | ✅ (4 makale) |
| Financial Times (`ft.com`) | ✅ | ✅ (2 makale) |
| WSJ (`wsj.com`) | ✅ | ✅ (4 makale) |
| Business Insider (`businessinsider.com`) | ✅ | ✅ (3 makale) |
| Barron's (`barrons.com`) | ✅ | ✅ (1 makale) |
| CNBC (`cnbc.com`) | ✅ | ⚠️ bu örneklemede 0 (kaynak geçerli, o an yayın hacmi düşük olabilir) |
| MarketWatch (`marketwatch.com`) | ✅ | ⚠️ bu örneklemede 0 (aynı not) |
| Fortune (`fortune.com`) | ✅ | ⚠️ bu örneklemede 0 (aynı not) |
| Economist (`economist.com`) | ✅ | ⚠️ bu örneklemede 0 (aynı not) |

12 sitenin **12'si de** `getNewsSourceUri` ile doğrulandı (0 "bulunamadı"
oldu). Tek bir 60 makalelik örnekleme turunda 8'inden fiilen makale geldi;
kalan 4'ü (CNBC, MarketWatch, Fortune, Economist) o anki yayın hacmine bağlı
olarak boş dönmüş olabilir — kaynak geçersiz olduğu için değil (zaten ayrıca
doğrulanmıştı). Gerçek kullanımda zaman içinde bu kaynaklardan da makale
gelmesi beklenir.

> **Not:** MarketWatch, doğrudan RSS/scrape kaynağı olarak `enabled: false`
> (robots.txt engeli, bkz. yukarıdaki tablo) — ama bu lisanslı yol üzerinden
> (NewsAPI.ai) içeriğine erişim **mevcut ve açık**, robots.txt kısıtlaması
> bu yolu etkilemez (tıpkı Reuters/Bloomberg gibi).

### Karşılaştırma: finlight.me vs. NewsAPI.ai (Event Registry)

| | **finlight.me** | **NewsAPI.ai (Event Registry)** |
|---|---|---|
| Ücretsiz plan | Ayda 5.000 istek, kredi kartı gerekmiyor | Ayda 2.000 istek/token, kredi kartı gerekmiyor |
| Kaynak filtreleme (ücretsiz planda) | ❌ **Yok** — finlight'ın kendi fiyatlandırma sayfası ücretsiz planda "haber kaynakları veya ticker/entities erişimi yok" ve verilerin 12 saat gecikmeli olduğunu açıkça belirtiyor | ✅ Var — `sourceUri` parametresiyle tek tek kaynağa (ör. `reuters.com`, `bloomberg.com`) filtrelenebiliyor |
| Reuters/Bloomberg'e özel filtre | Belirsiz/mümkün değil (kaynak erişimi ücretsiz planda kapalı) | Doğrulandı: resmi Python SDK'sında `sourceUri = QueryItems.OR(["reuters.com", "bloomberg.com"])` kalıbı belgelenmiş |

**Seçim: NewsAPI.ai (Event Registry).** Gerekçe basit: bizim tek ihtiyacımız
"sadece Reuters ve Bloomberg'e filtrele" özelliği, ve finlight'ın ücretsiz
planı bunu (kaynak erişimini) açıkça hariç tutuyor — yani finlight'ı seçsek bile
ücretsiz katmanda istediğimiz filtrelemeyi yapamayacaktık. NewsAPI.ai'nin
ücretsiz planı hem bu filtrelemeyi destekliyor hem de aylık kotası (2.000
istek) bu proje için (30 dakikalık bir zamanlayıcıdan bağımsız, kendi
kota-korumalı önbellekleme mekanizmasıyla) yeterli.

### Hesap Açma ve API Key Alma

1. https://www.newsapi.ai/register adresinden ücretsiz bir hesap açın (kredi
   kartı istenmiyor).
2. Giriş yaptıktan sonra Dashboard'daki API Key'inizi kopyalayın
   (`https://newsapi.ai/dashboard`).
3. `python main.py`'yi çalıştırıp anahtarı istendiğinde girin — otomatik
   olarak doğrulanıp `.env`'e kaydedilir (bkz. "Eksik API Anahtarları"
   bölümü). Elle eklemek isterseniz `.env` dosyanıza şu satırı ekleyin:
   ```
   EVENTREGISTRY_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
4. `EVENTREGISTRY_API_KEY` tanımlı değilse bu kaynak **hatasız** şekilde
   atlanır (bir uyarı loglanır, program çökmez) — diğer tüm kaynaklarla aynı
   hata izolasyonu mantığı geçerlidir.

### Ücretsiz Planın Sınırları

- **Ayda 2.000 istek/token.** Kullanılmayan haklar bir sonraki aya devretmiyor.
- **Sadece güncel veri** (son 30 gün); tarihsel (2014'ten itibaren) arşiv
  erişimi ücretli planlarda.
- Free plan sayfasında Reuters/Bloomberg gibi belirli kaynaklara erişimin
  engellendiğine dair bir ifade **yok** (finlight'ın aksine) — `sourceUri`
  filtrelemesi temel bir sorgu parametresi, plan bazlı bir kısıtlama değil.

### Kota Koruması Nasıl Çalışıyor

30 dakikalık ana zamanlayıcı ile 2.000/ay kotası doğrudan çakışır (30 dk'da
bir gerçek çağrı yapılsa ayda ~1.440 çağrı olur, kotanın çoğunu tüketir). Bu
yüzden `src/fetchers/licensed_aggregator.py` kendi bağımsız kota koruma
katmanını içerir:

> **`source_domains` listesine kaynak eklemek gerçek API çağrısı sayısını
> ARTIRMAZ.** `_fetch_from_api()` (bkz. `src/fetchers/licensed_aggregator.py`),
> `source_domains` listesindeki TÜM siteleri `sourceUri=QueryItems.OR([...])`
> ile TEK bir sorgu nesnesine koyup TEK bir `query.execQuery(...)` çağrısı
> yapar — kaynak listesi 2 site olsa da 12 site olsa da, `fetch_licensed_aggregator()`
> her çağrıldığında `calls_this_month` sayacı yalnızca **1** artar (aynı
> fonksiyonun içinde, kaynak sayısından bağımsız tek bir yerde). Bu, hem
> koddan (tek `execQuery` çağrısı) hem de yukarıdaki test sırasında (12
> kaynağı birlikte sorgulayan TEK bir istekle 60 makalenin TEK bir HTTP
> yanıtında gelmesiyle) doğrulandı — aşağıdaki `min_interval_minutes`/
> `monthly_call_quota` ayarlarına hiç dokunulmadı, önceki değerleriyle aynen
> kaldı.

- `config.yaml` → `licensed_reuters_bloomberg.min_interval_minutes` (varsayılan
  **120 dk**): gerçek API çağrısı en fazla bu sıklıkla yapılır — ana
  zamanlayıcı 30 dakikada bir çalışsa bile, aradaki 3 çalıştırmada gerçek API
  çağrısı yapılmaz.
- Gerçek çağrılar arasında, `data/state/licensed_aggregator_state.json`
  içindeki **son başarılı sonuç önbellekten** döner — böylece her çalıştırmada
  boş dönmez, sadece veri "tazeliği" 2 saatte bir güncellenir.
- `config.yaml` → `monthly_call_quota` (varsayılan **200**, ücretsiz limitin
  oldukça altında): bu ay yapılan gerçek çağrı sayısı bu değere ulaşırsa,
  kalan günler boyunca kaynak sessizce (loglayarak) önbelleğe döner, hata
  fırlatmaz.
- `config.yaml` → `source_domains: ["reuters.com", "bloomberg.com"]`: yalnızca
  bu iki kaynaktan gelen makaleler istenir; `keywords` alanı eklenerek ek bir
  konu filtresi de tanımlanabilir.

Bu sayede günde en fazla 12, ayda ~360 gerçek API çağrısı yapılır — ücretsiz
2.000 kotasının rahatça altında — ve yine de her 30 dakikalık çalıştırmada
en güncel önbellekteki Reuters/Bloomberg haberleri gösterilir.

### Pipeline Entegrasyonu

Bu kaynak `type: licensed_aggregator` ile `config.yaml`'da tanımlı ve
`src/main.py`'deki `fetch_source()` yönlendiricisine eklendi. Döndürdüğü
`NewsItem` nesneleri diğer tüm kaynaklarla **birebir aynı model**i kullandığı
için tekilleştirme (`deduplicator.py`) ve özetleme (`summarizer.py`, Gemini/Claude)
adımlarından herhangi bir özel kod gerekmeden geçer — pipeline'ın geri kalanı
bu kaynağın "lisanslı bir aracı API" olduğunun farkında bile değildir.

## Nasıl Çalışır (akış)

Her tur (`src/main.py` → `run_once()`, `worker.py` tarafından periyodik olarak
veya `python -m src.main` ile tek seferlik çağrılır):

1. **Çekim** (`fetch_all_sources`): her aktif kaynağı ayrı bir thread'de çeker;
   bir kaynak hata verirse (ağ hatası, robots.txt engeli, HTML yapısı değişmiş,
   API key eksik, aylık kota dolu vb.) sadece o kaynak atlanır, diğerleri
   etkilenmez, hata loglanır (gereksinim #7).
2. **Gruplama** (`deduplicator.group_similar_news`): başlık benzerliği (difflib)
   + zaman penceresine göre farklı kaynaklardaki aynı konulu haberleri gruplar.
3. **Önbellek kontrolü:** her grubun başlığından kararlı bir `group_key`
   hesaplanır (`db.compute_group_key`); veritabanında bu anahtar için zaten
   LLM tarafından üretilmiş **gerçek** bir özet/skor varsa (`importance_score`
   dolu — ör. bir önceki 30 dakikalık taramada aynı haber işlendiyse),
   **tekrar API çağrısı yapılmadan** o özet/skor gruba kopyalanır. API hatası/
   anahtar eksikliği yüzünden oluşan "özetlenemedi" yer tutucusu
   `importance_score`'u `None` bıraktığından **hiçbir zaman önbelleğe alınmaz**
   — bir sonraki taramada tekrar denenir. Bu hem maliyet optimizasyonu hem de
   "sık aralıklı tarama" gereksiniminin doğal bir sonucu.
4. **Özetleme + Önem Skorlama** (`summarizer.Summarizer`, sadece yeni/önbellekte
   olmayan gruplar için, rate limit'e uygun aralıklarla — bkz. "Rate Limit
   Koruması" bölümü): seçili LLM sağlayıcısına (varsayılan Gemini
   `gemini-3.5-flash-lite`, sınırlı bir `thinking_budget` + `effort: low`;
   alternatif olarak Anthropic `claude-sonnet-4-6`, `thinking: disabled` +
   `effort: low`) TEK bir çağrıda hem özet hem de önem skorunu sorar; modelden **kendi cümleleriyle**
   2-4 cümlelik özet + varsa madde madde önemli noktalar + 1-5 arası önem
   skoru + kısa gerekçe içeren tek bir JSON ister. Telif hakkı gereği sistem
   promptu birebir kopyalamayı açıkça yasaklar. Her iki sağlayıcı da **aynı
   prompt'u ve aynı JSON şemasını** kullanır.
5. **Kalıcı hale getirme + bildirim** (`db.upsert_group`, `notifier.send_telegram_notification`):
   tüm gruplar SQLite'a kaydedilir; önem skoru `config.yaml → importance.threshold`
   değerini geçen ve **daha önce bildirilmemiş** haberler için Telegram
   bildirimi gönderilir, başarılıysa `notified=True` işaretlenir (bkz. aşağıdaki
   Telegram bölümü).
6. **Çıktı**: `output/cli_output.py` ve `output/markdown_output.py` sonuçları
   tarihe göre sıralı şekilde terminale basar ve `data/reports/YYYYMMDD_HHMMSS.md`
   dosyasına yazar; ayrıca web dashboard veritabanından okuyarak aynı verileri
   tarayıcıda gösterir (bkz. ilgili bölüm).

## Önem Skorlama

`summarizer.py`, her haber grubunu özetlerken (Gemini veya Anthropic, hangisi
seçiliyse) **aynı API çağrısında** 1-5 arası bir önem skoru da üretir (ekstra
bir çağrı yapılmaz — maliyet artmaz):

| Skor | Anlamı |
|---|---|
| **5** | Piyasayı doğrudan ve güçlü şekilde etkileyebilecek gelişme: merkez bankası faiz kararı, beklenmedik enflasyon/işsizlik verisi, büyük bir döviz/piyasa şoku, savaş/jeopolitik kriz vb. |
| **4** | Önemli ama 5 kadar acil olmayan gelişme: büyük bir şirketin beklentilerden ciddi sapan çeyreklik sonuçları, kritik bir düzenleyici karar, büyük birleşme/satın alma. |
| **3** | Orta düzeyde ilgi çekici, ama acil aksiyon gerektirmeyen haber. |
| **2** | Rutin şirket/sektör haberi. |
| **1** | Genel/finansal önemi düşük haber. |

Skor üretilemezse (API anahtarı yok, ayrıştırma hatası vb.) `None` kalır — bu
haberler **asla** Telegram eşiğini geçemez, sadece normal çıktıda görünür.

Eşik `config.yaml → importance.threshold` üzerinden ayarlanır (**varsayılan 4**):
bu değeri düşürürseniz daha fazla haber Telegram'a gider, yükseltirseniz daha az.

## Rate Limit Koruması

Gemini'nin ücretsiz katmanının hem **dakika-başına** (RPM) hem de **günlük**
(RPD - "requests per day") istek limitleri vardır ve model bazında ciddi
şekilde değişir. Bunu test ederken gerçek bir örnekle karşılaştık: flagship
model `gemini-3.6-flash`'ın ücretsiz kotası günde sadece **20 istek**
(`GenerateRequestsPerDayPerProjectPerModel-FreeTier`) çıktı — RPM'i aşmasak
bile bu, ~90 haberlik bir tarama için baştan yetersizdi. Bu yüzden varsayılan
model, çok daha yüksek günlük kotaya sahip `gemini-3.5-flash-lite` olarak
değiştirildi (bkz. yukarıdaki not). Onlarca haberi (bir taramada tipik olarak
~70-90 grup) art arda hızlı özetlemeye/skorlamaya çalışmak yine de RPM
limitini aşıp `429 RESOURCE_EXHAUSTED` hatasına yol açabilir. `summarizer.py`
bunu üç mekanizmayla ele alır:

1. **İstekler arası bekleme (throttle).** Her API çağrısından önce, bir
   önceki çağrının üzerinden `config.yaml → summarizer.requests_per_minute`
   (varsayılan **5**) limitine göre hesaplanan minimum süre (60/RPM + küçük
   bir güvenlik payı, RPM=5 için ~12.5 sn) geçmediyse beklenir. Bu sayede
   art arda gelen istekler asla RPM limitini aşmaz.
2. **429 alınırsa otomatik tekrar deneme.** Yine de bir 429 hatası alınırsa,
   Google'ın (Gemini) yanıtındaki `retryDelay` alanı (ör. `"38s"`) ya da
   Anthropic'in `retry-after` header'ı okunur ve tam o kadar beklenip
   otomatik olarak tekrar denenir — en fazla `config.yaml →
   summarizer.max_retries` (varsayılan **3**) kez.
3. **Günlük kota tükendiyse hemen vazgeçme.** 429 hatasının `quotaId`'si bir
   GÜNLÜK kota tükenmesine işaret ediyorsa (ör. içinde "PerDay" geçiyorsa),
   `retryDelay` kadar beklemenin bir faydası olmaz (kota ancak ertesi gün
   sıfırlanır) — bu durumda tekrar denemeden direkt vazgeçilip net bir hata
   loglanır ve normal fallback'e düşülür. Bu ayrım olmasaydı, günlük kota
   tükendiğinde her haber için gereksiz yere birkaç dakika boşuna beklenirdi.

Her iki durumda da (RPM tükenmesi ya da tüm denemelerin başarısız olması) o
haber için özetleme başarısız sayılır ve ham metin fallback'ine düşülür
(önem skoru `None` kalır, uygulama çökmez).

Ayarlanabilir/kapatılabilir:

```yaml
summarizer:
  rate_limit_enabled: true      # false yaparsanız bekleme tamamen kapanır
  requests_per_minute: 5        # ücretsiz Gemini için güvenli varsayılan
  max_retries: 3                # RPM kaynaklı 429'da en fazla kaç kez tekrar denensin
```

**Pratik sonuç:** ~90 haberlik bir tarama, 5 RPM ile ~90 × 12.5 sn ≈ 19 dakika
sürebilir (özetlenmemiş/önbellekte olmayan gruplar için — bkz. "Nasıl Çalışır"
adım 3, zaten özetlenmiş haberler için tekrar API çağrısı yapılmaz). Ücretli
bir plana geçer ya da Anthropic kullanırsanız `requests_per_minute` değerini
yükselterek bu süreyi kısaltabilirsiniz. Günlük kota modele göre değiştiğinden,
farklı bir Gemini modeline geçerseniz (`config.yaml → summarizer.gemini_model`)
o modelin ücretsiz günlük kotasını da göz önünde bulundurun.

## Telegram Bildirimi

**Bot artık "herkese açık bir haber kanalı" gibi çalışır:** botla konuşan
HERKES otomatik olarak abone olur ve önemli haberleri alır — tek bir kişiye
(sahibine) özel değildir. Bunu sağlayan iki parça:

- `src/telegram_bot.py`: `python-telegram-bot`'un `Application`/`Updater`
  yapısını kullanarak bota gelen mesajları **sürekli dinler** (long polling).
  Bir kullanıcı bota `/start` yazdığında (veya doğrudan herhangi bir ilk mesaj
  gönderdiğinde), kullanıcı adıyla bir karşılama mesajı gönderilir ve
  `chat_id`'si `subscribers` tablosuna kaydedilir (zaten aboneyse tekrar
  eklenmez). `/stop` yazan bir kullanıcı abonelikten çıkarılır. Bu dinleyici,
  `worker.py`/`main.py` başlatıldığında RSS tarama zamanlayıcısından bağımsız,
  ayrı bir arka plan thread'inde çalışır — biri diğerini bloklamaz.
- `src/telegram_bot.py`, ayrıca `/turkiye`, `/abd`, `/avrupa`, `/asya` ve
  `/yardim` (`/help`) komutlarını da sağlar (bkz. aşağıdaki "Bölge Bazlı Haber
  Filtreleme" bölümü). Bu komutlar, önem skoru eşiğini geçen haberlerin
  otomatik gönderildiği akıştan **tamamen bağımsız**, kullanıcının isteği
  üzerine çalışan ek bir sorgu katmanıdır — otomatik bildirim davranışını
  değiştirmez.
- `src/notifier.py`: **SADECE** `importance_score >= importance.threshold`
  olan haberler için, `subscribers` tablosundaki **TÜM** abonelere bildirim
  gönderir (tek bir sabit chat_id'ye değil). Eşiğin altındaki tüm haberler
  yalnızca CLI/Markdown çıktısında ve web dashboard'da görünür, bildirim
  gitmez. Aynı haber (aynı `group_key`) için birden fazla bildirim gitmez —
  veritabanındaki `notified` bayrağı bunu garanti eder. Gönderim sırasında
  biri botu engellemişse ("Forbidden: bot was blocked by the user") o kişi
  otomatik olarak `subscribers` tablosundan çıkarılır, diğer abonelere
  gönderim etkilenmez; abone sayısı arttıkça Telegram'ın rate limitine
  takılmamak için mesajlar arasına küçük bir bekleme konur.

### Bot Oluşturma (BotFather ile)

1. Telegram'da **@BotFather** ile bir sohbet başlatın.
2. `/newbot` komutunu gönderin, botunuza bir isim ve kullanıcı adı verin
   (kullanıcı adı `_bot` ile bitmeli, ör. `finanshaberbot`).
3. BotFather size bir **token** verecek (`123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
   formatında). `python main.py`'yi çalıştırıp istendiğinde bu token'ı girin
   — `getMe()` ile otomatik doğrulanıp `.env`'e kaydedilir. Elle eklemek
   isterseniz `.env` dosyanıza `TELEGRAM_BOT_TOKEN` olarak yazın.

### Abone Olma (artık `.env` düzenlemeye gerek yok)

Kurulumu yapan kişinin `.env` dosyasındaki `TELEGRAM_CHAT_ID` (varsa),
uygulama ilk açıldığında `subscribers` tablosuna otomatik olarak ilk abone
kaydı olarak eklenir — yani kurulumu yapan kişi otomatik abone olmuş olur.
**Ama bundan sonra yeni bir aboneliğin `.env` ile hiçbir ilgisi yoktur:**
botun kullanıcı adını bilen herhangi biri, Telegram'da o botla bir sohbet
başlatıp `/start` yazarak kendi kendine abone olabilir ve o andan itibaren
önem skoru eşiği geçen haberleri almaya başlar. Abonelikten çıkmak için
`/stop` yazması yeterlidir.

### Doğrulama ve Devre Dışı Bırakma

`TELEGRAM_BOT_TOKEN` tanımlı değilse (veya `config_setup.py`'nin sorduğu anda
boş bırakılır/geçersiz çıkarsa) bot dinleyicisi başlatılmaz ve `notifier.py`
bunu loglayıp bildirim adımını **sessizce atlar** — program çökmez, diğer her
şey (çekim, özetleme, veritabanı, dashboard) normal çalışmaya devam eder. Hiç
abone yoksa (subscribers tablosu boşsa) bildirim de aynı şekilde sessizce
atlanır. `config.yaml → telegram.enabled: false` yaparak hem bot dinleyicisini
hem de bildirimleri, kimlik bilgileri girilmiş olsa bile tamamen
kapatabilirsiniz.

### ⚠ Güvenlik Notu: Bot Token'ını Kimseyle Paylaşmayın

Bot artık kendisiyle konuşan herkesi otomatik abone yaptığından, **botun
kendisi (kullanıcı adı) herkese açık olabilir**, ama `TELEGRAM_BOT_TOKEN`
**asla paylaşılmamalıdır** (`.env` dosyası zaten `.gitignore` ile repoya
commit'lenmekten hariç tutulur). Token'ı ele geçiren herhangi biri:

- botunuzu tamamen ele geçirip kendi mesajlarını/spam'ini tüm abonelerinize
  gönderebilir,
- bota gelen tüm mesajları (`getUpdates`) okuyabilir,
- botu kendi amaçları için kullanabilir veya tamamen değiştirebilir.

Token sızarsa BotFather'da `/revoke` ile hemen iptal edip yeni bir token
üretin ve `.env` dosyasını güncelleyin.

## Bölge Bazlı Haber Filtreleme

Otomatik bildirim akışına (yalnızca eşiği geçen haberler) ek olarak,
kullanıcılar isterlerse belirli bir bölgedeki **tüm** haberleri (önem
skorundan bağımsız) sorgulayabilir:

- **Etiketleme** (`src/summarizer.py`): her haber özetlenirken, AYNI LLM
  çağrısında (ekstra çağrı/maliyet yok) `regions` alanı da üretilir —
  `"turkiye"`, `"abd"`, `"avrupa"`, `"asya"`, `"diger"` kategorilerinden bir
  veya birden fazlası. Karar **haberin içeriğine** göre verilir, kaynağına
  göre değil (ör. CNBC-e'de yayınlanan bir Fed haberi `"abd"` olarak
  etiketlenir, `"turkiye"` değil). Sonuç, `news_records.regions` kolonuna
  JSON listesi olarak kaydedilir (bkz. `src/db.py > NewsRecord.regions_list`).
  Bu özellik eklenmeden ÖNCE kaydedilmiş haberlerin `regions` değeri boştur
  (yeniden özetlenmedikleri sürece hiçbir bölge komutunda görünmezler).
- **Telegram komutları** (`src/telegram_bot.py`): `/turkiye`, `/abd`,
  `/avrupa`, `/asya` — komutu gönderen kullanıcıya, son 24 saat içinde o
  bölgeye etiketlenmiş TÜM haberleri (başlık, özet, kaynak, link) tek veya
  (Telegram'ın ~4096 karakter mesaj limiti aşılırsa) birden fazla mesaj
  halinde listeler. O bölgede hiç haber yoksa "Bugün bu bölgeden haber
  bulunamadı" mesajı döner. `/yardim` (veya `/help`) tüm komutları özetler.
  (Not: Telegram bot komutları yalnızca `a-z0-9_` içerebilir, bu yüzden
  `/yardım` değil `/yardim` kullanılır.)
- Bu komutlar `importance.threshold` eşiğinden **bağımsızdır** — düşük önem
  skorlu haberler de (hatta hiç skorlanamamışlar da) bölge sorgusunda
  görünür; sadece otomatik/anlık bildirim akışına girmezler.

## Günlük Özet Raporu

Mevcut ANLIK bildirim akışına (önem skoru eşiğini geçen haberler özetlenir
özetlenmez tek tek gönderilir) ek olarak, `worker.py` içinde ayrı bir
zamanlanmış görev (`gunluk_ozet`) çalışır: **hafta içi (Pazartesi-Cuma), her
gün İstanbul saatiyle 09:00'da**, bir önceki 24 saatte toplanan TÜM
haberler arasından Gemini/Claude'a (mevcut özetleme çağrısına EK, tek bir
seçim çağrısıyla) "gerçekten piyasa/yatırımcı için değerli, aksiyon
alınabilir" 5-10 haberi seçtirir (bkz. `src/summarizer.py > select_daily_highlights`,
`src/daily_digest.py`) ve `subscribers` tablosundaki TÜM abonelere
`"☀️ Günaydın! Dünden bugüne önemli N haber:"` başlığıyla gönderir. O gün
5'ten az önemli haber varsa olanı olduğu gibi gönderir, "Bugün az sayıda
önemli haber vardı" notu düşer. LLM seçimi herhangi bir sebeple başarısız
olursa (API anahtarı yok, çağrı hatası vb.) sessizce önem skoruna göre ilk
10 habere geri döner - günlük özet hiçbir durumda uygulamayı durdurmaz.

Zamanlama `worker.py` başındaki `DAILY_DIGEST_*` sabitleriyle ayarlanır
(varsayılan: `day_of_week="mon-fri"`, `hour=9`, `minute=0`,
`timezone="Europe/Istanbul"`).

> **Önemli:** Günlük özetin gönderilebilmesi için uygulamanın
> (`python main.py`) sabah 09:00'da **çalışıyor olması** gerekir (yerel/
> ücretsiz çalıştığımız için sunucu yok — bilgisayar kapalıyken veya
> `python main.py` çalışmıyorken tetiklenen zamanlama kaçırılır ve o güne ait
> özet gönderilmez, bir sonraki güne "telafi" olarak da taşınmaz).

## Web Dashboard

`http://localhost:8000` (veya `config.yaml → web.host`/`web.port` neyse) — tüm
haberleri veritabanından okuyup en yeniden en eskiye doğru listeler:

- Her kartta **kaynak(lar)**, yayın tarihi, önem skoru rozeti (5 = kırmızı,
  4 = turuncu, 3 = sarı, 1-2 = gri, bilinmiyor = açık gri) gösterilir.
- Önem skoru eşiği (`importance.threshold`) geçen haberler kartın sol
  kenarında kırmızı bir çizgiyle **görsel olarak öne çıkarılır**.
- Üstteki açılır menüden **kaynağa göre filtreleme** yapılabilir.
- Sayfa `config.yaml → web.refresh_seconds` (varsayılan 180 saniye) aralıkla
  otomatik yenilenir (`<meta http-equiv="refresh">` — ekstra JS/websocket
  gerektirmez).
- `/health` uç noktası basit bir sağlık kontrolü döner.

## Veritabanı

`src/db.py`, SQLAlchemy ile bir SQLite dosyasına (`config.yaml → database.path`,
varsayılan `data/finans_haber.db`) yazar. İki tablo:

- `news_records` — her satır bir haber **grubunu** (tek veya çok kaynaklı)
  temsil eder ve şunları tutar: başlık, kaynaklar, linkler (JSON), yayın
  tarihi, özet, önemli noktalar (JSON), önem skoru + gerekçe, bölge
  etiket(ler)i (JSON, ör. `["turkiye"]` — bkz. Bölge Bazlı Haber Filtreleme),
  `notified` bayrağı + zamanı, ilk/son görülme zamanı.
- `subscribers` — botla konuşup abone olmuş her Telegram kullanıcısı için bir
  satır: `chat_id` (UNIQUE), `username`, `first_name`, `subscribed_at`. Bota
  `/start` yazan herkes buraya eklenir, `/stop` yazan silinir (bkz. `src/telegram_bot.py`
  ve README > Telegram Bildirimi).

Worker (arka plan thread'i), Telegram bot dinleyicisi (ayrı bir arka plan
thread'i) ve web sunucusu (istek thread'leri) **aynı SQLite dosyasını
paylaşır** (`check_same_thread=False`); bu ölçekte (birkaç dakikada bir yazma,
ara sıra okuma) bu yeterlidir, ayrı bir veritabanı sunucusuna gerek yoktur.

## Loglama

Tüm çalıştırmalar `data/logs/finans_haber.log` dosyasına (dönen/rotating, 5 yedek,
2MB) ve konsola loglanır. Her kaynak/adım için ayrı try/except olduğundan tek bir
hata tüm süreci durdurmaz; log dosyasında hangi kaynağın neden başarısız olduğunu
görebilirsiniz.

## Bilinen Kısıtlamalar / Sonraki Adımlar

- **Tekilleştirme** şu an başlık benzerliğine (karakter bazlı) dayanıyor; anlam
  bazlı (embedding) bir yaklaşım daha isabetli gruplama sağlayabilir.
- **`group_key` kararlılığı:** Veritabanı kimliği (aynı haberi tekrar
  özetlememek/bildirmemek için), grubun temsilci başlığının normalize edilmiş
  hash'ine dayanır. Aynı olay farklı kaynaklarda belirgin şekilde farklı
  başlıklarla yazılırsa, ya da dedup grubunun "temsilcisi" çalıştırmalar
  arasında değişirse, nadiren aynı hikaye için ikinci bir kayıt oluşabilir
  (yani teorik olarak nadir bir durumda aynı habere iki bildirim gidebilir).
  Daha sağlam bir çözüm (ör. linke dayalı kimlik + embedding benzerliği)
  gelecekte eklenebilir.
- **Reuters/Bloomberg** lisanslı bir yol (NewsAPI.ai) üzerinden dahil; ücretsiz
  kotanın üzerine çıkmak isterseniz `config.yaml`'daki `monthly_call_quota` /
  `min_interval_minutes` değerlerini ücretli bir plana göre gevşetebilirsiniz.
- **SQLite eşzamanlılığı:** Mevcut ölçek (birkaç dakikada bir yazma, ara sıra
  dashboard okuması) için yeterli; çok daha yüksek trafik/çoklu worker senaryosu
  için PostgreSQL gibi bir sunucu tabanlı veritabanına geçiş gerekebilir.
- Varsayılan sağlayıcı Gemini, model `gemini-3.5-flash-lite` (`config.yaml` →
  `summarizer.gemini_model`). Anthropic seçilirse model `claude-sonnet-4-6`
  olarak sabit (kullanıcı talebiyle); daha yeni `claude-sonnet-5` modeli de
  mevcuttur, isterseniz `config.yaml` → `summarizer.anthropic_model` alanından
  değiştirebilirsiniz. Google modelleri zaman zaman yeni kullanıcılara
  kapatılabiliyor ("no longer available to new users") — böyle bir hatayla
  karşılaşırsanız `gemini_model` değerini Google AI Studio dokümantasyonundan
  (ai.google.dev/gemini-api/docs/models) güncel bir modelle değiştirin.
- Dashboard'da şu an manuel bir "bildirimi tekrar gönder" veya haberi
  arşivleme/silme aksiyonu yok — salt-okunur bir görünüm.

## Opsiyonel: JavaScript render (Playwright)

`config.yaml` içinde bir kaynağa `render_js: true` verirseniz:

```powershell
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe -m playwright install chromium
```

kurulumunu yapmanız gerekir. Playwright kurulu değilse kod hatayla çökmez, sadece
o kaynağı atlar ve nedenini loglar.
