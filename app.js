const $=id=>document.getElementById(id);
const fmt=(v,d=1)=>v==null?'—':Number(v).toLocaleString('zh-TW',{maximumFractionDigits:d,minimumFractionDigits:d});
const fmt0=v=>v==null?'—':Number(v).toLocaleString('zh-TW',{maximumFractionDigits:0});
const pct=(v,d=1)=>v==null?'—':`${v>=0?'+':''}${Number(v).toFixed(d)}%`;
let currentTicker='';
let stockRequestSequence=0;
let marginHistoryRefreshTimer=null;
const marginHistoryRefreshAttempts={};
let candleViewState={ticker:'',size:0,end:0};

function lineSvg(values){
  if(!values.length)return '<div class="empty">資料不足</div>';
  const w=560,h=170,p=16,min=Math.min(...values),max=Math.max(...values),range=max-min||1;
  const pts=values.map((v,i)=>`${p+i*(w-2*p)/(Math.max(1,values.length-1))},${h-p-(v-min)*(h-2*p)/range}`).join(' ');
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line x1="${p}" y1="${h-p}" x2="${w-p}" y2="${h-p}" stroke="#304653"/><polyline points="${pts}" fill="none" stroke="#4ce0b3" stroke-width="3" vector-effect="non-scaling-stroke"/></svg>`;
}

function revenueBarSvg(series){
  const rows=(series||[]).filter(x=>x?.period&&x.revenue!=null).slice(-24);
  if(!rows.length)return '<div class="empty">營收資料不足</div>';
  const w=640,h=230,left=54,right=624,top=22,bottom=184,plotW=right-left,max=Math.max(1,...rows.map(x=>Number(x.revenue))),step=plotW/rows.length,barW=Math.max(5,step*.66);
  const bars=rows.map((row,i)=>{const value=Number(row.revenue),height=value/max*(bottom-top),x=left+i*step+(step-barW)/2,y=bottom-height;return `<rect class="revenue-bar" x="${x}" y="${y}" width="${barW}" height="${height}" data-index="${i}"/>`}).join('');
  const ticks=[0,Math.floor((rows.length-1)/2),rows.length-1],labels=ticks.map(i=>`<text class="revenue-date" x="${left+(i+.5)*step}" y="${h-12}" text-anchor="middle">${rows[i].period.replace('-','/')}</text>`).join('');
  return `<div class="revenue-chart"><svg viewBox="0 0 ${w} ${h}" role="img" aria-label="近 ${rows.length} 個月實際營收柱狀圖"><line class="revenue-grid" x1="${left}" y1="${top}" x2="${right}" y2="${top}"/><line class="revenue-grid" x1="${left}" y1="${bottom}" x2="${right}" y2="${bottom}"/><text class="revenue-axis" x="${left-7}" y="${top+4}" text-anchor="end">${fmt(max/1e8,0)}</text><text class="revenue-axis" x="${left-7}" y="${bottom+4}" text-anchor="end">0</text>${bars}${labels}<rect class="revenue-hit" x="${left}" y="${top}" width="${plotW}" height="${bottom-top}"/></svg><div class="revenue-tooltip" hidden></div><small>單位：億元 · 公司公告實際月營收</small></div>`;
}
function bindRevenueTooltip(series){
  const rows=(series||[]).filter(x=>x?.period&&x.revenue!=null).slice(-24),chart=document.querySelector('.revenue-chart'),svg=chart?.querySelector('svg'),tip=chart?.querySelector('.revenue-tooltip');
  if(!rows.length||!svg||!tip)return;
  const show=e=>{const rect=svg.getBoundingClientRect(),ratio=Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width)),idx=Math.min(rows.length-1,Math.max(0,Math.floor(ratio*rows.length))),row=rows[idx],prior=rows.find(x=>Number(x.period.slice(0,4))===Number(row.period.slice(0,4))-1&&x.period.slice(5)===row.period.slice(5)),yoy=prior&&Number(prior.revenue)?(Number(row.revenue)/Number(prior.revenue)-1)*100:null;tip.innerHTML=`<b>${row.period}</b><span>實際營收 ${fmt(Number(row.revenue)/1e8,2)} 億元</span><span>年增 ${pct(yoy)}</span>`;tip.hidden=false;tip.style.left=`${Math.max(20,Math.min(80,ratio*100))}%`;};
  svg.addEventListener('pointerdown',show);svg.addEventListener('pointermove',e=>{if(e.pointerType==='mouse'||e.buttons)show(e)});svg.addEventListener('pointerleave',()=>{tip.hidden=true});
}

function money(v){
  if(v==null)return '—';
  const n=Number(v),a=Math.abs(n),unit=a>=1e8?'億':a>=1e4?'萬':'元',scaled=a>=1e8?n/1e8:a>=1e4?n/1e4:n;
  return `${n>0?'+':''}${Number(scaled).toLocaleString('zh-TW',{maximumFractionDigits:a>=1e8?2:0})} ${unit}`;
}

function flowMatrix(fl){
  const rows=[['外資','foreign'],['投信','trust'],['自營商','dealer']];
  const periods=[[1,'今日'],[5,'近 5 日'],[20,'近 20 日']];
  const cards=rows.map(([label,key])=>`<section class="flow-card" aria-label="${label}估算淨買賣金額"><div class="flow-card-head"><b>${label}</b><small>估算淨買賣金額</small></div><div class="flow-periods">${periods.map(([n,period])=>{const v=fl[`${key}_${n}_amount`];return `<div class="flow-period"><span>${period}</span><strong class="${v<0?'neg':'pos'}">${money(v)}</strong></div>`}).join('')}</div></section>`).join('');
  const margin=`<section class="margin-card" aria-label="融資餘額變化"><div class="flow-card-head"><b>融資餘額變化</b><small>增減百分比</small></div><div class="flow-periods">${periods.map(([n,period])=>{const v=fl[`margin_${n}_pct`];return `<div class="flow-period"><span>${period}</span><strong class="${v>0?'neg':'pos'}">${v==null?'—':`${v>0?'+':''}${fmt(v,1)}%`}</strong></div>`}).join('')}</div></section>`;
  return `<div class="flow-cards">${cards}</div><small class="flow-amount-note">換算方式：每日淨買賣股數 × 當日收盤價；屬估算金額，非官方逐筆成交金額。</small>${margin}`;
}
function signedLots(v){
  if(v==null)return '—';
  const lots=Number(v);
  return `${lots>0?'+':''}${Number(lots).toLocaleString('zh-TW',{maximumFractionDigits:1})} 張`;
}
function creditHistoryChart(series,key,label){
  const balanceKey=`${key}_balance`,changeKey=`${key}_change`;
  const rows=series.filter(x=>x?.date&&x[balanceKey]!=null).slice(-60);
  if(rows.length<2)return `<section class="credit-chart-card"><div class="credit-chart-head"><div><b>${label}</b><small>官方歷史準備中</small></div></div><div class="empty credit-chart-empty">正在背景補齊最近 60 個交易日…</div></section>`;
  const w=640,h=224,left=54,right=596,top=48,bottom=178,zero=(top+bottom)/2,plotW=right-left;
  const balances=rows.map(x=>Number(x[balanceKey])),changes=rows.map(x=>Number(x[changeKey]||0));
  const maxChange=Math.max(1,...changes.map(Math.abs)),rawMin=Math.min(...balances),rawMax=Math.max(...balances),padding=Math.max(1,(rawMax-rawMin)*.08),minBalance=rawMin-padding,maxBalance=rawMax+padding,range=maxBalance-minBalance||1;
  const x=i=>left+i*plotW/Math.max(1,rows.length-1),changeY=v=>zero-v*(bottom-top)/(2*maxChange),balanceY=v=>bottom-(v-minBalance)*(bottom-top)/range,barW=Math.max(2,Math.min(8,plotW/rows.length*.65));
  const bars=changes.map((v,i)=>{const y=changeY(v),height=Math.max(1,Math.abs(y-zero));return `<rect class="credit-bar ${v<0?'down':'up'}" x="${x(i)-barW/2}" y="${Math.min(y,zero)}" width="${barW}" height="${height}"/>`}).join('');
  const line=balances.map((v,i)=>`${x(i)},${balanceY(v)}`).join(' '),mid=Math.floor((rows.length-1)/2),latest=rows[rows.length-1],latestBalance=latest[balanceKey],latestChange=latest[changeKey];
  const dateLabel=i=>String(rows[i].date).slice(0,10).replaceAll('-','/');
  return `<section class="credit-chart-card"><div class="credit-chart-head"><div><b>${label}餘額 ${signedLots(latestBalance).replace('+','')}</b><small class="${latestChange<0?'pos':'neg'}">${latestChange==null?'今日增減 —':`${latestChange>0?'增加':'減少'} ${signedLots(Math.abs(latestChange)).replace('+','')}`}</small></div><span>柱：每日增減<br>線：餘額</span></div><div class="credit-chart" data-kind="${key}"><svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${label}最近 ${rows.length} 個交易日餘額與每日增減"><line class="credit-grid" x1="${left}" y1="${top}" x2="${right}" y2="${top}"/><line class="credit-grid zero" x1="${left}" y1="${zero}" x2="${right}" y2="${zero}"/><line class="credit-grid" x1="${left}" y1="${bottom}" x2="${right}" y2="${bottom}"/>${bars}<polyline class="credit-balance-line" points="${line}"/><text class="credit-axis left top" x="${left-7}" y="${top+4}" text-anchor="end">${fmt0(maxChange)}</text><text class="credit-axis left" x="${left-7}" y="${zero+4}" text-anchor="end">0</text><text class="credit-axis left bottom" x="${left-7}" y="${bottom+4}" text-anchor="end">-${fmt0(maxChange)}</text><text class="credit-axis right top" x="${right+7}" y="${top+4}">${fmt0(maxBalance)}</text><text class="credit-axis right bottom" x="${right+7}" y="${bottom+4}">${fmt0(minBalance)}</text><text class="credit-date" x="${left}" y="${h-12}">${dateLabel(0)}</text><text class="credit-date" x="${x(mid)}" y="${h-12}" text-anchor="middle">${dateLabel(mid)}</text><text class="credit-date" x="${right}" y="${h-12}" text-anchor="end">${dateLabel(rows.length-1)}</text><rect class="credit-hit" x="${left}" y="${top}" width="${plotW}" height="${bottom-top}"/></svg><div class="credit-chart-tooltip" hidden></div></div></section>`;
}
function marginHistoryDashboard(fl){
  const series=fl.margin_history||[];
  return `${creditHistoryChart(series,'margin','融資')}${creditHistoryChart(series,'short','融券')}<small class="credit-chart-source">資料：${fl.margin_history_source||'TWSE MI_MARGN／FinMind fallback'} · 官方交易單位（張） · 截至 ${fl.margin_last_date||'—'}</small>`;
}
function bindCreditChartTooltips(series){
  document.querySelectorAll('.credit-chart').forEach(chart=>{
    const kind=chart.dataset.kind,rows=(series||[]).filter(x=>x?.date&&x[`${kind}_balance`]!=null).slice(-60),svg=chart.querySelector('svg'),tip=chart.querySelector('.credit-chart-tooltip');
    if(!rows.length||!svg||!tip)return;
    const show=e=>{const rect=svg.getBoundingClientRect(),ratio=Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width)),idx=Math.round(ratio*(rows.length-1)),row=rows[idx],balance=row[`${kind}_balance`],change=row[`${kind}_change`];tip.innerHTML=`<b>${String(row.date).slice(0,10)}</b><span>餘額 ${signedLots(balance).replace('+','')}</span><span>當日增減 ${signedLots(change)}</span>`;tip.hidden=false;tip.style.left=`${Math.max(16,Math.min(84,ratio*100))}%`;};
    svg.addEventListener('pointerdown',show);svg.addEventListener('pointermove',e=>{if(e.pointerType==='mouse'||e.buttons)show(e)});svg.addEventListener('pointerleave',()=>{tip.hidden=true});
  });
}
function scheduleMarginHistoryRefresh(ticker,seriesLength,revenueLength,institutionalLength,financialVerified){
  clearTimeout(marginHistoryRefreshTimer);
  if(seriesLength>=21&&revenueLength>=24&&institutionalLength>=20&&financialVerified){delete marginHistoryRefreshAttempts[ticker];return;}
  if((marginHistoryRefreshAttempts[ticker]||0)>=20)return;
  marginHistoryRefreshTimer=setTimeout(async()=>{if(currentTicker!==ticker)return;marginHistoryRefreshAttempts[ticker]=(marginHistoryRefreshAttempts[ticker]||0)+1;try{const response=await fetch(`/api/stock/${encodeURIComponent(ticker)}`,{cache:'no-store'}),data=await readApiResponse(response);if(response.ok&&currentTicker===ticker)render(data)}catch(e){console.warn('official history refresh pending',e)}},10000);
}
function _techLinePoints(series,key,w,h,p,min,max){
  const vals=series.map(x=>x[key]);
  const range=(max-min)||1;
  let chunks=[],cur=[];
  vals.forEach((v,i)=>{if(v==null||!Number.isFinite(Number(v))){if(cur.length){chunks.push(cur);cur=[]}return}
    cur.push(`${p+i*(w-2*p)/Math.max(1,series.length-1)},${h-p-(Number(v)-min)*(h-2*p)/range}`)});
  if(cur.length)chunks.push(cur);
  return chunks.map(c=>c.join(' '));
}
function candleSvg(series){
  if(!series?.length)return '<div class="empty">K線資料不足</div>';
  const data=series.filter(x=>[x.open,x.high,x.low,x.close].every(v=>v!=null&&Number.isFinite(Number(v))));
  if(!data.length)return '<div class="empty">K線資料不足</div>';
  const w=720,h=300,p=42, vals=data.flatMap(x=>[x.high,x.low,x.ma20,x.ma60].filter(v=>v!=null).map(Number));
  const min=Math.min(...vals),max=Math.max(...vals),range=max-min||1, xstep=(w-2*p)/Math.max(1,data.length);
  const y=v=>h-p-(Number(v)-min)*(h-2*p)/range;
  const body=Math.max(1,Math.min(5,xstep*.66));
  const candles=data.map((x,i)=>{const cx=p+(i+.5)*xstep, up=Number(x.close)>=Number(x.open), cls=up?'up':'down';
    const yo=y(x.open),yc=y(x.close),yh=y(x.high),yl=y(x.low),top=Math.min(yo,yc),bh=Math.max(1,Math.abs(yc-yo));
    return `<g class="candle ${cls}"><line x1="${cx}" y1="${yh}" x2="${cx}" y2="${yl}"/><rect x="${cx-body/2}" y="${top}" width="${body}" height="${bh}"/></g>`}).join('');
  const p20=_techLinePoints(data,'ma20',w,h,p,min,max).map(points=>`<polyline class="ma20" points="${points}"/>`).join('');
  const p60=_techLinePoints(data,'ma60',w,h,p,min,max).map(points=>`<polyline class="ma60" points="${points}"/>`).join('');
  const priceTicks=[max,(max+min)/2,min].map(v=>`<g class="price-tick"><line x1="${p}" y1="${y(v)}" x2="${w-p}" y2="${y(v)}"/><text x="${p-6}" y="${y(v)+3}" text-anchor="end">${fmt(v,1)}</text></g>`).join('');
  const dateIndexes=[0,.25,.5,.75,1].map(r=>Math.round(r*(data.length-1))).filter((v,i,a)=>a.indexOf(v)===i);
  const dateTicks=dateIndexes.map((i,j)=>{const xx=p+(i+.5)*xstep,anchor=j===0?'start':(j===dateIndexes.length-1?'end':'middle');return `<text class="candle-date" x="${xx}" y="${h-8}" text-anchor="${anchor}">${String(data[i]?.date||'').slice(0,10)}</text>`}).join('');
  const latest=data[data.length-1],previous=data[data.length-2],latestChange=previous?.close?((Number(latest.close)-Number(previous.close))/Number(previous.close)*100):null,latestY=y(latest.close);
  return `<div class="candle-chart"><div class="tech-chart-title"><b>日K</b><span>點選價位 · 左右拖曳 · 雙指縮放</span></div><div class="candle-controls" aria-label="K線縮放控制"><button type="button" data-candle-action="zoom-in" aria-label="放大K線">＋</button><button type="button" data-candle-action="zoom-out" aria-label="縮小K線">－</button><button type="button" data-candle-action="reset">重設一年</button><span>${data.length} 個交易日</span></div><svg class="candle-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="日K，含日期與價格刻度，可縮放與左右移動">${priceTicks}<line class="axis" x1="${p}" y1="${h-p}" x2="${w-p}" y2="${h-p}"/>${candles}${p20}${p60}<line class="latest-price-line" x1="${p}" y1="${latestY}" x2="${w-p}" y2="${latestY}"/>${dateTicks}<g class="candle-crosshair" hidden><line class="crosshair-x" y1="${p}" y2="${h-p}"/><circle class="crosshair-dot" r="4"/></g><rect class="candle-hit" x="${p}" y="${p}" width="${w-2*p}" height="${h-2*p}"/></svg><div class="candle-details" aria-live="polite">${candleDetailsHtml(latest,latestChange)}</div><div class="chart-legend"><span>MA20</span><span>MA60</span></div></div>`;
}
function candleDetailsHtml(row,change){
  const changeText=change==null?'—':`${change>=0?'+':''}${fmt(change,2)}%`;
  return `<div class="candle-details-head"><b>${String(row.date||'').slice(0,10)}</b><span>漲跌 <strong class="${change!=null&&change<0?'down':'up'}">${changeText}</strong></span></div><div class="candle-details-prices"><span>開 <b>${fmt(row.open,1)}</b></span><span>高 <b>${fmt(row.high,1)}</b></span><span>低 <b>${fmt(row.low,1)}</b></span><span>收 <b>${fmt(row.close,1)}</b></span><span>MA20 <b>${fmt(row.ma20,1)}</b></span><span>MA60 <b>${fmt(row.ma60,1)}</b></span></div>`;
}
function bindCandleTooltip(series){
  const rows=(series||[]).filter(x=>[x.open,x.high,x.low,x.close].every(v=>v!=null&&Number.isFinite(Number(v)))),chart=document.querySelector('.candle-chart'),svg=chart?.querySelector('.candle-svg'),details=chart?.querySelector('.candle-details'),crosshair=chart?.querySelector('.candle-crosshair');
  if(!rows.length||!svg||!details||!crosshair)return;
  const w=720,h=300,p=42,vals=rows.flatMap(x=>[x.high,x.low,x.ma20,x.ma60].filter(v=>v!=null).map(Number)),min=Math.min(...vals),max=Math.max(...vals),range=max-min||1,y=v=>h-p-(Number(v)-min)*(h-2*p)/range;
  const show=e=>{const rect=svg.getBoundingClientRect(),ratio=Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width)),idx=Math.min(rows.length-1,Math.floor(ratio*rows.length)),row=rows[idx],prev=idx?Number(rows[idx-1].close):null,change=prev?((Number(row.close)-prev)/prev*100):null,cx=p+(idx+.5)*(w-2*p)/rows.length,cy=y(row.close);crosshair.querySelector('.crosshair-x').setAttribute('x1',cx);crosshair.querySelector('.crosshair-x').setAttribute('x2',cx);crosshair.querySelector('.crosshair-dot').setAttribute('cx',cx);crosshair.querySelector('.crosshair-dot').setAttribute('cy',cy);crosshair.hidden=false;details.innerHTML=candleDetailsHtml(row,change);};
  svg.addEventListener('pointerdown',show);svg.addEventListener('pointermove',e=>{if(e.pointerType==='mouse'||e.buttons)show(e)});svg.addEventListener('pointerleave',e=>{if(e.pointerType==='mouse')crosshair.hidden=true});
}
function candleWindow(rows){
  const total=rows.length,size=Math.max(1,Math.min(total,candleViewState.size||total)),end=Math.max(size,Math.min(total,candleViewState.end||total));
  candleViewState.size=size;candleViewState.end=end;
  return rows.slice(end-size,end);
}
function rerenderCandle(rows){
  const host=document.querySelector('.candle-host');if(!host)return;
  const visible=candleWindow(rows);host.innerHTML=candleSvg(visible);bindCandleTooltip(visible);bindCandleZoomPan(rows);
}
function bindCandleZoomPan(series){
  const rows=(series||[]).filter(x=>[x.open,x.high,x.low,x.close].every(v=>v!=null&&Number.isFinite(Number(v)))),chart=document.querySelector('.candle-chart'),svg=chart?.querySelector('.candle-svg');
  if(rows.length<2||!chart||!svg)return;
  const clamp=(v,min,max)=>Math.max(min,Math.min(max,v)),apply=(size,end)=>{candleViewState.size=clamp(Math.round(size),Math.min(20,rows.length),rows.length);candleViewState.end=clamp(Math.round(end),candleViewState.size,rows.length);rerenderCandle(rows);};
  chart.querySelector('[data-candle-action="zoom-in"]')?.addEventListener('click',()=>apply(candleViewState.size*.7,candleViewState.end));
  chart.querySelector('[data-candle-action="zoom-out"]')?.addEventListener('click',()=>apply(candleViewState.size*1.4,candleViewState.end));
  chart.querySelector('[data-candle-action="reset"]')?.addEventListener('click',()=>apply(rows.length,rows.length));
  const points=new Map();let gesture=null;
  svg.addEventListener('pointerdown',e=>{if(e.pointerType!=='touch')return;points.set(e.pointerId,{x:e.clientX,y:e.clientY});svg.setPointerCapture?.(e.pointerId);if(points.size===1)gesture={kind:'pan',startX:e.clientX,size:candleViewState.size,end:candleViewState.end};else if(points.size===2){const p=[...points.values()],distance=Math.hypot(p[0].x-p[1].x,p[0].y-p[1].y);gesture={kind:'pinch',distance,size:candleViewState.size,end:candleViewState.end,currentDistance:distance}}});
  svg.addEventListener('pointermove',e=>{if(!points.has(e.pointerId))return;points.set(e.pointerId,{x:e.clientX,y:e.clientY});if(gesture?.kind==='pinch'&&points.size>=2){const p=[...points.values()];gesture.currentDistance=Math.hypot(p[0].x-p[1].x,p[0].y-p[1].y)}});
  const finish=e=>{if(!points.has(e.pointerId))return;const point=points.get(e.pointerId);if(gesture?.kind==='pinch'){const distance=gesture.currentDistance||gesture.distance,center=gesture.end-gesture.size/2,newSize=gesture.size*gesture.distance/Math.max(1,distance);points.clear();gesture=null;apply(newSize,center+newSize/2);return}if(gesture?.kind==='pan'&&points.size===1){const dx=point.x-gesture.startX;if(Math.abs(dx)>12){const shift=-dx/svg.getBoundingClientRect().width*gesture.size;points.clear();const g=gesture;gesture=null;apply(g.size,g.end+shift);return}}points.delete(e.pointerId);if(!points.size)gesture=null;};
  svg.addEventListener('pointerup',finish);svg.addEventListener('pointercancel',finish);
}
function oscillatorSvg(series,keys,title,minFixed=null,maxFixed=null,levels=[]){
  if(!series?.length)return '';
  const w=720,h=120,p=20;
  const vals=series.flatMap(x=>keys.map(k=>x[k]).filter(v=>v!=null&&Number.isFinite(Number(v))).map(Number));
  if(!vals.length)return `<div class="indicator-panel"><b>${title}</b><div class="empty">資料不足</div></div>`;
  const min=minFixed==null?Math.min(...vals):minFixed,max=maxFixed==null?Math.max(...vals):maxFixed,range=(max-min)||1;
  const levelSvg=levels.map(v=>{const yy=h-p-(v-min)*(h-2*p)/range;return `<line class="guide" x1="${p}" y1="${yy}" x2="${w-p}" y2="${yy}"/><text x="${p+2}" y="${yy-2}">${v}</text>`}).join('');
  const paths=keys.map((k,j)=>_techLinePoints(series,k,w,h,p,min,max).map(points=>`<polyline class="indicator-line line${j}" points="${points}"/>`).join('')).join('');
  return `<div class="indicator-panel"><div class="indicator-title"><b>${title}</b><span>${keys.map(k=>k.toUpperCase()).join(' / ')}</span></div><svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${levelSvg}${paths}</svg></div>`;
}
function macdSvg(series){
  if(!series?.length)return '';
  const w=720,h=130,p=20;
  const vals=series.flatMap(x=>['macd','macd_signal','macd_hist'].map(k=>x[k]).filter(v=>v!=null).map(Number));
  if(!vals.length)return `<div class="indicator-panel"><b>MACD</b><div class="empty">資料不足</div></div>`;
  const min=Math.min(...vals,0),max=Math.max(...vals,0),range=(max-min)||1,y=v=>h-p-(Number(v)-min)*(h-2*p)/range,xstep=(w-2*p)/Math.max(1,series.length-1),zero=y(0);
  const bars=series.map((x,i)=>x.macd_hist==null?'':`<rect class="macd-bar ${x.macd_hist<0?'neg':''}" x="${p+i*xstep-1}" y="${Math.min(zero,y(x.macd_hist))}" width="${Math.max(1,xstep*.55)}" height="${Math.max(1,Math.abs(y(x.macd_hist)-zero))}"/>`).join('');
  const dif=_techLinePoints(series,'macd',w,h,p,min,max).map(points=>`<polyline class="indicator-line line0" points="${points}"/>`).join('');
  const sig=_techLinePoints(series,'macd_signal',w,h,p,min,max).map(points=>`<polyline class="indicator-line line1" points="${points}"/>`).join('');
  return `<div class="indicator-panel"><div class="indicator-title"><b>MACD</b><span>DIF / Signal / Histogram</span></div><svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line class="guide" x1="${p}" y1="${zero}" x2="${w-p}" y2="${zero}"/>${bars}${dif}${sig}</svg></div>`;
}
function technicalDashboard(t){
  const s=t.series||[];
  return `<div class="technical-dashboard"><div class="candle-host">${candleSvg(candleWindow(s))}</div>${oscillatorSvg(s,['k','d'],'KD',0,100,[20,80])}${macdSvg(s)}${oscillatorSvg(s,['rsi14'],'RSI 14',0,100,[30,70])}</div>`;
}
function metric(k,v,note=''){return `<div class="metric"><span>${k}</span><b>${v}</b><em>${note}</em></div>`}
function targetRow(x){return `<div class="target-row ${x.name==='悲觀'?'bear':x.name==='樂觀'?'bull':'base'}"><span>${x.name}</span><b>${fmt0(x.target)}</b></div>`}

