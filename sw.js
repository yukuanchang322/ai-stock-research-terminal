// V5.11.2: retire service-worker caching entirely to stop stale app/runtime versions.
self.addEventListener('install',event=>event.waitUntil(self.skipWaiting()));
self.addEventListener('activate',event=>event.waitUntil((async()=>{
  try{for(const k of await caches.keys())await caches.delete(k);}catch(e){}
  try{await self.registration.unregister();}catch(e){}
  try{const clientsList=await self.clients.matchAll({type:'window',includeUncontrolled:true});for(const c of clientsList)c.postMessage({type:'AI_STOCK_SW_RETIRED',version:'5.11.2'});}catch(e){}
})()));
self.addEventListener('fetch',()=>{});
