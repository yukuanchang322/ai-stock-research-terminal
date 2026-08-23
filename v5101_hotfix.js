(()=>{
  'use strict';
  const VERSION='5.10.1';
  const normalizeVersionUi=()=>{
    const cloud=document.getElementById('cloudStatus');
    if(cloud){
      fetch('/health',{cache:'no-store'}).then(r=>r.json()).then(j=>{
        if(j?.version) cloud.textContent=`雲端服務正常 · V${j.version}`;
      }).catch(()=>{});
    }
    document.querySelectorAll('[data-app-version]').forEach(el=>el.remove());
    document.querySelectorAll('.cloud-status .status-sep').forEach((el,i,arr)=>{
      const prev=el.previousElementSibling, next=el.nextElementSibling;
      if(!prev||!next||prev.classList.contains('status-sep')||next.classList.contains('status-sep')) el.remove();
    });
    document.title=`AI Stock Research Terminal V${VERSION}`;
  };
  window.addEventListener('DOMContentLoaded',normalizeVersionUi);
  window.addEventListener('load',()=>setTimeout(normalizeVersionUi,100));
  new MutationObserver(normalizeVersionUi).observe(document.documentElement,{subtree:true,childList:true,characterData:true});
})();
