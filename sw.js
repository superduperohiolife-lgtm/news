/* 統合ニュース Service Worker
   - HTML(ドキュメント)はネットワーク優先→失敗時キャッシュ(オフライン閲覧)
   - アイコン等の静的資産はキャッシュ優先 */
const CACHE = 'news-cache-v1';
const ASSETS = [
  './index.html',
  './manifest.webmanifest',
  './icon-192.png',
  './icon-512.png',
  './apple-touch-icon.png'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  const isDoc = req.mode === 'navigate' ||
                url.pathname.endsWith('/') ||
                url.pathname.endsWith('index.html');
  if (isDoc) {
    // ネットワーク優先(最新ニュース取得)、失敗時はキャッシュ
    e.respondWith(
      fetch(req)
        .then((res) => {
          const cp = res.clone();
          caches.open(CACHE).then((c) => c.put('./index.html', cp));
          return res;
        })
        .catch(() => caches.match('./index.html'))
    );
  } else {
    // キャッシュ優先
    e.respondWith(
      caches.match(req).then((r) =>
        r || fetch(req).then((res) => {
          const cp = res.clone();
          caches.open(CACHE).then((c) => c.put(req, cp));
          return res;
        }).catch(() => r)
      )
    );
  }
});