function snapshotKey(ticker){return `ai-stock-v5.2.12-snapshot-${ticker}`}
function currentSnapshot(d){
  return {generated_at:d.generated_at,score:d.scores?.['綜合'],price:d.price,median_target:d.research?.median_target,median_eps:d.research?.median_forward_eps,per:d.per?.per,foreign20:d.flow?.foreign_20_amount,regime:d.expectation_gap?.regime};
}
function deltaText(now,old,digits=1,suffix=''){if(now==null||old==null)return '—';const delta=Number(now)-Number(old);return `${delta>=0?'+':''}${delta.toFixed(digits)}${suffix}`}
function renderSnapshotCompare(d){
  const key=snapshotKey(d.ticker), oldRaw=localStorage.getItem(key), now=currentSnapshot(d);
  let old=null; try{old=oldRaw?JSON.parse(oldRaw):null}catch(e){}
  const box=$('snapshotCompare');
  if(!box)return;
  if(!old){box.innerHTML='<div class="empty bordered">這是此裝置第一次保存這檔股票的分析；下次查詢時會自動顯示前後變化。</div>';}
  else{
    const rows=[['Research Score',old.score,now.score,deltaText(now.score,old.score,0,' 分')],['法人目標價中位數',old.median_target,now.median_target,deltaText(now.median_target,old.median_target,0,'')],['Forward EPS 中位數',old.median_eps,now.median_eps,deltaText(now.median_eps,old.median_eps,2,'')],['PER',old.per,now.per,deltaText(now.per,old.per,1,'x')],['外資20日估算金額',old.foreign20,now.foreign20,deltaText(now.foreign20,old.foreign20,0,' 元')]];
    box.innerHTML=`<div class="snapshot-head"><span>前次 ${old.generated_at?new Date(old.generated_at).toLocaleString('zh-TW'):'—'}</span><b>${old.regime||'—'} → ${now.regime||'—'}</b></div><div class="snapshot-grid">${rows.map(r=>`<div><span>${r[0]}</span><small>${r[1]==null?'—':fmt(r[1],r[0]==='Research Score'?0:1)} → ${r[2]==null?'—':fmt(r[2],r[0]==='Research Score'?0:1)}</small><b>${r[3]}</b></div>`).join('')}</div>`;
  }
  localStorage.setItem(key,JSON.stringify(now));
}


