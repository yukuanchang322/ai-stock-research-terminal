// AI Stock Research Terminal V5.4.5 — Data Recovery
// Keeps the last successful research result on this device and only restores it
// when the live API cannot produce a valid report. Recovered data is always marked stale.
(() => {
  'use strict';

  const VERSION = '5.4.5';
  const DB_NAME = 'ai-stock-research-terminal';
  const STORE = 'research-recovery';
  const LOCAL_PREFIX = 'ai-stock-v5.4.5-recovery-';
  const MAX_LOCAL_ENTRIES = 12;
  const originalLoadTicker = window.loadTicker;

  function recoveryKey(ticker){ return `${LOCAL_PREFIX}${String(ticker||'').toUpperCase()}`; }
  function clone(value){ return JSON.parse(JSON.stringify(value)); }
  function validReport(d){ return !!(d && typeof d === 'object' && d.ticker && d.price != null && d.generated_at); }

  function openDb(){
    return new Promise((resolve,reject) => {
      if(!('indexedDB' in window)) return reject(new Error('indexedDB unavailable'));
      const req = indexedDB.open(DB_NAME,1);
      req.onupgradeneeded = () => {
        const db=req.result;
        if(!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE,{keyPath:'ticker'});
      };
      req.onsuccess=()=>resolve(req.result);
      req.onerror=()=>reject(req.error||new Error('indexedDB open failed'));
    });
  }

  async function idbPut(record){
    const db=await openDb();
    return new Promise((resolve,reject)=>{
      const tx=db.transaction(STORE,'readwrite');
      tx.objectStore(STORE).put(record);
      tx.oncomplete=()=>{db.close();resolve();};
      tx.onerror=()=>{const e=tx.error;db.close();reject(e);};
    });
  }

  async function idbGet(ticker){
    const db=await openDb();
    return new Promise((resolve,reject)=>{
      const tx=db.transaction(STORE,'readonly');
      const req=tx.objectStore(STORE).get(ticker);
      req.onsuccess=()=>resolve(req.result||null);
      req.onerror=()=>reject(req.error);
      tx.oncomplete=()=>db.close();
    });
  }

  function pruneLocalStorage(){
    try{
      const entries=[];
      for(let i=0;i<localStorage.length;i++){
        const k=localStorage.key(i);
        if(!k?.startsWith(LOCAL_PREFIX)) continue;
        try{ const r=JSON.parse(localStorage.getItem(k)); entries.push([k,r?.saved_at||'']); }catch(_){ }
      }
      entries.sort((a,b)=>String(b[1]).localeCompare(String(a[1])));
      entries.slice(MAX_LOCAL_ENTRIES).forEach(([k])=>localStorage.removeItem(k));
    }catch(_){ }
  }

  async function saveRecovery(d){
    if(!validReport(d) || d?.recovery?.active) return;
    const ticker=String(d.ticker).toUpperCase();
    const record={ticker,saved_at:new Date().toISOString(),version:VERSION,data:clone(d)};
    try{ await idbPut(record); }
    catch(_){
      try{ localStorage.setItem(recoveryKey(ticker),JSON.stringify(record)); pruneLocalStorage(); }catch(__){ }
    }
  }

  async function readRecovery(ticker){
    ticker=String(ticker||'').toUpperCase();
    let record=null;
    try{ record=await idbGet(ticker); }catch(_){ }
    if(!record){
      try{ record=JSON.parse(localStorage.getItem(recoveryKey(ticker))||'null'); }catch(_){ record=null; }
    }
    return record && validReport(record.data) ? record : null;
  }

  function ensureBanner(){
    let el=document.getElementById('dataRecoveryBanner');
    if(el) return el;
    el=document.createElement('div');
    el.id='dataRecoveryBanner';
    el.hidden=true;
    el.setAttribute('role','status');
    el.style.cssText='margin:12px 0 18px;padding:13px 14px;border:1px solid #d6a94f;border-radius:12px;background:#fff4d6;color:#5f4308;font-size:14px;line-height:1.55;';
    const report=document.getElementById('report');
    if(report) report.insertBefore(el,report.firstChild);
    return el;
  }

  function clearRecoveryBanner(){
    const el=ensureBanner();
    el.hidden=true;
    el.textContent='';
    document.documentElement.removeAttribute('data-recovery-mode');
  }

  function showRecoveryBanner(record,reason){
    const d=record.data;
    const el=ensureBanner();
    const reportTime=d.generated_at ? new Date(d.generated_at).toLocaleString('zh-TW') : '未知';
    const savedTime=record.saved_at ? new Date(record.saved_at).toLocaleString('zh-TW') : '未知';
    el.innerHTML=`<b>Data Recovery · 本機備份</b><br>即時資料目前無法完整取得，已顯示此裝置最後一次成功報告。原報告時間：${reportTime}；備份時間：${savedTime}。<br><small>這不是即時資料，不會覆寫為最新資料。${reason?` · ${String(reason).slice(0,100)}`:''}</small>`;
    el.hidden=false;
    document.documentElement.setAttribute('data-recovery-mode','true');
  }

  async function fetchLive(ticker,force){
    const path=`/api/stock/${encodeURIComponent(ticker)}${force?'?refresh=true':''}`;
    const res=await fetch(path,{method:'GET',headers:{'Accept':'application/json'},cache:'no-store'});
    const ctype=(res.headers.get('content-type')||'').toLowerCase();
    if(!ctype.includes('application/json')) throw new Error(`HTTP ${res.status}：伺服器回傳非 JSON 內容`);
    let data=null;
    try{ data=await res.json(); }catch(_){ throw new Error('伺服器 JSON 格式異常'); }
    if(!res.ok) throw new Error(data?.message||data?.detail||`HTTP ${res.status}`);
    if(!validReport(data)) throw new Error('即時研究資料不完整');
    return data;
  }

  async function recoveryLoadTicker(ticker,force=false){
    ticker=String(ticker||'').trim().toUpperCase();
    const errorBox=document.getElementById('errorBox');
    const report=document.getElementById('report');
    const loading=document.getElementById('loading');
    const searchBtn=document.getElementById('searchBtn');
    const pdfBtn=document.getElementById('pdfBtn');
    const dockPdf=document.getElementById('dockPdf');
    const dockShare=document.getElementById('dockShare');

    errorBox?.classList.add('hidden');
    if(!/^[0-9A-Z.-]{2,12}$/.test(ticker)){
      report?.classList.add('hidden');
      if(errorBox){errorBox.textContent='股票代號格式不正確，請輸入例如 2330、3661。';errorBox.classList.remove('hidden');}
      return;
    }

    loading?.classList.remove('hidden');
    if(searchBtn) searchBtn.disabled=true;
    if(pdfBtn) pdfBtn.disabled=true;
    if(dockPdf) dockPdf.disabled=true;
    if(dockShare) dockShare.disabled=true;

    try{
      const live=await fetchLive(ticker,force);
      clearRecoveryBanner();
      window.render(live);
      await saveRecovery(live);
    }catch(err){
      const record=await readRecovery(ticker);
      if(record){
        const recovered=clone(record.data);
        recovered.recovery={active:true,version:VERSION,saved_at:record.saved_at,original_generated_at:recovered.generated_at,reason:String(err?.message||'live API unavailable')};
        window.render(recovered);
        showRecoveryBanner(record,err?.message);
        if(errorBox) errorBox.classList.add('hidden');
      }else{
        report?.classList.add('hidden');
        if(errorBox){
          errorBox.textContent=`${err?.message||'資料取得失敗'}；此裝置尚無可復原的成功報告。`;
          errorBox.classList.remove('hidden');
        }
      }
    }finally{
      loading?.classList.add('hidden');
      if(searchBtn) searchBtn.disabled=false;
    }
  }

  // Replace the global loader. Existing app.js event handlers resolve this binding at call time.
  window.loadTicker=recoveryLoadTicker;
  window.aiStockDataRecovery={version:VERSION,save:saveRecovery,read:readRecovery,load:recoveryLoadTicker,originalLoadTicker};

  // Rebind direct controls defensively in case a browser captured old function references.
  window.addEventListener('DOMContentLoaded',()=>{
    const input=document.getElementById('tickerInput');
    const search=document.getElementById('searchBtn');
    const refresh=document.getElementById('refreshBtn');
    const dockRefresh=document.getElementById('dockRefresh');
    if(search) search.onclick=()=>recoveryLoadTicker(input?.value||'');
    if(refresh) refresh.onclick=()=>recoveryLoadTicker(input?.value||'',true);
    if(input) input.onkeydown=e=>{if(e.key==='Enter') recoveryLoadTicker(e.target.value,true);};
    if(dockRefresh) dockRefresh.onclick=()=>window.currentTicker&&recoveryLoadTicker(window.currentTicker,true);
  });
})();
