// V5.5.0 UI hardening: revenue YoY bars + mobile institutional-flow matrix.
(() => {
  'use strict';
  const VERSION='5.5.0';
  let latestReport=null;
  const nativeFetch=window.fetch.bind(window);
  const css=`
    .revenue-yoy-wrap{width:100%;overflow:hidden}.revenue-yoy-head{display:flex;justify-content:space-between;gap:10px;align-items:center;margin:2px 2px 8px;font-size:11px;color:var(--muted)}
    .revenue-yoy-head b{color:var(--text);font-size:12px}.revenue-bar-svg{display:block;width:100%;height:210px}.revenue-zero{stroke:#405461;stroke-width:1}.revenue-bar.positive{fill:var(--accent)}.revenue-bar.negative{fill:var(--red)}.revenue-xlabel,.revenue-axis-label{fill:var(--muted);font-size:10px}.chart-note{display:block;color:var(--muted);font-size:10px;line-height:1.45;margin:6px 2px 0}
    #flowTable{width:100%;min-width:0;overflow:hidden}.flow-matrix{width:100%;min-width:0;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#0a171f}.flow-head,.flow-matrix-row{display:grid!important;grid-template-columns:54px repeat(3,minmax(0,1fr))!important;align-items:stretch;width:100%;min-width:0}.flow-head>* ,.flow-matrix-row>*{min-width:0;padding:11px 6px;border-right:1px solid var(--line);display:flex;align-items:center;justify-content:flex-end;overflow:hidden}.flow-head>*:last-child,.flow-matrix-row>*:last-child{border-right:0}.flow-head{border-bottom:1px solid var(--line);color:var(--muted);font-size:10px}.flow-head b:first-child,.flow-matrix-row>b{justify-content:flex-start}.flow-matrix-row{border-bottom:1px solid var(--line)}.flow-matrix-row:last-child{border-bottom:0}.flow-matrix-row>b{font-size:11px;line-height:1.15}.flow-matrix-row span{font-size:11px;font-weight:800;white-space:nowrap;letter-spacing:-.02em}.flow-matrix-row .pos{color:var(--accent)}.flow-matrix-row .neg{color:var(--red)}.margin-row span{font-weight:700}
    @media(max-width:600px){.revenue-bar-svg{height:190px}.flow-head,.flow-matrix-row{grid-template-columns:46px repeat(3,minmax(0,1fr))!important}.flow-head>* ,.flow-matrix-row>*{padding:10px 4px}.flow-matrix-row span{font-size:10px}.flow-matrix-row>b{font-size:10px}.two-col .panel{min-width:0;overflow:hidden}}
  `;
  const style=document.createElement('style');style.textContent=css;document.head.appendChild(style);

  function patchVersion(){
    document.title=`AI Stock Research Terminal V${VERSION}`;
    document.querySelectorAll('[data-app-version]').forEach(el=>el.textContent=`V${VERSION}`);
    const k=document.getElementById('kpis');if(k)k.querySelectorAll('small').forEach(el=>{if(/^V5\./i.test((el.textContent||'').trim()))el.textContent=`V${VERSION}`;});
  }
  function revenueYoY(series){
    const rows=(series||[]).filter(x=>x&&x.period&&Number.isFinite(Number(x.revenue)));
    const map=new Map(rows.map(x=>[String(x.period),Number(x.revenue)]));
    return rows.map(x=>{const m=String(x.period).match(/^(\d{4})-(\d{2})$/);if(!m)return null;const base=map.get(`${Number(m[1])-1}-${m[2]}`);if(!Number.isFinite(base)||base===0)return null;return {period:String(x.period),yoy:(Number(x.revenue)/base-1)*100};}).filter(Boolean).slice(-12);
  }
  function revenueBars(series){
    const data=revenueYoY(series);if(!data.length)return '<div class="empty">營收年增資料不足</div>';
    const w=720,h=210,pl=44,pr=12,pt=14,pb=30,vals=data.map(x=>x.yoy),min=Math.min(0,...vals),max=Math.max(0,...vals),span=(max-min)||1;
    const y=v=>pt+(max-v)*(h-pt-pb)/span,zero=y(0),step=(w-pl-pr)/data.length,bw=Math.max(12,step*.62);
    const bars=data.map((x,i)=>{const cx=pl+(i+.5)*step,yy=y(x.yoy),top=Math.min(yy,zero),bh=Math.max(1,Math.abs(yy-zero));return `<g><rect class="revenue-bar ${x.yoy>=0?'positive':'negative'}" x="${cx-bw/2}" y="${top}" width="${bw}" height="${bh}" rx="3"><title>${x.period} YoY ${x.yoy>=0?'+':''}${x.yoy.toFixed(1)}%</title></rect><text class="revenue-xlabel" x="${cx}" y="${h-9}" text-anchor="middle">${x.period.slice(5)}</text></g>`}).join('');
    const latest=data[data.length-1];return `<div class="revenue-yoy-wrap"><div class="revenue-yoy-head"><b>近 12 月營收年增率</b><span>最新 ${latest.yoy>=0?'+':''}${latest.yoy.toFixed(1)}%</span></div><svg class="revenue-bar-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line class="revenue-zero" x1="${pl}" y1="${zero}" x2="${w-pr}" y2="${zero}"/>${bars}<text class="revenue-axis-label" x="4" y="${pt+8}">${max.toFixed(0)}%</text><text class="revenue-axis-label" x="8" y="${Math.min(h-pb-2,Math.max(pt+10,zero+4))}">0%</text>${min<0?`<text class="revenue-axis-label" x="4" y="${h-pb}">${min.toFixed(0)}%</text>`:''}</svg><small class="chart-note">每根柱為該月營收相較去年同月的 YoY；綠色成長、紅色衰退。</small></div>`;
  }
  function compact(v){
    if(v==null||!Number.isFinite(Number(v)))return '—';const n=Number(v),a=Math.abs(n),sign=n>0?'+':n<0?'-':'';if(a>=1e8)return `${sign}${(a/1e8).toFixed(a>=1e9?1:2)}億`;if(a>=1e4)return `${sign}${(a/1e4).toFixed(a>=1e7?0:1)}萬`;return `${sign}${Math.round(a).toLocaleString('zh-TW')}`;
  }
  function flowMatrix(fl){
    const rows=[['外資','foreign'],['投信','trust'],['自營商','dealer']];
    const cell=v=>`<span class="${Number(v)<0?'neg':'pos'}" title="${v==null?'—':Number(v).toLocaleString('zh-TW')}">${compact(v)}</span>`;
    return `<div class="flow-matrix"><div class="flow-head"><b>法人</b><b>1日</b><b>5日</b><b>20日</b></div>${rows.map(([label,key])=>`<div class="flow-matrix-row"><b>${label}</b>${[1,5,20].map(n=>cell(fl?.[`${key}_${n}`])).join('')}</div>`).join('')}<div class="flow-matrix-row margin-row"><b>融資%</b>${[1,5,20].map(n=>{const v=fl?.[`margin_${n}_pct`];return `<span class="${Number(v)>0?'neg':'pos'}">${v==null?'—':`${Number(v)>0?'+':''}${Number(v).toFixed(1)}%`}</span>`}).join('')}</div></div>`;
  }
  function apply(d){
    if(!d)return;latestReport=d;patchVersion();
    const chart=document.getElementById('fundChart');if(chart&&d.revenue?.series?.length){chart.innerHTML=revenueBars(d.revenue.series);const h=chart.previousElementSibling;if(h&&h.tagName==='H4')h.textContent='近 12 月營收成長 YoY';}
    const ft=document.getElementById('flowTable');if(ft&&d.flow)ft.innerHTML=flowMatrix(d.flow);
    const eps=document.getElementById('epsBasis');if(eps&&d.valuation?.valuation_anchor==='market_implied_pe_same_eps_basis')eps.textContent=`${d.valuation.eps_basis||''}｜估值錨：市場隱含 PER ${d.valuation.market_implied_pe??'—'}x（歷史樣本不足，避免口徑錯配）`;
  }
  window.fetch=async function(input,init){
    const response=await nativeFetch(input,init);try{const u=new URL(typeof input==='string'?input:input.url,location.origin);if(/^\/api\/stock\//.test(u.pathname)&&response.ok){const d=await response.clone().json();if(d?.ticker){latestReport=d;setTimeout(()=>apply(d),0);setTimeout(()=>apply(d),80);}}}catch(_){ }return response;
  };
  const observer=new MutationObserver(()=>{patchVersion();if(latestReport){const c=document.getElementById('fundChart');if(c&&!c.querySelector('.revenue-bar-svg'))apply(latestReport);}});
  window.addEventListener('DOMContentLoaded',()=>{patchVersion();observer.observe(document.body,{subtree:true,childList:true});});
})();