function eventTags(tags){return `<div class="event-tags">${(tags||[]).map(t=>`<span>${t}</span>`).join('')}</div>`}
function _bulletBlock(title,items,cls=''){
  const list=(items||[]).slice(0,3);
  if(!list.length)return '';
  return `<div class="call-bucket ${cls}"><b>${title}</b><ul>${list.map(b=>`<li>${b}</li>`).join('')}</ul></div>`;
}
function earningsCallCard(x,index){
  const official=x.official_source?'<span class="official-badge">官方優先來源</span>':'';
  return `<article class="earnings-call-card">
    <div class="earnings-call-head"><div><small>最近第 ${index+1} 次 · ${x.date||'—'} ${x.quarter_hint?`· ${x.quarter_hint}`:''}</small><a href="${x.source_url||'#'}" target="_blank" rel="noopener noreferrer">${x.title||'法人說明會'}</a>${official}</div>${eventTags(x.tags)}</div>
    <div class="call-buckets">
      ${_bulletBlock('財務重點',x.financial_highlights,'financial')}
      ${_bulletBlock('營運重點',x.operating_highlights,'operations')}
      ${_bulletBlock('管理層展望',x.management_outlook,'outlook')}
      ${_bulletBlock('風險 / 變數',x.risk_highlights,'risk')}
    </div>
    ${(!(x.financial_highlights||[]).length&&!(x.operating_highlights||[]).length&&!(x.management_outlook||[]).length)?`<div class="call-summary"><b>公開摘要</b><ul>${(x.summary_bullets||[]).slice(0,4).map(b=>`<li>${b}</li>`).join('')}</ul></div>`:''}
    <div class="call-source"><span>${x.publisher||'公開來源'}</span><small>${x.official_source?'Official-first':'公開資訊交叉來源'} · 原文數字以來源頁為準</small></div>
  </article>`;
}
function materialInfoCard(x){
  return `<article class="material-info-item"><div><time>${x.date||'—'}</time>${eventTags(x.tags)}</div><div><a href="${x.source_url||'#'}" target="_blank" rel="noopener noreferrer">${x.title||'—'}</a>${x.official_source?'<span class="official-badge small">官方來源</span>':''}<p>${x.summary||''}</p><small>${x.publisher||'公開來源'}</small></div></article>`;
}

