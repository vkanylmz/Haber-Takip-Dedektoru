# Genel API (`/api/v1/*`)

Bu proje, topladığı/özetlediği finans haberlerini ve piyasa verisini
**başka projelerden kullanılabilecek** genel bir REST API olarak da sunar.
Bu API, dashboard'un kendi iç kullandığı `/api/*` rotalarından **tamamen
bağımsızdır** — dashboard'u etkilemeden serbestçe kullanılabilir.

**Canlı adres:** `https://finans-haber-dashboard.onrender.com`

İnteraktif dokümantasyon (Swagger UI) için: `/docs`
Ham OpenAPI şeması için: `/openapi.json`

## Kimlik Doğrulama

Her istekte `X-API-Key` header'ı gönderilmelidir:

```
X-API-Key: <api-anahtarınız>
```

- Header eksikse veya anahtar geçersiz/devre dışıysa: `401 Unauthorized`
- Rate-limit aşıldıysa: `429 Too Many Requests`

Yeni bir anahtar almak için proje sahibiyle iletişime geçin (şu an
self-servis bir anahtar üretme endpoint'i yoktur — kötüye kullanımı
önlemek için kasıtlı olarak eklenmedi).

## Rate Limit

| Tier | Dakikada | Günde |
|---|---|---|
| `standard` | 60 istek | 5.000 istek |
| `admin` (proje sahibinin kendi kullanımı) | 1.000 istek | 200.000 istek |

Limit aşıldığında `429` yanıtı, `detail` alanında hangi limitin (dakikalık/
günlük) aşıldığını açıklayan bir mesajla birlikte döner.

## Hata Kodları

| Kod | Anlamı | `detail` mesajı örneği |
|---|---|---|
| `401 Unauthorized` | `X-API-Key` header'ı hiç gönderilmedi | `"X-API-Key header eksik."` |
| `401 Unauthorized` | Gönderilen anahtar geçersiz/devre dışı | `"Geçersiz veya devre dışı API anahtarı."` |
| `404 Not Found` | `/api/v1/news/{id}` için verilen id bulunamadı | `"Haber bulunamadı."` |
| `429 Too Many Requests` | Dakikalık limit aşıldı | `"Dakikalık istek limiti (60) aşıldı. Lütfen bir dakika sonra tekrar deneyin."` |
| `429 Too Many Requests` | Günlük limit aşıldı | `"Günlük istek limiti (5000) aşıldı."` |
| `422 Unprocessable Entity` | Bir query/path parametresi yanlış tipte/aralık dışında (ör. `min_importance=9`) | FastAPI'nin standart doğrulama hata gövdesi |

Tüm hata yanıtları `{"detail": "..."}` şeklinde JSON döner (FastAPI'nin
standart hata formatı).

## Genel Notlar

- Tüm yanıtlar JSON formatındadır.
- Tarihler ISO 8601 formatındadır (ör. `2026-07-29T14:28:37.223753+00:00`).
- Bu API **hiçbir zaman** abone listesi, Telegram chat_id'si veya API
  anahtarlarının kendisi gibi hassas bilgi döndürmez — sadece haber/piyasa
  verisi paylaşılır.
- `/api/v1/market-data`, `/api/v1/commodities` gibi uç noktalar mevcut
  arka plan önbelleklerinden okur — bu uç noktaları çağırmak EK bir Yahoo
  Finance/LLM isteği tetiklemez.

---

## `GET /api/v1/news`

Haberleri filtrelenebilir/sayfalanabilir şekilde listeler.

**Query parametreleri (hepsi opsiyonel):**

| Parametre | Tip | Açıklama |
|---|---|---|
| `source` | string | Kaynak adı (ör. `Bloomberg HT`) |
| `sector` | string | Sektör slug'ı: `finans`, `enerji`, `teknoloji`, `otomotiv`, `perakende`, `saglik`, `savunma`, `gayrimenkul`, `tarim`, `diger` |
| `region` | string | Bölge slug'ı: `turkiye`, `abd`, `avrupa`, `asya`, `diger` |
| `sentiment` | string | `pozitif`, `negatif`, `notr` |
| `min_importance` | int (1-5) | En az bu önem skoruna sahip haberler |
| `date_from` | ISO 8601 datetime | Bu tarihten SONRAKİ haberler |
| `date_to` | ISO 8601 datetime | Bu tarihten ÖNCEKİ haberler |
| `page` | int (varsayılan 1) | Sayfa numarası |
| `page_size` | int (varsayılan 20, en fazla 100) | Sayfa başına kayıt |

