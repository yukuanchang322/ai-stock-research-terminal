const CACHE='ai-stock-v5.11.0';
const SHELL=['/static/styles.css?v=5.11.0','/static/app.js?v=5.11.0','/static/manifest.webmanifest?v=5.11.0'];
self.addEventListener('install',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.map(k=>caches.delete(k)))).then(()=>caches.open(CACHE)).then(c=>Promise.allSettled(SHELL.map(u=>c.add(u)))).then(()=>self.skipWaiting())));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',event=>{
 const u=new URL(event.request.url);
 if(event.request.method!=='GET'||u.pathname.startsWith('/api/')||u.pathname==='/health') return;
 if(event.request.mode==='navigate'||u.pathname==='/'){event.respondWith(fetch(event.request,{cache:'no-store'}));return;}
 if(u.pathname.startsWith('/static/')||u.pathname==='/sw.js') event.respondWith(fetch(event.request,{cache:'reload'}).catch(()=>caches.match(event.request)));
});