async function safeJsonFetch(url, options={}){
  const res=await fetch(url,{...options,cache:'no-store',headers:{...(options.headers||{}),'Accept':'application/json'}});
  const ctype=(res.headers.get('content-type')||'').toLowerCase();
  let payload=null;
  if(ctype.includes('application/json')){
    try{ payload=await res.json(); }catch(e){ payload=null; }
  }else{
    // Never display upstream HTML/CSS/font bodies to the user.
    payload={status:'degraded',message:`HTTP ${res.status}：資料來源暫時異常`,errors:['伺服器回傳非 JSON 錯誤內容，已隱藏']};
  }
  if(!res.ok){
    const msg=(payload&&payload.message) || `HTTP ${res.status}`;
    const err=new Error(msg);
    err.payload=payload||{};
    err.status=res.status;
    throw err;
  }
  return payload||{};
}
function cleanUiError(err){
  const p=err?.payload||{};
  const msg=p.message||err?.message||'資料來源暫時無法連線';
  if(String(msg).includes('@font-face')||String(msg).includes('base64,')||String(msg).includes('<html')){
    return '資料來源暫時異常，錯誤內容已隱藏。';
  }
  return String(msg).slice(0,180);
}

function render(d){
  currentTicker=d.ticker;
  $('report').classList.remove('hidden'); $('pdfBtn').disabled=false;
  $('generatedAt').textContent=new Date(d.generated_at).toLocaleString('zh-TW');
  $('companyName').textContent=d.name; $('tickerLabel').textContent=d.ticker; $('sector').textContent=d.industry; $('marketType').textContent=d.market_type;
  $('stanceTag').textContent=d.stance; $('confidenceScore').textContent=d.confidence?.overall ?? '—'; $('price').textContent=fmt(d.price,1); $('dayChange').textContent=pct(d.change_pct);
  $('thesis').textContent=d.thesis; $('dataPolicy').textContent=d.data_policy; $('overallScore').textContent=d.scores['綜合'];
  $('kpis').innerHTML=[['預期狀態',d.expectation_gap?.regime||'—','V5.2.15'],['Research Score',`${d.scores['綜合']}/100`,'量化綜合'],['可信度',`${d.confidence?.overall??'—'}/100`,'資料+估值'],['PER',`${fmt(d.per?.per,1)}x`,'最新可得'],['營收 YoY',pct(d.revenue?.revenue_yoy),'最新月'],['外資 20日',money(d.flow?.foreign_20_amount),'估算淨買賣金額'],['RSI14',fmt(d.technical?.rsi14,1),'技術動能']].map(x=>`<div class="kpi"><span>${x[0]}</span><b>${x[1]}</b><small>${x[2]}</small></div>`).join('');
  $('dockPdf').disabled=false; $('dockShare').disabled=false; $('lastFetch').textContent=`資料頁產生 ${new Date(d.generated_at).toLocaleTimeString('zh-TW',{hour:'2-digit',minute:'2-digit'})}`;
  // Safari-safe URL update: avoid passing a URL object to history.replaceState.
  try {
    const qs = new URLSearchParams(location.search);
    qs.set('ticker', d.ticker);
    const nextUrl = `${location.pathname}?${qs.toString()}`;
    history.replaceState({}, '', nextUrl);
  } catch (_) { /* URL decoration is non-critical */ }
  $('scoreBars').innerHTML=['基本面','籌碼面','技術面','估值'].map(k=>`<div class="scorebar"><span>${k}</span><div class="scorebar-track"><div class="scorebar-fill" style="width:${d.scores[k]}%"></div></div><b>${d.scores[k]}</b></div>`).join('');

  const scenarios=d.valuation.scenarios||[];
  $('targetRows').innerHTML=scenarios.length?scenarios.map(targetRow).join(''):'<div class="empty">估值資料不足</div>';
  $('epsBasis').textContent=`EPS 基礎：${d.valuation.eps_basis||'資料不足'}`;

  const f=d.financial||{}, r=d.revenue||{}, p=d.per||{}, es=d.eps_stack||{}, fi=d.financial_integrity||{};
  const diagLink=`<a class="diagnostic-link" href="/api/diagnostics/financial/${encodeURIComponent(d.ticker)}" target="_blank" rel="noopener noreferrer">查看官方資料診斷</a>`;
  const staleText=fi.official_verified ? `✅ ${fi.message||'官方最新財報期已驗證'}` : `△ ${fi.message||'財報最新季度尚未通過官方驗證'}；目前缺口以備援暫估，重新查詢取得官方值後自動覆蓋`;
  const staleHtml=fi.official_verified?staleText:`${staleText} · ${diagLink}`;
  const perNote=fi.core_financials_allowed ? (p.last_date||'市場資料') : `${p.last_date||''} · 市場 PER 可顯示，但不代表財報已驗證`;
  $('fundamentalTable').innerHTML=[
    metric('最新月營收',fmt0(r.latest_revenue),r.revenue_period||''), metric('營收 YoY',pct(r.revenue_yoy),'年增率'),
    metric('單季 EPS',fmt(es.quarter_eps,2),`${es.quarter_period||'—'} · ${es.quarter_method_label||'資料不足'}`), metric('YTD EPS',fmt(es.ytd_eps,2),`${es.ytd_period||'—'} · ${es.ytd_method_label||''}`),
    metric('TTM EPS',fmt(es.ttm_eps,2),`${es.ttm_period||'—'} · ${es.ttm_method_label||''}`), metric('財報來源',f.source||es.source||'—',`${f.period||f.statement_date||''} · ${staleHtml}`),
    metric('毛利率',pct(f.gross_margin),`${f.display_badge||'—'} ${f.period||f.statement_date||'最新可得'}${f.margin_sanity==='blocked_invalid_values'?' · 異常值已攔截':''}`), metric('營益率',pct(f.operating_margin),`${f.display_badge||'—'} ${f.period||f.statement_date||'最新可得'}${f.margin_sanity==='blocked_invalid_values'?' · 異常值已攔截':''}`), metric('PER / PBR',`${fmt(p.per,1)}x / ${fmt(p.pbr,1)}x`,perNote)
  ].join('');
  const ledger=(es.evidence_ledger||[]);
  const ledgerHtml=ledger.length?`<div class="eps-ledger"><h4>EPS Evidence Ledger</h4><div class="ledger-list">${ledger.map(x=>`<div class="ledger-row ${x.status==='usable'?'ok':(x.status==='provisional'?'':'missing')}"><b>${x.period}</b><span>${x.status==='provisional'?'△ ':''}${x.quarter_eps_direct!=null?`單季 ${fmt(x.quarter_eps_direct,2)}`:(x.ytd_eps!=null?`YTD ${fmt(x.ytd_eps,2)}`:(x.derived_quarter_eps!=null?`暫估單季 ${fmt(x.derived_quarter_eps,2)}`:'缺資料'))}</span><small>${x.source||x.missing_reason||'無官方證據'}${x.derived_quarter_eps!=null&&x.status!=='provisional'?` · 推導單季 ${fmt(x.derived_quarter_eps,2)}`:''}</small></div>`).join('')}</div></div>`:'';
  const eg=d.evidence_graph||{}, esum=eg.summary||{};
  const evidenceHtml=`<div class="evidence-matrix"><h4>Multi-Source Evidence Matrix</h4><div class="evidence-stats"><div><span>Evidence</span><b>${esum.usable??0}</b></div><div><span>官方/驗證</span><b>${esum.official_or_verified??0}</b></div><div><span>Fact</span><b>${esum.facts??0}</b></div><div><span>Derived</span><b>${esum.derived_facts??0}</b></div><div><span>Estimate</span><b>${esum.estimates??0}</b></div><div><span>真正衝突</span><b>${esum.conflicts??0}</b></div><div><span>預估修正</span><b>${esum.estimate_revisions??0}</b></div><div><span>Evidence Score</span><b>${esum.evidence_score??0}</b></div></div>${(eg.conflicts||[]).length?`<div class="evidence-conflicts"><b>同定義來源衝突</b>${eg.conflicts.slice(0,4).map(c=>`<small>${c.metric} ${c.period||''} · spread ${c.spread_pct}%</small>`).join('')}</div>`:'<small class="evidence-ok">目前核心 Fact 未偵測到同期間、同定義的重大來源衝突。</small>'}${(eg.estimate_revisions||[]).length?`<div class="evidence-revisions"><b>Estimate Revision</b>${eg.estimate_revisions.slice(-4).map(r=>`<small>${r.metric} ${r.period||''} · ${r.revision_pct==null?'—':`${r.revision_pct>0?'+':''}${r.revision_pct}%`}</small>`).join('')}</div>`:''}<a class="diagnostic-link" href="/api/evidence/${encodeURIComponent(d.ticker)}" target="_blank" rel="noopener noreferrer">查看完整 Evidence JSON</a></div>`;
  // This render runs again after background history recovery and stock changes.
  // Replace the owned region atomically so evidence panels and mobile toggles
  // cannot accumulate across renders.
  $('fundamentalEvidence').innerHTML=`${ledgerHtml}${evidenceHtml}`;
  $('fundChart').innerHTML=revenueBarSvg(r.series||[]);
  bindRevenueTooltip(r.series||[]);
  $('fundAnalysis').textContent=r.revenue_yoy==null?`營收年增資料不足。${staleText}`:`最新月營收年增 ${pct(r.revenue_yoy)}；${fi.core_financials_allowed?`${es.quarter_period||'最新財報'} 單季 EPS ${fmt(es.quarter_eps,2)}、YTD EPS ${fmt(es.ytd_eps,2)}、TTM EPS ${fmt(es.ttm_eps,2)}。`:(es.quarter_eps!=null?`${es.quarter_method_label}單季 EPS ${fmt(es.quarter_eps,2)}，僅供暫時分析，不進核心估值。`:'財報 EPS 尚未通過最新季度閘門，不進核心估值。')} ${staleText}`;

  const fl=d.flow||{};
  $('flowTable').innerHTML=flowMatrix(fl);
  $('marginHistoryCharts').innerHTML=marginHistoryDashboard(fl);
  bindCreditChartTooltips(fl.margin_history||[]);
  scheduleMarginHistoryRefresh(d.ticker,(fl.margin_history||[]).length,(r.series||[]).length,fl.institutional_history_count||0,Boolean(fi.official_verified));
  const flows={外資:fl.foreign_20_amount,投信:fl.trust_20_amount,自營商:fl.dealer_20_amount};
  const available=Object.values(flows).filter(v=>v!=null), mx=Math.max(1,...available.map(Math.abs));
  $('flowBars').innerHTML=Object.entries(flows).map(([k,v])=>`<div class="flow-row"><span>${k} 20日</span><div class="flow-track"><div class="flow-fill ${v!=null&&v<0?'neg':''}" style="width:${v==null?0:Math.abs(v)/mx*100}%"></div></div><b>${money(v)}</b></div>`).join('');
  const direction=v=>v==null?'資料不足':(v>=0?'偏買超':'偏賣超');
  $('flowAnalysis').textContent=`法人籌碼金額以每日淨買賣股數乘當日收盤價估算；短線看1日、波段轉折看5日、中期方向看20日。外資20日 ${direction(fl.foreign_20_amount)}，投信20日 ${direction(fl.trust_20_amount)}。`;

  const t=d.technical||{}; $('techPill').textContent=t.trend||'資料不足';
  if(candleViewState.ticker!==d.ticker){candleViewState={ticker:d.ticker,size:(t.series||[]).length,end:(t.series||[]).length}}
  $('priceChart').innerHTML=technicalDashboard(t);
  bindCandleTooltip(candleWindow(t.series||[]));
  bindCandleZoomPan(t.series||[]);
  $('levels').innerHTML=[['MA20',t.ma?.['20']],['MA60',t.ma?.['60']],['第一支撐',t.support1],['60日壓力',t.resistance],['KD K',t.k],['KD D',t.d],['RSI14',t.rsi14],['MACD Hist',t.macd_hist]].map(x=>`<div class="level"><span>${x[0]}</span><b>${fmt(x[1],x[0].includes('MACD')?2:1)}</b></div>`).join('');
  $('techAnalysis').textContent=`近一年日K；MA60 為中期趨勢核心。趨勢：${t.trend||'—'}；K/D ${fmt(t.k,1)}/${fmt(t.d,1)}；MACD Hist ${fmt(t.macd_hist,2)}；RSI14 ${fmt(t.rsi14,1)}。KD >80 / <20、RSI >70 / <30 僅代表動能極端，需搭配均線與量價確認。`;

  const rr=d.research||{}; $('reportCount').textContent=rr.count||0; $('consensusText').textContent=rr.median_target?`目標價中位數 ${fmt0(rr.median_target)} · 平均 ${fmt0(rr.average_target)}`:'目前尚無可解析的法人目標價共識'; $('revisionText').textContent=rr.forward_eps_year?`${rr.forward_eps_year}E EPS 中位數 ${fmt(rr.median_forward_eps,2)}（${rr.eps_coverage||0} 筆明確年度預估）`:(rr.target_revision_pct!=null?`同機構目標價修正中位數 ${pct(rr.target_revision_pct)}`:'Forward EPS：缺乏可比年度標註，不納入估值');
  if($('consensusStats')) $('consensusStats').innerHTML=[['法人機構',rr.institution_count||0],['最高目標',fmt0(rr.high_target)],['最低目標',fmt0(rr.low_target)],['買進/正向',rr.ratings?.['買進']||0],['中立',rr.ratings?.['中立']||0],['公開網路',rr.public_web_count||0]].map(x=>`<div><span>${x[0]}</span><b>${x[1]}</b></div>`).join('');
  $('analystTable').innerHTML=(rr.reports||[]).length?`<table class="clean-table analyst-web-table"><thead><tr><th>法人/券商</th><th>日期</th><th>評等</th><th>目標價</th><th>來源/標題</th><th>可信度</th></tr></thead><tbody>${rr.reports.map(x=>`<tr><td>${x.institution||'—'}</td><td>${x.report_date||'—'}</td><td>${x.rating||'—'}</td><td>${fmt0(x.target_price)}</td><td>${x.source_url?`<a href="${x.source_url}" target="_blank" rel="noopener noreferrer">${x.title||x.publisher||'查看來源'}</a>`:(x.title||x.publisher||'自行匯入')}</td><td>${x.confidence!=null?`${x.confidence}/100`:'—'}</td></tr>`).join('')}</tbody></table>`:'<div class="empty bordered">目前尚未搜尋到可解析的公開法人研究引用。可按「強制刷新」重新搜尋最新網路資料。</div>';

  const ce=d.company_events||{};
  const calls=(ce.earnings_calls||[]).slice(0,3);
  const material=(ce.material_info||[]);
  if($('earningsCallCount')) $('earningsCallCount').textContent=calls.length;
  if($('officialCallCount')) $('officialCallCount').textContent=`官方 ${ce.official_call_count||0}`;
  if($('materialInfoCount')) $('materialInfoCount').textContent=material.length;
  if($('earningsCallList')) $('earningsCallList').innerHTML=calls.length?calls.map(earningsCallCard).join(''):'<div class="empty bordered">目前尚未取得最近法說會的公開摘要。</div>';
  if($('materialInfoList')) $('materialInfoList').innerHTML=material.length?material.map(materialInfoCard).join(''):'<div class="empty bordered">目前沒有可辨識的近期重大訊息。</div>';

const ex=d.expectation_gap||{};
  if($('expectationRegime')) $('expectationRegime').textContent=ex.regime||'資料不足';
  if($('expectationSummary')) $('expectationSummary').textContent=ex.summary||'目前無法形成預期差判斷。';
  if($('revisionScore')) $('revisionScore').textContent=ex.revision_score??'—';
  if($('expectationMethod')) $('expectationMethod').textContent=ex.methodology||'';
  if($('expectationSignals')) $('expectationSignals').innerHTML=(ex.signals||[]).map(x=>`<div class="expect-signal ${x.direction||'flat'}"><span>${x.name}</span><b>${x.display||'—'}</b></div>`).join('');
  if($('revisionTable')) $('revisionTable').innerHTML=(ex.institution_revisions||[]).length?`<table class="clean-table"><thead><tr><th>法人</th><th>前次 → 最新</th><th>EPS 修正</th><th>目標價修正</th><th>最新目標</th></tr></thead><tbody>${ex.institution_revisions.map(x=>`<tr><td>${x.institution||'—'}</td><td>${x.previous_date||'—'} → ${x.latest_date||'—'}</td><td>${pct(x.eps_revision_pct)}</td><td>${pct(x.target_revision_pct)}</td><td>${fmt0(x.latest_target)}</td></tr>`).join('')}</tbody></table>`:'<div class="empty bordered">目前沒有足夠的同機構前後研究資料可比較。</div>';
  renderSnapshotCompare(d);


  const rp=d.research_pipeline||{}, db=rp.data_boundary||{};
  if($('pipelineView')) $('pipelineView').innerHTML=`<div class="pipeline-stance"><b>${rp.stance||'—'}</b><span>Research Score ${rp.research_score??'—'}</span></div><p>${rp.investment_view||'目前資料不足以形成完整研究結論。'}</p><div class="evidence-mini"><span>Fact ${rp.evidence_counts?.facts??0}</span><span>Derived ${rp.evidence_counts?.derived_facts??0}</span><span>Estimate ${rp.evidence_counts?.estimates??0}</span></div>`;
  if($('dataBoundary')) $('dataBoundary').innerHTML=`<div class="boundary-grade grade-${db.grade||'D'}"><b>${db.grade||'—'}</b><span>Data Boundary Grade</span></div><p>${db.message||'—'}</p>`;
  if($('actionConditions')) $('actionConditions').innerHTML=(rp.action_conditions||[]).map(x=>`<div class="condition-row"><b>${x.condition}</b><small>${x.meaning||''}</small></div>`).join('')||'<div class="empty">目前沒有足夠條件資料。</div>';
  const mcp=d.twstock_mcp||{};
  if($('mcpCrosscheck')) {
    const mrecs=mcp.records||[];
    const mcpLabel={ok:'已完成',pending:'背景準備中',degraded:'暫無可比 Evidence',error:'選用來源暫不可用',timeout:'選用來源逾時',disabled:'未啟用'}[mcp.status]||'選用來源暫不可用';
    const mcpNote=mcp.message||((mcp.status==='ok')?'僅作第二來源交叉驗證。':'此為選用二次驗證，不影響官方核心資料評級。');
    $('mcpCrosscheck').innerHTML=`<div class="mcp-crosscheck ${mcp.status||'optional'}"><div><b>TWStock MCP 二次驗證（選用）</b><span>${mcpLabel}</span></div><small>工具 ${mcp.tool_count??0} · 成功 ${mcp.successful_calls??0} · Evidence ${mrecs.length}</small><small>${mcpNote}</small>${mrecs.length?`<div class="mcp-evidence-mini">${mrecs.slice(0,6).map(x=>`<span>${x.metric}: ${fmt(x.value,2)}</span>`).join('')}</div>`:''}<a href="/api/diagnostics/mcp/${encodeURIComponent(d.ticker)}" target="_blank" rel="noopener noreferrer">查看 MCP 診斷</a></div>`;
  }
  if($('invalidationConditions')) $('invalidationConditions').innerHTML=(rp.invalidation_conditions||[]).map(x=>`<div class="condition-row invalid"><b>${x}</b></div>`).join('');
  $('valuationBody').innerHTML=scenarios.length?scenarios.map(x=>`<tr><td>${x.name}</td><td>${fmt(x.eps,2)}</td><td>${fmt(x.pe,1)}x</td><td><b>${fmt0(x.target)}</b></td><td>${pct(x.upside_pct)}</td></tr>`).join(''):'<tr><td colspan="5">估值資料不足</td></tr>';
  $('assumptions').innerHTML=`<div class="assumption-row"><b>EPS Basis</b><span>${d.valuation.eps_basis||'—'}</span></div><div class="assumption-row"><b>PE Basis</b><span>${d.valuation.pe_basis||'—'}</span></div><div class="assumption-row"><b>估值信心</b><span>${d.valuation.confidence||0}/100</span></div><div class="assumption-row"><b>模型原則</b><span>Bear/Base/Bull 對 EPS 與 PE 同時做情境化，而非單點預測。</span></div>`; $('peBand').innerHTML=`歷史 PER：P25 <b>${fmt(d.per?.pe_p25,1)}x</b> · Median <b>${fmt(d.per?.pe_median,1)}x</b> · P75 <b>${fmt(d.per?.pe_p75,1)}x</b>`;

  const base=scenarios.find(x=>x.name==='基準');
  $('strategyGrid').innerHTML=[
    ['趨勢支撐',fmt(t.support1,1),'觀察量價是否止穩'],['深度支撐',fmt(t.support2,1),'跌破需重估趨勢'],['壓力區',fmt(t.resistance,1),'突破需成交量確認'],['模型基準價',base?fmt0(base.target):'—','估值情境，不是保證價'],['52週區間',`${fmt(t.low_52w,1)}–${fmt(t.high_52w,1)}`,'理解目前價格位置']
  ].map(x=>`<div class="strategy"><span>${x[0]}</span><b>${x[1]}</b><small>${x[2]}</small></div>`).join('');

  $('catalystList').innerHTML=(d.catalysts||[]).map(x=>`<li>${x}</li>`).join(''); $('riskList').innerHTML=(d.risks||[]).map(x=>`<li>${x}</li>`).join('');
  $('freshnessStrip').innerHTML=d.source_status.map(x=>`<div class="fresh ${x.status}"><span>${x.name}</span><b>${x.as_of||'缺資料'}</b></div>`).join('');
  $('sourceTable').innerHTML=`<table class="clean-table"><thead><tr><th>資料</th><th>Dataset</th><th>最新資料日</th><th>預定更新</th><th>狀態</th></tr></thead><tbody>${d.source_status.map(x=>`<tr><td>${x.name}</td><td>${x.dataset}</td><td>${x.as_of||'—'}</td><td>${x.scheduled_update}</td><td>${x.status==='ok'?'可用':x.status==='warming'?'歷史補齊中':x.status==='stale'?'STALE / 已降權':x.status==='optional'?'選用 / 不影響評級':'缺資料'}</td></tr>`).join('')}</tbody></table>`;
  wrapWideTables();
}