**Örnek istek:**

```bash
curl -H "X-API-Key: <anahtar>" \
  "https://finans-haber-dashboard.onrender.com/api/v1/news?sector=finans&min_importance=4&page=1&page_size=10"
```

**Örnek yanıt:**

```json
{
  "page": 1,
  "page_size": 10,
  "total": 2443,
  "has_next": true,
  "items": [
    {
      "id": "62bfbd97f795826d52d27390",
      "title": "Stock market today: Dow, S&P 500, Nasdaq slide...",
      "title_tr": "Borsalarda son durum: Fed kararı ve...",
      "summary": "Fed'in yaklaşan faiz kararı öncesinde...",
      "key_points": ["..."],
      "importance_score": 4,
      "importance_reason": "...",
      "sources": ["Yahoo Finance"],
      "sector": ["finans", "teknoloji"],
      "regions": ["abd"],
      "sentiment": "negatif",
      "market_impact": "...",
      "top_category": "makro",
      "company_ticker": null,
      "published_at": "2026-07-29T08:20:09+00:00",
      "first_seen_at": "2026-07-29T14:28:24.991339+00:00",
      "links": [{"source": "Yahoo Finance", "link": "https://..."}]
    }
  ]
}
```

## `GET /api/v1/news/{id}`

Tek bir haberin tam detayını döner. `{id}`, `/api/v1/news` listesindeki
`id` alanıdır (dahili olarak kararlı bir grup anahtarıdır).

- Bulunamazsa: `404 Not Found`

**Örnek:**

```bash
curl -H "X-API-Key: <anahtar>" \
  "https://finans-haber-dashboard.onrender.com/api/v1/news/62bfbd97f795826d52d27390"
```

## `GET /api/v1/companies/{ticker}`

Bir şirketin güncel fiyatı, 1 aylık fiyat geçmişi ve son 30 günün ilgili
haberlerini döner.

**Path parametresi:** `ticker` — borsa sembolü (ör. `ADM`, `AAPL`, `THYAO`)

**Query parametresi:** `exchange` (opsiyonel, varsayılan `NASDAQ`) — `NYSE`,
`NASDAQ`, `BIST`, `LSE` gibi bir borsa kodu.

**Örnek:**

```bash
curl -H "X-API-Key: <anahtar>" \
  "https://finans-haber-dashboard.onrender.com/api/v1/companies/ADM?exchange=NYSE"
```

**Örnek yanıt:**

```json
{
  "ticker": "NYSE: ADM",
  "quote": {
    "symbol": "ADM",
    "price": 82.375,
    "change_pct": -0.97,
    "currency": "USD",
    "is_open": true,
    "is_delayed": false,
    "delay_minutes": 0
  },
  "history": [{"timestamp": 1782739800, "close": 76.87}],
  "news": [
    {
      "title": "...",
      "title_tr": null,
      "summary": "...",
      "sources": "Yahoo Finance",
      "published_at": "2026-07-29T...",
      "link": "https://..."
    }
  ]
}
```

Fiyat verisi geçici olarak alınamazsa (ör. veri kaynağı o an aşırı
yoğunsa) `quote` alanı `null` döner — hata fırlatılmaz.

## `GET /api/v1/market-data`

Güncel piyasa şeridi verisini (döviz kurları, altın/petrol, büyük
endeksler) döner. Sabit bir semboller listesi olduğundan filtreleme
parametresi yoktur.

```bash
curl -H "X-API-Key: <anahtar>" \
  "https://finans-haber-dashboard.onrender.com/api/v1/market-data"
```

## `GET /api/v1/commodities`

Haftalık emtia raporu verisini döner: her izlenen emtia için fiyat,
haftalık değişim, 1 aylık fiyat geçmişi, LLM tarafından üretilmiş kısa bir
sektör etki analizi ve ilgili şirketlerin listesi.

```bash
curl -H "X-API-Key: <anahtar>" \
  "https://finans-haber-dashboard.onrender.com/api/v1/commodities"
```

## `GET /api/v1/sectors/heatmap`

Son 24 saatteki haber yoğunluğuna göre sektör bazlı bir "ısı haritası"
döner (her sektör için haber sayısı, ortalama önem skoru, duygu dağılımı).

```bash
curl -H "X-API-Key: <anahtar>" \
  "https://finans-haber-dashboard.onrender.com/api/v1/sectors/heatmap"
```
