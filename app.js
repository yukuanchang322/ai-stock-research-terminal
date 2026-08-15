const $=id=>document.getElementById(id);
const fmt=(v,d=1)=>v==null?'—':Number(v).toLocaleString('zh-TW',{maximumFractionDigits:d,minimumFractionDigits:d});
const fmt0=v=>v==null?'—':Number(v).toLocaleString('zh-TW',{maximumFractionDigits:0});
const pct=(v,d=1)=>v==null?'—':`${v>=0?'+':''}${Number(v).toFixed(d)}%`;
let currentTicker='';

function lineSvg(values){
  if(!values.length)return '<div class="empty">資料不足</div>';
  const w=560,h=170,p=16,min=Math.min(...values),max=Math.max(...values),range=max-min||1;
  const pts=values.map((v,i)=>`${p+i*(w-2*p)/(Math.max(1,values.length-1))},${h-p-(v-min)*(h-2*p)/range}`).join(' ');
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line x1="${p}" y1="${h-p}" x2="${w-p}" y2="${h-p}" stroke="#304653"/><polyline points="${pts}" fill="none" stroke="#4ce0b3" stroke-width="3" vector-effect="non-scaling-stroke"/></svg>`;
}

function flowMatrix(fl){
  const rows=[['外資','foreign'],['投信','trust'],['自營商','dealer']];
  const signed=v=>v==null?'—':`${Number(v)>0?'+':''}${fmt0(v)}`;
  return `<div class="flow-matrix"><div class="flow-head"><b>法人</b><b>1日</b><b>5日</b><b>20日</b></div>${rows.map(([label,key])=>`<div class="flow-matrix-row"><b>${label}</b>${[1,5,20].map(n=>{const v=fl[`${key}_${n}`];return `<span class="${v<0?'neg':'pos'}">${signed(v)}</span>`}).join('')}</div>`).join('')}<div class="flow-matrix-row margin-row"><b>融資%</b>${[1,5,20].map(n=>{const v=fl[`margin_${n}_pct`];return `<span class="${v>0?'neg':'pos'}">${v==null?'—':`${v>0?'+':''}${fmt(v,1)}%`}</span>`}).join('')}</div></div>`;
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
  const w=720,h=300,p=24, vals=data.flatMap(x=>[x.high,x.low,x.ma20,x.ma60].filter(v=>v!=null).map(Number));
  const min=Math.min(...vals),max=Math.max(...vals),range=max-min||1, xstep=(w-2*p)/Math.max(1,data.length);
  const y=v=>h-p-(Number(v)-min)*(h-2*p)/range;
  const body=Math.max(1,Math.min(5,xstep*.66));
  const candles=data.map((x,i)=>{const cx=p+(i+.5)*xstep, up=Number(x.close)>=Number(x.open), cls=up?'up':'down';
    const yo=y(x.open),yc=y(x.close),yh=y(x.high),yl=y(x.low),top=Math.min(yo,yc),bh=Math.max(1,Math.abs(yc-yo));
    return `<g class="candle ${cls}"><line x1="${cx}" y1="${yh}" x2="${cx}" y2="${yl}"/><rect x="${cx-body/2}" y="${top}" width="${body}" height="${bh}"/></g>`}).join('');
  const p20=_techLinePoints(data,'ma20',w,h,p,min,max).map(points=>`<polyline class="ma20" points="${points}"/>`).join('');
  const p60=_techLinePoints(data,'ma60',w,h,p,min,max).map(points=>`<polyline class="ma60" points="${points}"/>`).join('');
  const first=data[0]?.date||'', last=data[data.length-1]?.date||'';
  return `<div class="tech-chart-title"><b>近一年日K</b><span>MA20 · MA60</span></div><svg class="candle-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line class="axis" x1="${p}" y1="${h-p}" x2="${w-p}" y2="${h-p}"/>${candles}${p20}${p60}<text x="${p}" y="${h-5}">${first}</text><text x="${w-p}" y="${h-5}" text-anchor="end">${last}</text></svg><div class="chart-legend"><span>MA20</span><span>MA60</span></div>`;
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
  return `<div class="technical-dashboard">${candleSvg(s)}${oscillatorSvg(s,['k','d'],'KD',0,100,[20,80])}${macdSvg(s)}${oscillatorSvg(s,['rsi14'],'RSI 14',0,100,[30,70])}</div>`;
}
function metric(k,v,note=''){return `<div class="metric"><span>${k}</span><b>${v}</b><em>${note}</em></div>`}
function targetRow(x){return `<div class="target-row ${x.name==='悲觀'?'bear':x.name==='樂觀'?'bull':'base'}"><span>${x.name}</span><b>${fmt0(x.target)}</b></div>`}