async function readApiResponse(response){
  const contentType=(response.headers.get('content-type')||'').toLowerCase();
  const raw=await response.text();
  if(!raw) return {};
  if(contentType.includes('application/json')){
    try{return JSON.parse(raw);}catch(_){throw new Error('伺服器回傳的 JSON 格式不正確，請重新整理後再試。');}
  }
  // Render/proxy errors can return HTML. Show a useful message instead of Safari's vague SyntaxError.
  const plain=raw.replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim();
  throw new Error(plain.slice(0,180)||`伺服器回傳非 JSON 資料（HTTP ${response.status}）`);
}

async function loadTicker(ticker, force=false){
  ticker=String(ticker||'').trim().toUpperCase();
  const requestId=++stockRequestSequence;
  currentTicker=ticker;
  clearTimeout(marginHistoryRefreshTimer);
  $('errorBox').classList.add('hidden');
  if(!/^[0-9A-Z.-]{2,12}$/.test(ticker)){
    $('report').classList.add('hidden');
    $('errorBox').textContent='股票代號格式不正確，請輸入例如 2330、3661。';
    $('errorBox').classList.remove('hidden');
    return;
  }
  $('loading').classList.remove('hidden'); $('searchBtn').disabled=true; $('pdfBtn').disabled=true; $('dockPdf').disabled=true; $('dockShare').disabled=true;
  try{
    const path=`/api/stock/${encodeURIComponent(ticker)}${force?'?refresh=true':''}`;
    const r=await fetch(path,{method:'GET',headers:{'Accept':'application/json'},cache:force?'no-store':'default'});
    const j=await readApiResponse(r);
    if(requestId!==stockRequestSequence)return;
    if(!r.ok) throw new Error(j.detail||j.message||`資料取得失敗（HTTP ${r.status}）`);
    try{render(j);}catch(renderError){
      console.error('render failed',renderError);
      throw new Error(`畫面產生失敗：${renderError?.message||'未知錯誤'}`);
    }
  }catch(e){
    if(requestId!==stockRequestSequence)return;
    console.error('loadTicker failed',e);
    $('report').classList.add('hidden');
    $('errorBox').textContent=e?.message||'資料取得失敗，請稍後再試。';
    $('errorBox').classList.remove('hidden');
  } finally {if(requestId===stockRequestSequence){$('loading').classList.add('hidden'); $('searchBtn').disabled=false;}}
}
$('searchBtn').onclick=()=>loadTicker($('tickerInput').value.trim()); $('refreshBtn').onclick=()=>loadTicker($('tickerInput').value.trim(), true);
$('tickerInput').addEventListener('keydown',e=>{if(e.key==='Enter')loadTicker(e.target.value.trim())});
$('pdfBtn').onclick=()=>{if(currentTicker) window.location.href=`/api/stock/${currentTicker}/pdf`;};
$('methodBtn').onclick=()=>$('methodModal').classList.add('open'); $('closeModal').onclick=()=>$('methodModal').classList.remove('open');
window.addEventListener('load',()=>{const q=new URLSearchParams(location.search).get('ticker'); if(q) $('tickerInput').value=q; loadTicker($('tickerInput').value.trim());});

