(()=>{
  const VERSION='5.10.0';
  function cleanVersionUi(){
    const status=document.querySelector('.cloud-status');
    if(!status)return;
    // Keep only cloudStatus + lastFetch; remove every extra static/legacy V5.x badge and orphan separators.
    [...status.children].forEach(el=>{
      if(el.id==='cloudStatus'||el.id==='lastFetch'||el.classList.contains('status-dot'))return;
      const t=(el.textContent||'').trim();
      if(/^V5\.\d+(?:\.\d+)?$/.test(t)||el.hasAttribute('data-app-version'))el.remove();
    });
    // Collapse duplicate separators after legacy nodes are removed.
    let prevSep=false;
    [...status.children].forEach(el=>{
      if(el.classList.contains('status-sep')){
        if(prevSep)el.remove(); else prevSep=true;
      }else if(!el.classList.contains('status-dot')) prevSep=false;
    });
    const last=status.lastElementChild;
    if(last?.classList?.contains('status-sep')) last.remove();
    document.title=`AI Stock Research Terminal V${VERSION}`;
  }
  cleanVersionUi();
  new MutationObserver(cleanVersionUi).observe(document.documentElement,{subtree:true,childList:true});
  if('serviceWorker' in navigator){
    navigator.serviceWorker.register(`/sw.js?v=${VERSION}`,{updateViaCache:'none'}).then(r=>r.update()).catch(()=>{});
  }
})();
