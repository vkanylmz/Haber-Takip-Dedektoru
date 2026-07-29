// Finansal Haber Dashboard - Web Push Service Worker
//
// Bu dosya /sw.js yolundan servis edilir (bkz. src/web/app.py > service_worker_file)
// - KASITLI OLARAK kök yoldan, çünkü bir Service Worker'ın varsayılan
// "scope"u kendi URL'inin bulunduğu dizindir (ör. /static/sw.js servis
// edilseydi scope /static/ ile sınırlı kalırdı, /'daki dashboard'u
// KAPSAMAZDI). Tarayıcı sekmesi/uygulama TAMAMEN KAPALIYKEN bile push
// mesajlarını dinleyip bildirim gösterebilmesi, Service Worker'ların
// (normal sayfa script'lerinin aksine) tarayıcı tarafından ayrı bir
// arka plan sürecinde çalıştırılabilmesinden kaynaklanır.

self.addEventListener('install', function (event) {
  // Yeni bir sw.js sürümü yayınlandığında ESKİ sürümün kapanmasını
  // BEKLEMEDEN hemen aktif ol - kişisel/tek kullanıcılı bir dashboard için
  // "eski sekmeler tam kapanana kadar bekle" temkinli davranışının hiçbir
  // faydası yok, sadece güncellemeyi geciktirir.
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  // Zaten AÇIK olan sekmeleri de (sayfa yeniden yüklenmeden) hemen bu yeni
  // Service Worker'ın kontrolüne al.
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', function (event) {
  // Sunucudan (bkz. src/web_push.py > send_push_notification) gelen veri
  // JSON olarak gönderiliyor: {title, body, url, tag}. Ayrıştırma
  // başarısız olursa (ör. gelecekte format değişirse) sessizce genel bir
  // bildirime düşülür - push olayı ASLA hatasız/bildirimsiz bırakılmaz
  // (bazı tarayıcılar bunu "sessiz push" kabul edip izni otomatik iptal
  // edebilir).
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'Finansal Haber Dashboard', body: event.data ? event.data.text() : 'Yeni bir haber var.' };
  }

  const title = data.title || 'Finansal Haber Dashboard';
  const options = {
    body: data.body || '',
    // Aynı `tag` ile gelen ardışık bildirimler ÜST ÜSTE YIĞILMAZ, birbirinin
    // yerine geçer - kısa sürede çok sayıda eşleşen haber gelirse kullanıcı
    // bildirim listesinin şişmesi yerine hep EN SON haberi görür.
    tag: data.tag || 'finans-haber',
    data: { url: data.url || '/' },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (clientList) {
      // Dashboard zaten açık bir sekmede varsa, YENİ bir sekme açmak yerine
      // o sekmeyi öne getir (daha az sekme kalabalığı).
      for (const client of clientList) {
        if (client.url.indexOf(targetUrl) !== -1 && 'focus' in client) {
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
    })
  );
});
