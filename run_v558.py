"""V5.5.8 Margin/Short Dual Chart UI.

Adds the requested financing/short-selling presentation:
- red/green daily balance-change bars,
- orange balance line on the right axis,
- latest balance plus 1/5/20 trading-day absolute changes,
while preserving institutional cash-flow tables and the V5.5.7 revenue amount bars.
"""
from __future__ import annotations
import asyncio
from typing import Any
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
import run_v557, run_v551, server

VERSION="5.5.8"
server.app.version=VERSION
_base_build=server.build_stock


def _num(v:Any):
    try:
        if v is None:return None
        s=str(v).replace(',','').replace('--','').strip()
        return float(s) if s else None
    except:return None


def _chg_abs(hist,n,key):
    if len(hist)<=n:return None
    now=_num(hist[-1].get(key)); old=_num(hist[-1-n].get(key))
    return None if now is None or old is None else now-old

async def _margin_history(ticker:str,d:dict[str,Any]):
    tech=[x for x in (d.get('technical') or {}).get('series',[]) if x.get('date')][-70:]
    if len(tech)<2:return d
    dates=[str(x['date']) for x in tech[-60:]]
    sem=asyncio.Semaphore(3)
    async with httpx.AsyncClient(follow_redirects=True,headers={'User-Agent':'Mozilla/5.0 AI-Stock-Research/5.5.8'}) as c:
        async def one(ds):
            async with sem:
                return await run_v557._margin_day(c,ticker,ds)
        rows=await asyncio.gather(*(one(ds) for ds in dates))
    hist=sorted([x for x in rows if x and (_num(x.get('margin_balance')) is not None or _num(x.get('short_balance')) is not None)],key=lambda x:x['date'])
    if len(hist)<2:return d
    # Attach day-over-day deltas for chart bars.
    out=[]
    prev_m=prev_s=None
    for x in hist:
        mb=_num(x.get('margin_balance')); sb=_num(x.get('short_balance'))
        out.append({'date':x['date'],'margin_balance':mb,'short_balance':sb,
                    'margin_change':None if prev_m is None or mb is None else mb-prev_m,
                    'short_change':None if prev_s is None or sb is None else sb-prev_s})
        if mb is not None: prev_m=mb
        if sb is not None: prev_s=sb
    cf=d.get('cashflow') or {}
    cf['margin_history']=out
    cf['margin_short_rows']=len(out)
    cf['margin_short_as_of']=out[-1]['date']
    cf['margin_short_abs']={str(n):{'margin_change':_chg_abs(out,n,'margin_balance'),'short_change':_chg_abs(out,n,'short_balance')} for n in (1,5,20)}
    cf['margin_balance']=out[-1].get('margin_balance')
    cf['short_balance']=out[-1].get('short_balance')
    d['cashflow']=cf
    return d

async def build_stock_v558(ticker:str,force_refresh:bool=False):
    d=await _base_build(ticker,force_refresh=force_refresh)
    if isinstance(d,dict):
        try:d=await _margin_history(ticker,d)
        except Exception:pass
        d['version']=VERSION
        d['data_policy']=(d.get('data_policy') or '')+' V5.5.8：融資融券改為日增減柱＋餘額線雙軸圖，並顯示最新餘額與 1/5/20 日張數變化。'
    return d
server.build_stock=build_stock_v558

