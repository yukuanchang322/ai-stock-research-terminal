const CACHE="ai-stock-v5.4.8";
const SHELL = ['/', '/styles.css', '/app.js', '/recovery.js', '/v547_hotfix.js', '/static/manifest.webmanifest'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).catch(() => caches.match('/')));
    return;
  }
  if (['/app.js','/recovery.js','/v547_hotfix.js','/styles.css','/sw.js'].includes(url.pathname)) {
    event.respondWith(fetch(event.request).then(resp => {
      const copy=resp.clone(); caches.open(CACHE).then(cache=>cache.put(event.request,copy)); return resp;
    }).catch(()=>caches.match(event.request)));
    return;
  }
  event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request).then(resp => {
    const copy = resp.clone();
    caches.open(CACHE).then(cache => cache.put(event.request, copy));
    return resp;
  })));
});

// V5.4.8 Valuation Recovery, retaining official data fallback and Data Recovery.
// API responses are never stored in Service Worker Cache. Missing optional icons are
// intentionally excluded from the atomic shell install so a 404 cannot break SW activation.
