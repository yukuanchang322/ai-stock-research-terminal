const CACHE_PREFIX = 'ai-stock-shell-';
const CACHE = `${CACHE_PREFIX}20260815-1`;
const APP_CACHE_PREFIX = 'ai-stock-';
const SHELL = [
  '/',
  '/styles.css?v=20260815-1',
  '/app.js?v=20260815-1',
  '/static/manifest.webmanifest?v=20260815-1',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key.startsWith(APP_CACHE_PREFIX) && key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

async function networkFirst(request, fallback) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request, {cache: 'no-store'});
    if (response.ok && response.type === 'basic') await cache.put(request, response.clone());
    return response;
  } catch (error) {
    return (await cache.match(request)) || (fallback && await cache.match(fallback)) || Response.error();
  }
}

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  if (event.request.mode === 'navigate') {
    event.respondWith(networkFirst(event.request, '/'));
    return;
  }
  event.respondWith(networkFirst(event.request));
});