function snapshotKey(ticker){return `ai-stock-v5.2.12-snapshot-${ticker}`}
function currentSnapshot(d){
  return {generated_at:d.generated_at,score:d.scores?.['綜合'],price:d.price,median_target:d.research?.median_target,median_eps:d.research?.median_forward_eps,per:d.per?.per,foreign20:d.flow?.foreign_20,regime:d.expectation_gap?.regime};
}
function deltaText(now,old,digits=1,suffix=''){if(now==null||old==null)return '—';const delta=Number(now)-Number(old);return `${delta>=0?'+':''}${delta.toFixed(digits)}${suffix}`}
function renderSnapshotCompare(d){
  const key=snapshotKey(d.ticker), oldRaw=localStorage.getItem(key), now=currentSnapshot(d);
  let old=null; try{old=oldRaw?JSON.parse(oldRaw):null}catch(e){}
  const box=$('snapshotCompare');
  if(!box)return;
  if(!old){box.innerHTML='<div class="empty bordered">這是此裝置第一次保存這檔股票的分析；下次查詢時會自動顯示前後變化。</div>';}
  else{
    const rows=[['Research Score',old.score,now.score,deltaText(now.score,old.score,0,' 分')],['法人目標價中位數',old.median_target,now.median_target,deltaText(now.median_target,old.median_target,0,'')],['Forward EPS 中位數',old.median_eps,now.median_eps,deltaText(now.median_eps,old.median_eps,2,'')],['PER',old.per,now.per,deltaText(now.per,old.per,1,'x')],['外資20日',old.foreign20,now.foreign20,deltaText(now.foreign20,old.foreign20,0,'')]];
    box.innerHTML=`<div class="snapshot-head"><span>前次 ${old.generated_at?new Date(old.generated_at).toLocaleString('zh-TW'):'—'}</span><b>${old.regime||'—'} → ${now.regime||'—'}</b></div><div class="snapshot-grid">${rows.map(r=>`<div><span>${r[0]}</span><small>${r[1]==null?'—':fmt(r[1],r[0]==='Research Score'?0:1)} → ${r[2]==null?'—':fmt(r[2],r[0]==='Research Score'?0:1)}</small><b>${r[3]}</b></div>`).join('')}</div>`;
  }
  localStorage.setItem(key,JSON.stringify(now));
}

