const CACHE="ai-stock-v5.5.0";
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
    event.respondWith(fetch(event.request,{cache:'no-store'}).catch(() => caches.match('/')));
    return;
  }
  if (['/app.js','/recovery.js','/v547_hotfix.js','/styles.css','/sw.js'].includes(url.pathname)) {
    event.respondWith(fetch(event.request,{cache:'no-store'}).then(resp => {
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

// V5.5.0 Price Integrity + UI hardening.
// API responses are never cached. Core JS/CSS use network-first + no-store so iPhone PWA
// cannot stay pinned to an older chart/layout implementation after a deployment.
