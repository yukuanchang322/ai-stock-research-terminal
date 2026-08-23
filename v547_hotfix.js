// V5.4.9 UI: version sync + monthly revenue YoY bar chart.
(() => {
  const VERSION='5.4.9';
  function patchVersion(){
    document.title=`AI Stock Research Terminal V${VERSION}`;
    const kpis=document.getElementById('kpis');
    if(kpis) kpis.querySelectorAll('small').forEach(el=>{if(/^V5\.2\.15$/i.test((el.textContent||'').trim()))el.textContent=`V${VERSION}`;});
    document.querySelectorAll('[data-app-version]').forEach(el=>el.textContent=`V${VERSION}`);
  }
  function revenueYoY(series){
    const rows=(series||[]).filter(x=>x&&x.period&&Number.isFinite(Number(x.revenue)));
    const map=new Map(rows.map(x=>[String(x.period),Number(x.revenue)]));
    return rows.map(x=>{const m=String(x.period).match(/^(\d{4})-(\d{2})$/);if(!m)return null;const base=map.get(`${Number(m[1])-1}-${m[2]}`);if(!Number.isFinite(base)||base===0)return null;return {period:x.period,yoy:(Number(x.revenue)/base-1)*100};}).filter(Boolean).slice(-12);
  }
  function revenueBarSvg(series){
    const data=revenueYoY(series);if(!data.length)return '<div class="empty">營收年增資料不足</div>';
    const w=720,h=220,pl=42,pr=14,pt=20,pb=34,vals=data.map(x=>x.yoy),min=Math.min(0,...vals),max=Math.max(0,...vals),span=(max-min)||1;
    const y=v=>pt+(max-v)*(h-pt-pb)/span,zero=y(0),step=(w-pl-pr)/data.length,bw=Math.max(8,step*.58);
    const bars=data.map((x,i)=>{const cx=pl+(i+.5)*step,yy=y(x.yoy),top=Math.min(yy,zero),bh=Math.max(1,Math.abs(yy-zero)),cls=x.yoy>=0?'revenue-bar positive':'revenue-bar negative',label=(i%2===0||i===data.length-1)?`<text class="revenue-xlabel" x="${cx}" y="${h-10}" text-anchor="middle">${String(x.period).slice(5)}</text>`:'';return `<g><rect class="${cls}" x="${cx-bw/2}" y="${top}" width="${bw}" height="${bh}" rx="2"><title>${x.period} YoY ${x.yoy>=0?'+':''}${x.yoy.toFixed(1)}%</title></rect>${label}</g>`;}).join('');
    const latest=data[data.length-1];return `<div class="tech-chart-title"><b>近12月營收年增率</b><span>最新 ${latest.yoy>=0?'+':''}${latest.yoy.toFixed(1)}%</span></div><svg class="revenue-bar-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line class="revenue-zero" x1="${pl}" y1="${zero}" x2="${w-pr}" y2="${zero}"/>${bars}<text class="revenue-axis-label" x="6" y="${pt+8}">${max.toFixed(0)}%</text><text class="revenue-axis-label" x="6" y="${Math.min(h-pb,zero)+4}">0%</text>${min<0?`<text class="revenue-axis-label" x="6" y="${h-pb}">${min.toFixed(0)}%</text>`:''}</svg><small class="chart-note">各月營收相較去年同月；正值為成長、負值為衰退。</small>`;
  }
  function patchReport(d){
    patchVersion();const chart=document.getElementById('fundChart');if(chart&&d?.revenue?.series?.length)chart.innerHTML=revenueBarSvg(d.revenue.series);
    const heading=chart?.previousElementSibling;if(heading&&heading.tagName==='H4')heading.textContent='近 12 月營收成長 YoY';
    const eps=document.getElementById('epsBasis');if(eps&&d?.valuation?.valuation_anchor==='market_implied_pe_same_eps_basis')eps.textContent=`${d.valuation.eps_basis||''}｜估值錨：市場隱含 PER ${d.valuation.market_implied_pe??'—'}x（歷史樣本不足，避免口徑錯配）`;
  }
  const originalRender=window.render;if(typeof originalRender==='function')window.render=function(d){const result=originalRender(d);try{patchReport(d);}catch(e){console.warn('v5.4.9 ui patch failed',e);}return result;};
  const observer=new MutationObserver(patchVersion);window.addEventListener('DOMContentLoaded',()=>{patchVersion();observer.observe(document.body,{subtree:true,childList:true,characterData:true});});
})();