function render(d){
  currentTicker=d.ticker;
  $('report').classList.remove('hidden'); $('pdfBtn').disabled=false;
  $('generatedAt').textContent=new Date(d.generated_at).toLocaleString('zh-TW');
  $('companyName').textContent=d.name; $('tickerLabel').textContent=d.ticker; $('sector').textContent=d.industry; $('marketType').textContent=d.market_type;
  $('stanceTag').textContent=d.stance; $('confidenceScore').textContent=d.confidence?.overall ?? '—'; $('price').textContent=fmt(d.price,1); $('dayChange').textContent=pct(d.change_pct);
  $('thesis').textContent=d.thesis; $('dataPolicy').textContent=d.data_policy; $('overallScore').textContent=d.scores['綜合'];
  $('kpis').innerHTML=[['預期狀態',d.expectation_gap?.regime||'—','V5.2.15'],['Research Score',`${d.scores['綜合']}/100`,'量化綜合'],['可信度',`${d.confidence?.overall??'—'}/100`,'資料+估值'],['PER',`${fmt(d.per?.per,1)}x`,'最新可得'],['營收 YoY',pct(d.revenue?.revenue_yoy),'最新月'],['外資 1/5/20日',`${fmt0(d.flow?.foreign_1)} / ${fmt0(d.flow?.foreign_5)} / ${fmt0(d.flow?.foreign_20)}`,'淨買賣'],['RSI14',fmt(d.technical?.rsi14,1),'技術動能']].map(x=>`<div class="kpi"><span>${x[0]}</span><b>${x[1]}</b><small>${x[2]}</small></div>`).join('');
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
  const staleNote=fi.official_verified ? `✅ ${fi.message||'官方最新財報期已驗證'}` : `⚠ ${fi.message||'財報最新季度尚未通過官方驗證'} · ${diagLink}`;
  const perNote=fi.core_financials_allowed ? (p.last_date||'市場資料') : `${p.last_date||''} · 市場 PER 可顯示，但不代表財報已驗證`;
  $('fundamentalTable').innerHTML=[
    metric('最新月營收',fmt0(r.latest_revenue),r.revenue_period||''), metric('營收 YoY',pct(r.revenue_yoy),'年增率'),
    metric('單季 EPS',fmt(es.quarter_eps,2),`${es.quarter_period||'—'} · ${es.quarter_method_label||'資料不足'}`), metric('YTD EPS',fmt(es.ytd_eps,2),`${es.ytd_period||'—'} · ${es.ytd_method_label||''}`),
    metric('TTM EPS',fmt(es.ttm_eps,2),`${es.ttm_period||'—'} · ${es.ttm_method_label||''}`), metric('財報來源',f.source||es.source||'—',`${f.period||f.statement_date||''} · ${staleNote}`),
    metric('毛利率',pct(f.gross_margin),f.period||'最新財報期'), metric('營益率',pct(f.operating_margin),f.period||'最新財報期'), metric('PER / PBR',`${fmt(p.per,1)}x / ${fmt(p.pbr,1)}x`,perNote)
  ].join('');
  const ledger=(es.evidence_ledger||[]);
  const ledgerHtml=ledger.length?`<div class="eps-ledger"><h4>EPS Evidence Ledger</h4><div class="ledger-list">${ledger.map(x=>`<div class="ledger-row ${x.status==='usable'?'ok':'missing'}"><b>${x.period}</b><span>${x.quarter_eps_direct!=null?`單季 ${fmt(x.quarter_eps_direct,2)}`:(x.ytd_eps!=null?`YTD ${fmt(x.ytd_eps,2)}`:'缺資料')}</span><small>${x.source||x.missing_reason||'無官方證據'}${x.derived_quarter_eps!=null?` · 推導單季 ${fmt(x.derived_quarter_eps,2)}`:''}</small></div>`).join('')}</div></div>`:'';
  $('fundamentalTable').insertAdjacentHTML('afterend',ledgerHtml);
  const eg=d.evidence_graph||{}, esum=eg.summary||{};
  const evidenceHtml=`<div class="evidence-matrix"><h4>Multi-Source Evidence Matrix</h4><div class="evidence-stats"><div><span>Evidence</span><b>${esum.usable??0}</b></div><div><span>官方/驗證</span><b>${esum.official_or_verified??0}</b></div><div><span>Fact</span><b>${esum.facts??0}</b></div><div><span>Derived</span><b>${esum.derived_facts??0}</b></div><div><span>Estimate</span><b>${esum.estimates??0}</b></div><div><span>真正衝突</span><b>${esum.conflicts??0}</b></div><div><span>預估修正</span><b>${esum.estimate_revisions??0}</b></div><div><span>Evidence Score</span><b>${esum.evidence_score??0}</b></div></div>${(eg.conflicts||[]).length?`<div class="evidence-conflicts"><b>同定義來源衝突</b>${eg.conflicts.slice(0,4).map(c=>`<small>${c.metric} ${c.period||''} · spread ${c.spread_pct}%</small>`).join('')}</div>`:'<small class="evidence-ok">目前核心 Fact 未偵測到同期間、同定義的重大來源衝突。</small>'}${(eg.estimate_revisions||[]).length?`<div class="evidence-revisions"><b>Estimate Revision</b>${eg.estimate_revisions.slice(-4).map(r=>`<small>${r.metric} ${r.period||''} · ${r.revision_pct==null?'—':`${r.revision_pct>0?'+':''}${r.revision_pct}%`}</small>`).join('')}</div>`:''}<a class="diagnostic-link" href="/api/evidence/${encodeURIComponent(d.ticker)}" target="_blank" rel="noopener noreferrer">查看完整 Evidence JSON</a></div>`;
  document.querySelector('.eps-ledger')?.insertAdjacentHTML('afterend',evidenceHtml);
  $('fundChart').innerHTML=lineSvg((r.series||[]).map(x=>x.revenue));
  $('fundAnalysis').textContent=r.revenue_yoy==null?`營收年增資料不足。${staleNote}`:`最新月營收年增 ${pct(r.revenue_yoy)}；${fi.core_financials_allowed?`${es.quarter_period||'最新財報'} 單季 EPS ${fmt(es.quarter_eps,2)}、YTD EPS ${fmt(es.ytd_eps,2)}、TTM EPS ${fmt(es.ttm_eps,2)}。`:'財報 EPS 尚未通過最新季度閘門，不進核心估值。'} ${staleNote}`;

  const fl=d.flow||{};
  $('flowTable').innerHTML=flowMatrix(fl);
  const flows={外資:fl.foreign_20||0,投信:fl.trust_20||0,自營商:fl.dealer_20||0}, mx=Math.max(1,...Object.values(flows).map(Math.abs));
  $('flowBars').innerHTML=Object.entries(flows).map(([k,v])=>`<div class="flow-row"><span>${k} 20日</span><div class="flow-track"><div class="flow-fill ${v<0?'neg':''}" style="width:${Math.abs(v)/mx*100}%"></div></div><b>${v>0?'+':''}${fmt0(v)}</b></div>`).join('');
  $('flowAnalysis').textContent=`法人籌碼以 1日 / 5日 / 20日三個時間尺度判讀；短線看1日、波段轉折看5日、中期方向看20日。外資20日 ${fl.foreign_20>=0?'偏買超':'偏賣超'}，投信20日 ${fl.trust_20>=0?'偏買超':'偏賣超'}。`;

  const t=d.technical||{}; $('techPill').textContent=t.trend||'資料不足';
  $('priceChart').innerHTML=technicalDashboard(t);
  $('levels').innerHTML=[['MA20',t.ma?.['20']],['MA60',t.ma?.['60']],['第一支撐',t.support1],['60日壓力',t.resistance],['KD K',t.k],['KD D',t.d],['RSI14',t.rsi14],['MACD Hist',t.macd_hist]].map(x=>`<div class="level"><span>${x[0]}</span><b>${fmt(x[1],x[0].includes('MACD')?2:1)}</b></div>`).join('');
  $('techAnalysis').textContent=`近一年日K；MA60 為中期趨勢核心。趨勢：${t.trend||'—'}；K/D ${fmt(t.k,1)}/${fmt(t.d,1)}；MACD Hist ${fmt(t.macd_hist,2)}；RSI14 ${fmt(t.rsi14,1)}。KD >80 / <20、RSI >70 / <30 僅代表動能極端，需搭配均線與量價確認。`;

  const rr=d.research||{}; $('reportCount').textContent=rr.count||0; $('consensusText').textContent=rr.median_target?`目標價中位數 ${fmt0(rr.median_target)} · 平均 ${fmt0(rr.average_target)}`:'目前尚無可解析的法人目標價共識'; $('revisionText').textContent=rr.forward_eps_year?`${rr.forward_eps_year}E EPS 中位數 ${fmt(rr.median_forward_eps,2)}（${rr.eps_coverage||0} 筆明確年度預估）`:(rr.target_revision_pct!=null?`同機構目標價修正中位數 ${pct(rr.target_revision_pct)}`:'Forward EPS：缺乏可比年度標註，不納入估值');
  if($('consensusStats')) $('consensusStats').innerHTML=[['法人機構',rr.institution_count||0],['最高目標',fmt0(rr.high_target)],['最低目標',fmt0(rr.low_target)],['買進/正向',rr.ratings?.['買進']||0],['中立',rr.ratings?.['中立']||0],['公開網路',rr.public_web_count||0]].map(x=>`<div><span>${x[0]}</span><b>${x[1]}</b></div>`).join('');
  $('analystTable').innerHTML=(rr.reports||[]).length?`<table class="clean-table analyst-web-table"><thead><tr><th>法人/券商</th><th>日期</th><th>評等</th><th>目標價</th><th>來源/標題</th><th>可信度</th></tr></thead><tbody>${rr.reports.map(x=>`<tr><td>${x.institution||'—'}</td><td>${x.report_date||'—'}</td><td>${x.rating||'—'}</td><td>${fmt0(x.target_price)}</td><td>${x.source_url?`<a href="${x.source_url}" target="_blank" rel="noopener noreferrer">${x.title||x.publisher||'查看來源'}</a>`:(x.title||x.publisher||'自行匯入')}</td><td>${x.confidence!=null?`${x.confidence}/100`:'—'}</td></tr>`).join('')}</tbody></table>`:'<div class="empty bordered">目前尚未搜尋到可解析的公開法人研究引用。可按「強制刷新」重新搜尋最新網路資料。</div>';

  const ev=d.company_events?.rows||[]; if($('eventRadar')) $('eventRadar').innerHTML=ev.length?`<div class="event-list">${ev.map(x=>`<article class="event-item"><div><time>${x.date||'—'}</time><div class="event-tags">${(x.tags||[]).map(t=>`<span>${t}</span>`).join('')}</div></div><div><a href="${x.source_url||'#'}" target="_blank" rel="noopener noreferrer">${x.title||'—'}</a><small>${x.publisher||'公開來源'}</small></div></article>`).join('')}</div>`:'<div class="empty bordered">目前尚未搜尋到近期法說或重大訊息引用。</div>';

  const ex=d.expectation_gap||{};
  if($('expectationRegime')) $('expectationRegime').textContent=ex.regime||'資料不足';
  if($('expectationSummary')) $('expectationSummary').textContent=ex.summary||'目前無法形成預期差判斷。';
  if($('revisionScore')) $('revisionScore').textContent=ex.revision_score??'—';
  if($('expectationMethod')) $('expectationMethod').textContent=ex.methodology||'';
  if($('expectationSignals')) $('expectationSignals').innerHTML=(ex.signals||[]).map(x=>`<div class="expect-signal ${x.direction||'flat'}"><span>${x.name}</span><b>${x.display||'—'}</b></div>`).join('');
  if($('revisionTable')) $('revisionTable').innerHTML=(ex.institution_revisions||[]).length?`<table class="clean-table"><thead><tr><th>法人</th><th>前次 → 最新</th><th>EPS 修正</th><th>目標價修正</th><th>最新目標</th></tr></thead><tbody>${ex.institution_revisions.map(x=>`<tr><td>${x.institution||'—'}</td><td>${x.previous_date||'—'} → ${x.latest_date||'—'}</td><td>${pct(x.eps_revision_pct)}</td><td>${pct(x.target_revision_pct)}</td><td>${fmt0(x.latest_target)}</td></tr>`).join('')}</tbody></table>`:'<div class="empty bordered">目前沒有足夠的同機構前後研究資料可比較。</div>';
  renderSnapshotCompare(d);

  $('valuationBody').innerHTML=scenarios.length?scenarios.map(x=>`<tr><td>${x.name}</td><td>${fmt(x.eps,2)}</td><td>${fmt(x.pe,1)}x</td><td><b>${fmt0(x.target)}</b></td><td>${pct(x.upside_pct)}</td></tr>`).join(''):'<tr><td colspan="5">估值資料不足</td></tr>';
  $('assumptions').innerHTML=`<div class="assumption-row"><b>EPS Basis</b><span>${d.valuation.eps_basis||'—'}</span></div><div class="assumption-row"><b>PE Basis</b><span>${d.valuation.pe_basis||'—'}</span></div><div class="assumption-row"><b>估值信心</b><span>${d.valuation.confidence||0}/100</span></div><div class="assumption-row"><b>模型原則</b><span>Bear/Base/Bull 對 EPS 與 PE 同時做情境化，而非單點預測。</span></div>`; $('peBand').innerHTML=`歷史 PER：P25 <b>${fmt(d.per?.pe_p25,1)}x</b> · Median <b>${fmt(d.per?.pe_median,1)}x</b> · P75 <b>${fmt(d.per?.pe_p75,1)}x</b>`;

  const base=scenarios.find(x=>x.name==='基準');
  $('strategyGrid').innerHTML=[
    ['趨勢支撐',fmt(t.support1,1),'觀察量價是否止穩'],['深度支撐',fmt(t.support2,1),'跌破需重估趨勢'],['壓力區',fmt(t.resistance,1),'突破需成交量確認'],['模型基準價',base?fmt0(base.target):'—','估值情境，不是保證價'],['52週區間',`${fmt(t.low_52w,1)}–${fmt(t.high_52w,1)}`,'理解目前價格位置']
  ].map(x=>`<div class="strategy"><span>${x[0]}</span><b>${x[1]}</b><small>${x[2]}</small></div>`).join('');

  $('catalystList').innerHTML=(d.catalysts||[]).map(x=>`<li>${x}</li>`).join(''); $('riskList').innerHTML=(d.risks||[]).map(x=>`<li>${x}</li>`).join('');
  $('freshnessStrip').innerHTML=d.source_status.map(x=>`<div class="fresh ${x.status}"><span>${x.name}</span><b>${x.as_of||'缺資料'}</b></div>`).join('');
  $('sourceTable').innerHTML=`<table class="clean-table"><thead><tr><th>資料</th><th>Dataset</th><th>最新資料日</th><th>預定更新</th><th>狀態</th></tr></thead><tbody>${d.source_status.map(x=>`<tr><td>${x.name}</td><td>${x.dataset}</td><td>${x.as_of||'—'}</td><td>${x.scheduled_update}</td><td>${x.status==='ok'?'可用':x.status==='stale'?'STALE / 已降權':'缺資料'}</td></tr>`).join('')}</tbody></table>`;
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
    if(!r.ok) throw new Error(j.detail||j.message||`資料取得失敗（HTTP ${r.status}）`);
    try{render(j);}catch(renderError){
      console.error('render failed',renderError);
      throw new Error(`畫面產生失敗：${renderError?.message||'未知錯誤'}`);
    }
  }catch(e){
    console.error('loadTicker failed',e);
    $('report').classList.add('hidden');
    $('errorBox').textContent=e?.message||'資料取得失敗，請稍後再試。';
    $('errorBox').classList.remove('hidden');
  } finally {$('loading').classList.add('hidden'); $('searchBtn').disabled=false;}
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
    if(!table.parentElement.classList.contains('table-scroll')){
      const wrap=document.createElement('div'); wrap.className='table-scroll';
      table.parentNode.insertBefore(wrap,table); wrap.appendChild(table);
    }
  });
}
async function checkCloud(){
  const dot=document.querySelector('.status-dot');
  try{
    const r=await fetch('/health',{cache:'no-store'}); const j=await r.json();
    if(!r.ok) throw new Error();
    dot?.classList.remove('offline'); dot?.classList.add('online');
    $('cloudStatus').textContent=`雲端服務正常 · V${j.version||'5'}`;
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
if('serviceWorker' in navigator){window.addEventListener('load',()=>navigator.serviceWorker.register('/sw.js').catch(()=>{}));}
window.addEventListener('online',checkCloud); window.addEventListener('offline',checkCloud);
checkCloud();