_UI=r'''
function v558FmtLots(v){if(v==null||!Number.isFinite(Number(v)))return '—';return `${Math.round(Number(v)).toLocaleString('zh-TW')} 張`;}
function v558SignedLots(v){if(v==null||!Number.isFinite(Number(v)))return '<span>—</span>';const n=Math.round(Number(v));return `<span class="${n>0?'neg':n<0?'pos':''}">${n>0?'+':''}${n.toLocaleString('zh-TW')} 張</span>`;}
function v558DualChart(hist,balanceKey,changeKey,title,icon){
 const data=(hist||[]).filter(x=>x&&x.date&&Number.isFinite(Number(x[balanceKey]))).slice(-60);
 if(data.length<2)return `<div class="margin-card"><div class="empty">${title}歷史資料不足</div></div>`;
 const w=760,h=260,pl=40,pr=62,pt=22,pb=42,changes=data.map(x=>Number(x[changeKey]||0)),balances=data.map(x=>Number(x[balanceKey])),cmax=Math.max(1,...changes.map(x=>Math.abs(x))),bmin=Math.min(...balances),bmax=Math.max(...balances),bspan=(bmax-bmin)||1,zero=pt+(h-pt-pb)/2,step=(w-pl-pr)/data.length,bw=Math.max(2,step*.58),barScale=(h-pt-pb)/2/cmax;
 const bars=data.map((x,i)=>{const v=Number(x[changeKey]||0),cx=pl+(i+.5)*step,bh=Math.abs(v)*barScale,y=v>=0?zero-bh:zero;return `<rect class="ms-bar ${v>=0?'up':'down'}" x="${cx-bw/2}" y="${y}" width="${bw}" height="${Math.max(1,bh)}"><title>${x.date} ${v>=0?'+':''}${Math.round(v)} 張</title></rect>`}).join('');
 const pts=data.map((x,i)=>{const cx=pl+(i+.5)*step,y=pt+(bmax-Number(x[balanceKey]))*(h-pt-pb)/bspan;return `${cx},${y}`}).join(' ');
 const first=data[0].date,last=data[data.length-1].date,mid=data[Math.floor(data.length/2)].date;
 return `<div class="margin-chart-box"><div class="ms-legend"><span class="bar-key"></span>${title}增減 <span class="line-key"></span>${title}餘額(右軸)</div><svg class="ms-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line class="ms-zero" x1="${pl}" y1="${zero}" x2="${w-pr}" y2="${zero}"/>${bars}<polyline class="ms-line" points="${pts}" fill="none"/><text x="${w-pr+8}" y="${pt+8}" class="ms-axis">${Math.round(bmax).toLocaleString('zh-TW')}</text><text x="${w-pr+8}" y="${h-pb}" class="ms-axis">${Math.round(bmin).toLocaleString('zh-TW')}</text><text x="${pl}" y="${h-12}" class="ms-date">${first.slice(5).replace('-','/')}</text><text x="${w/2}" y="${h-12}" text-anchor="middle" class="ms-date">${mid.slice(5).replace('-','/')}</text><text x="${w-pr}" y="${h-12}" text-anchor="end" class="ms-date">${last.slice(5).replace('-','/')}</text></svg><small class="chart-note">紅柱＝增加；綠柱＝減少；橘線＝${title}餘額（右軸）。</small></div>`;
}
function v558MarginCards(cf){
 const hist=cf?.margin_history||[], a=cf?.margin_short_abs||{};
 const card=(title,icon,balanceKey,changeKey)=>`<div class="margin-card"><div class="margin-card-head"><div class="margin-title"><span class="margin-icon">${icon}</span><b>${title}餘額</b><span>餘額 <strong>${v558FmtLots(cf?.[balanceKey])}</strong></span></div><div class="margin-periods"><span>1日 ${v558SignedLots(a?.['1']?.[changeKey])}</span><span>5日 ${v558SignedLots(a?.['5']?.[changeKey])}</span><span>20日 ${v558SignedLots(a?.['20']?.[changeKey])}</span></div></div>${v558DualChart(hist,balanceKey,changeKey,title,icon)}</div>`;
 return `<div class="margin-dual-wrap">${card('融資','資','margin_balance','margin_change')}${card('融券','券','short_balance','short_change')}<div class="margin-info">ⓘ 融資融券資料每日收盤後更新，單位為張。1張＝1,000股。</div></div>`;
}
function v558Cash(cf){
 const inst=cf?.institutional||{},labs=[['外資','foreign'],['投信','trust'],['自營商','dealer']],money=v=>{if(v==null||!Number.isFinite(Number(v)))return '—';const n=Number(v)/1e8;return `${n>0?'+':''}${n.toFixed(Math.abs(n)>=100?0:Math.abs(n)>=10?1:2)}億`},blk=(kind,title)=>`<div class="cash-subhead">${title}</div><div class="flow-matrix"><div class="flow-head"><b>法人</b><b>1日</b><b>5日</b><b>20日</b></div>${labs.map(([n,k])=>`<div class="flow-matrix-row"><b>${n}</b>${[1,5,20].map(x=>`<span>${money(inst?.[k]?.[String(x)]?.[kind])}</span>`).join('')}</div>`).join('')}</div>`;
 return `<div class="cashflow-stack">${blk('buy','買進金額')}${blk('sell','賣出金額')}${blk('net','淨買賣金額')}<small class="chart-note">${cf?.amount_note||''}</small>${v558MarginCards(cf)}</div>`;
}
'''
_oldjs=run_v551._patched_app_js
def _js558():
    t=_oldjs()
    if 'function v558MarginCards' not in t:t=_UI+'\n'+t
    for old in ["$('flowTable').innerHTML=v556Cash(d.cashflow||{});","$('flowTable').innerHTML=flowCashMatrix(d.cashflow||{},fl);","$('flowTable').innerHTML=flowMatrix(fl);"]:
        t=t.replace(old,"$('flowTable').innerHTML=v558Cash(d.cashflow||{});")
    return t
