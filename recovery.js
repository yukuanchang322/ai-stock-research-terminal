// AI Stock Research Terminal V5.4.5 — Data Recovery
// Transparent network fallback: live API remains authoritative; a device backup is
// returned only when /api/stock/{ticker} fails or returns an invalid research payload.
(() => {
  'use strict';
  const VERSION='5.4.5';
  const DB_NAME='ai-stock-research-terminal';
  const STORE='research-recovery';
  const LOCAL_PREFIX='ai-stock-v5.4.5-recovery-';
  const MAX_LOCAL_ENTRIES=12;
  const nativeFetch=window.fetch.bind(window);

  const clone=v=>JSON.parse(JSON.stringify(v));
  const validReport=d=>!!(d&&typeof d==='object'&&d.ticker&&d.price!=null&&d.generated_at);
  const key=t=>`${LOCAL_PREFIX}${String(t||'').toUpperCase()}`;
  const stockRequest=url=>{
    try{
      const u=new URL(typeof url==='string'?url:url.url,location.origin);
      const m=u.pathname.match(/^\/api\/stock\/([0-9A-Z.-]{2,12})$/i);
      return m?decodeURIComponent(m[1]).toUpperCase():null;
    }catch(_){return null;}
  };

  function openDb(){
    return new Promise((resolve,reject)=>{
      if(!('indexedDB' in window)) return reject(new Error('indexedDB unavailable'));
      const req=indexedDB.open(DB_NAME,1);
      req.onupgradeneeded=()=>{if(!req.result.objectStoreNames.contains(STORE))req.result.createObjectStore(STORE,{keyPath:'ticker'});};
      req.onsuccess=()=>resolve(req.result);
      req.onerror=()=>reject(req.error||new Error('indexedDB open failed'));
    });
  }
  async function idbPut(record){
    const db=await openDb();
    return new Promise((resolve,reject)=>{const tx=db.transaction(STORE,'readwrite');tx.objectStore(STORE).put(record);tx.oncomplete=()=>{db.close();resolve();};tx.onerror=()=>{const e=tx.error;db.close();reject(e);};});
  }
  async function idbGet(ticker){
    const db=await openDb();
    return new Promise((resolve,reject)=>{const tx=db.transaction(STORE,'readonly');const req=tx.objectStore(STORE).get(ticker);req.onsuccess=()=>resolve(req.result||null);req.onerror=()=>reject(req.error);tx.oncomplete=()=>db.close();});
  }
  function pruneLocal(){
    try{
      const rows=[];
      for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);if(!k?.startsWith(LOCAL_PREFIX))continue;try{const r=JSON.parse(localStorage.getItem(k));rows.push([k,r?.saved_at||'']);}catch(_){}}
      rows.sort((a,b)=>String(b[1]).localeCompare(String(a[1])));rows.slice(MAX_LOCAL_ENTRIES).forEach(([k])=>localStorage.removeItem(k));
    }catch(_){ }
  }
  async function save(d){
    if(!validReport(d)||d?.recovery?.active)return;
    const record={ticker:String(d.ticker).toUpperCase(),saved_at:new Date().toISOString(),version:VERSION,data:clone(d)};
    try{await idbPut(record);}catch(_){try{localStorage.setItem(key(record.ticker),JSON.stringify(record));pruneLocal();}catch(__){}}
  }
  async function read(ticker){
    ticker=String(ticker||'').toUpperCase();let record=null;
    try{record=await idbGet(ticker);}catch(_){ }
    if(!record){try{record=JSON.parse(localStorage.getItem(key(ticker))||'null');}catch(_){record=null;}}
    return record&&validReport(record.data)?record:null;
  }
  function ensureBanner(){
    let el=document.getElementById('dataRecoveryBanner');if(el)return el;
    el=document.createElement('div');el.id='dataRecoveryBanner';el.hidden=true;el.setAttribute('role','status');
    el.style.cssText='margin:12px 0 18px;padding:13px 14px;border:1px solid #d6a94f;border-radius:12px;background:#fff4d6;color:#5f4308;font-size:14px;line-height:1.55;';
    const report=document.getElementById('report');if(report)report.insertBefore(el,report.firstChild);return el;
  }
  function clearBanner(){const el=ensureBanner();el.hidden=true;el.textContent='';document.documentElement.removeAttribute('data-recovery-mode');}
  function showBanner(record,reason){
    const el=ensureBanner(),d=record.data;
    const reportTime=d.generated_at?new Date(d.generated_at).toLocaleString('zh-TW'):'未知';
    const savedTime=record.saved_at?new Date(record.saved_at).toLocaleString('zh-TW'):'未知';
    el.innerHTML=`<b>Data Recovery · 本機備份</b><br>即時資料目前無法完整取得，已顯示此裝置最後一次成功報告。原報告時間：${reportTime}；備份時間：${savedTime}。<br><small>這不是即時資料，不會冒充最新資料。${reason?` · ${String(reason).slice(0,100)}`:''}</small>`;
    el.hidden=false;document.documentElement.setAttribute('data-recovery-mode','true');
  }
  async function responseJsonCopy(response){try{return await response.clone().json();}catch(_){return null;}}

  window.fetch=async function(input,init){
    const ticker=stockRequest(input);
    if(!ticker)return nativeFetch(input,init);
    try{
      const response=await nativeFetch(input,init);
      const ctype=(response.headers.get('content-type')||'').toLowerCase();
      const data=ctype.includes('application/json')?await responseJsonCopy(response):null;
      if(response.ok&&validReport(data)){
        clearBanner();
        save(data).catch(()=>{});
        return response;
      }
      const reason=(data&&((data.message)||(data.detail)))||`HTTP ${response.status}${ctype.includes('application/json')?'':' · 非 JSON'}`;
      const record=await read(ticker);
      if(!record)return response;
      const recovered=clone(record.data);
      recovered.recovery={active:true,version:VERSION,saved_at:record.saved_at,original_generated_at:recovered.generated_at,reason};
      showBanner(record,reason);
      return new Response(JSON.stringify(recovered),{status:200,headers:{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-AI-Stock-Recovery':'1'}});
    }catch(err){
      const record=await read(ticker);
      if(!record)throw err;
      const recovered=clone(record.data);
      const reason=String(err?.message||'network unavailable');
      recovered.recovery={active:true,version:VERSION,saved_at:record.saved_at,original_generated_at:recovered.generated_at,reason};
      showBanner(record,reason);
      return new Response(JSON.stringify(recovered),{status:200,headers:{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-AI-Stock-Recovery':'1'}});
    }
  };

  window.aiStockDataRecovery={version:VERSION,save,read};
})();
