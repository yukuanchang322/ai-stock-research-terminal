const CACHE="ai-stock-v5.10.1";
const SHELL=[
  '/static/styles.css?v=5.10.1',
  '/static/app.js?v=5.10.1',
  '/static/v5101_hotfix.js?v=5.10.1',
  '/static/manifest.webmanifest?v=5.10.1'
];

self.addEventListener('install',event=>{
  event.waitUntil(
    caches.open(CACHE)
      .then(cache=>Promise.allSettled(SHELL.map(url=>cache.add(url))))
      .then(()=>self.skipWaiting())
  );
});

self.addEventListener('activate',event=>{
  event.waitUntil(
    caches.keys()
      .then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k))))
      .then(()=>self.clients.claim())
  );
});

self.addEventListener('fetch',event=>{
  const url=new URL(event.request.url);
  if(event.request.method!=='GET'||url.pathname.startsWith('/api/')||url.pathname==='/health') return;

  if(event.request.mode==='navigate'||url.pathname==='/'){
    event.respondWith(fetch(event.request,{cache:'no-store'}));
    return;
  }

  if(url.pathname.startsWith('/static/')||url.pathname==='/sw.js'){
    event.respondWith(
      fetch(event.request,{cache:'reload'})
        .then(resp=>{
          if(resp.ok){const copy=resp.clone();caches.open(CACHE).then(cache=>cache.put(event.request,copy));}
          return resp;
        })
        .catch(()=>caches.match(event.request))
    );
    return;
  }

  event.respondWith(fetch(event.request).catch(()=>caches.match(event.request)));
});