// V4 Cloud / PWA mobile enhancements
let deferredInstallPrompt = null;
function wrapWideTables(){
  document.querySelectorAll('.clean-table').forEach(table=>{
    const labels=Array.from(table.querySelectorAll('thead th')).map(x=>x.textContent.trim());
    table.querySelectorAll('tbody tr').forEach(row=>Array.from(row.children).forEach((cell,index)=>{
      if(cell.tagName==='TD' && labels[index]) cell.dataset.label=labels[index];
    }));
    if(!table.parentElement.classList.contains('table-scroll')){
      const wrap=document.createElement('div'); wrap.className='table-scroll';
      table.parentNode.insertBefore(wrap,table); wrap.appendChild(table);
    }
  });
  setupMobileFolds();
  setupMobileDetails();
}
function setupMobileFolds(){
  document.querySelectorAll('.company-disclosure-section,.expectation-section,.research-pipeline-section,.valuation-section,.strategy-section,.source-section').forEach(section=>{
    if(section.dataset.foldReady)return;
    section.dataset.foldReady='1'; section.classList.add('mobile-fold','is-collapsed');
    const title=section.querySelector(':scope > .section-title');
    if(!title)return;
    const button=document.createElement('button');
    button.type='button'; button.className='mobile-fold-toggle'; button.setAttribute('aria-expanded','false'); button.textContent='展開';
    button.onclick=()=>{const collapsed=section.classList.toggle('is-collapsed');button.textContent=collapsed?'展開':'收合';button.setAttribute('aria-expanded',String(!collapsed));};
    title.appendChild(button);
  });
}
function setupMobileDetails(){
  [['#analystTable','公開法人研究明細'],['.eps-ledger','EPS 證據明細'],['.evidence-matrix','Evidence 矩陣']].forEach(([selector,label])=>{
    const detail=document.querySelector(selector); if(!detail||detail.dataset.mobileDetailReady)return;
    detail.dataset.mobileDetailReady='1'; detail.classList.add('mobile-detail','is-collapsed');
    const button=document.createElement('button'); button.type='button'; button.className='mobile-detail-toggle'; button.textContent=`展開${label}`; button.setAttribute('aria-expanded','false');
    button.onclick=()=>{const collapsed=detail.classList.toggle('is-collapsed');button.textContent=`${collapsed?'展開':'收合'}${label}`;button.setAttribute('aria-expanded',String(!collapsed));};
    detail.parentNode.insertBefore(button,detail);
  });
}
async function checkCloud(){
  const dot=document.querySelector('.status-dot');
  try{
    const r=await fetch('/health',{cache:'no-store'}); const j=await r.json();
    if(!r.ok) throw new Error();
    dot?.classList.remove('offline'); dot?.classList.add('online');
    $('cloudStatus').textContent='雲端服務正常';
    if(j.version) document.querySelectorAll('[data-app-version]').forEach(el=>{el.textContent=`V${j.version}`});
  }catch(e){
    dot?.classList.remove('online'); dot?.classList.add('offline');
    $('cloudStatus').textContent='雲端服務目前無法連線';
  }
}
function openInstallHelp(){
  if(deferredInstallPrompt){deferredInstallPrompt.prompt(); deferredInstallPrompt=null; return;}
  $('installSheet').classList.add('open');
}
window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();deferredInstallPrompt=e;});
$('installBtn').onclick=openInstallHelp;
$('closeInstall').onclick=()=>$('installSheet').classList.remove('open');
$('installOk').onclick=()=>$('installSheet').classList.remove('open');
$('dockSearch').onclick=()=>{document.querySelector('.search-card').scrollIntoView({behavior:'smooth',block:'start'});setTimeout(()=>$('tickerInput').focus(),300)};
$('dockRefresh').onclick=()=>currentTicker&&loadTicker(currentTicker,true);
$('dockPdf').onclick=()=>currentTicker&&(window.location.href=`/api/stock/${currentTicker}/pdf?refresh=true`);
$('dockShare').onclick=async()=>{
  if(!currentTicker)return;
  let shareUrl=`${location.origin}${location.pathname}?ticker=${encodeURIComponent(currentTicker)}`;
  const shareData={title:`${$('companyName').textContent} ${currentTicker} AI 研究報告`,text:'AI Stock Research Terminal 個股研究',url:shareUrl};
  try{if(navigator.share) await navigator.share(shareData); else {await navigator.clipboard.writeText(shareUrl); alert('研究連結已複製');}}catch(e){}
};
// Service workers were retired in V5.12.0. The server is the only shell/version source.
window.addEventListener('online',checkCloud); window.addEventListener('offline',checkCloud);
checkCloud();
