// V5.4.7 UI cleanup: keep visible module/version badges aligned with deployed runtime.
(() => {
  const VERSION='5.4.7';
  function patch(){
    const kpis=document.getElementById('kpis');
    if(kpis){
      kpis.querySelectorAll('small').forEach(el=>{
        if(/^V5\.2\.15$/i.test((el.textContent||'').trim())) el.textContent=`V${VERSION}`;
      });
    }
    document.querySelectorAll('[data-app-version]').forEach(el=>el.textContent=`V${VERSION}`);
  }
  const observer=new MutationObserver(patch);
  window.addEventListener('DOMContentLoaded',()=>{
    patch();
    observer.observe(document.body,{subtree:true,childList:true,characterData:true});
  });
})();