run_v551._patched_app_js=_js558
run_v551.CORE_CSS += r'''
.margin-dual-wrap{display:grid;gap:14px;margin-top:16px}.margin-card{border:1px solid var(--line);border-radius:16px;overflow:hidden;background:rgba(5,20,31,.45)}.margin-card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 16px;border-bottom:1px solid var(--line)}.margin-title{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.margin-title strong{color:var(--mint)}.margin-icon{display:inline-grid;place-items:center;width:34px;height:34px;border-radius:50%;background:#23894b;color:white;font-weight:900}.margin-periods{display:grid;grid-template-columns:repeat(3,auto);gap:14px}.margin-periods>span{white-space:nowrap}.margin-chart-box{padding:10px 12px 8px}.ms-legend{font-size:11px;color:var(--muted);display:flex;align-items:center;gap:7px;margin-bottom:4px}.bar-key{width:26px;height:3px;background:#ff4545;display:inline-block}.line-key{width:26px;height:3px;background:#ff9f1c;display:inline-block;margin-left:10px}.ms-svg{width:100%;height:auto;display:block}.ms-zero{stroke:#465866;stroke-width:1}.ms-bar.up{fill:#ff3434}.ms-bar.down{fill:#27b45a}.ms-line{stroke:#ff9f1c;stroke-width:3}.ms-axis,.ms-date{fill:#aab7c4;font-size:12px}.margin-info{padding:12px 14px;border:1px solid #315070;border-radius:12px;color:#a9bfd4;background:rgba(19,54,88,.3);font-size:11px}@media(max-width:600px){.margin-card-head{align-items:flex-start;flex-direction:column}.margin-periods{width:100%;grid-template-columns:repeat(3,1fr);gap:5px}.margin-periods>span{text-align:center;font-size:10px}.margin-title{font-size:12px}.ms-axis,.ms-date{font-size:11px}}
'''
_oldidx=run_v551._patched_index
def _idx558():return _oldidx().replace('5.5.7','5.5.8').replace('5.5.1','5.5.8')
run_v551._patched_index=_idx558
_oldsw=run_v551._patched_sw
def _sw558():return _oldsw().replace('ai-stock-v5.5.7','ai-stock-v5.5.8').replace('ai-stock-v5.5.1','ai-stock-v5.5.8')
run_v551._patched_sw=_sw558

@server.app.middleware('http')
async def v558_runtime(request:Request,call_next):
    if request.url.path=='/health':return JSONResponse({'status':'ok','version':VERSION,'mode':'margin-short-dual-chart'},headers={'Cache-Control':'no-store'})
    resp=await call_next(request);resp.headers['X-AI-Stock-Version']=VERSION;return resp
app=server.app
