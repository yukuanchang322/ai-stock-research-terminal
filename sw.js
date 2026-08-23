const CACHE="ai-stock-v5.10.0";
const SHELL = ['/styles.css?v=5.10.0', '/app.js?v=5.10.0', '/recovery.js?v=5.10.0', '/v547_hotfix.js?v=5.10.0', '/v5100_hotfix.js?v=5.10.0', '/static/manifest.webmanifest'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.pathname.startsWith('/api/') || url.pathname==='/health') return;
  if (event.request.mode === 'navigate' || url.pathname === '/') {
    event.respondWith(fetch(event.request,{cache:'no-store'}));
    return;
  }
  if (['/app.js','/recovery.js','/v547_hotfix.js','/v5100_hotfix.js','/styles.css','/sw.js'].includes(url.pathname)) {
    event.respondWith(fetch(event.request,{cache:'reload'}).then(resp => {
      const copy=resp.clone(); caches.open(CACHE).then(cache=>cache.put(event.request,copy)); return resp;
    }).catch(()=>caches.match(event.request)));
    return;
  }
  event.respondWith(fetch(event.request).catch(()=>caches.match(event.request)));
});
