"""V5.5.6 Stable Data Pipeline.
Rebuilds revenue, institutional cashflow, and margin/short history from V5.5.2 baseline.
Non-destructive rule: fallback data never overwrites usable base data with blanks.
"""
from __future__ import annotations
import asyncio, os
from datetime import date, timedelta
from typing import Any
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
import run_v552, run_v551, server

VERSION="5.5.6"
server.app.version=VERSION
_base_build=server.build_stock

def _num(v:Any):
    try:
        if v is None:return None
        s=str(v).replace(',','').replace('--','').strip()
        return float(s) if s else None
    except:return None

def _row(payload,ticker):
    if not isinstance(payload,dict):return None
    groups=[(payload.get('fields') or [],payload.get('data') or [])]
    groups += [(t.get('fields') or [],t.get('data') or []) for t in (payload.get('tables') or [])]
    for fields,data in groups:
        for r in data:
            d=dict(zip(fields,r)); code=str(d.get('證券代號') or d.get('股票代號') or d.get('代號') or '').strip()
            if code==ticker:return d
    return None

def _pick(d,words):
    if not d:return None
    for k,v in d.items():
        ks=str(k).replace(' ','')
        if all(w in ks for w in words):
            n=_num(v)
            if n is not None:return n
    return None

async def _finmind(dataset,ticker,start):
    params={'dataset':dataset,'data_id':ticker,'start_date':start}
    headers={'Accept':'application/json'}
    token=os.getenv('FINMIND_TOKEN')
    if token:headers['Authorization']=f'Bearer {token}'
    try:
        async with httpx.AsyncClient(timeout=20,follow_redirects=True,headers=headers) as c:
            r=await c.get('https://api.finmindtrade.com/api/v4/data',params=params)
            if r.status_code>=400:return []
            j=r.json(); return (j.get('data') or []) if isinstance(j,dict) else []
    except:return []

async def _revenue(ticker,d):
    start=(date.today()-timedelta(days=1100)).isoformat()
    rows=await _finmind('TaiwanStockMonthRevenue',ticker,start)
    by={}
    for x in rows:
        rv=_num(x.get('revenue')); y=x.get('revenue_year'); m=x.get('revenue_month')
        if rv is None:continue
        try:p=f'{int(y):04d}-{int(m):02d}'
        except:
            ds=str(x.get('date') or ''); p=ds[:7] if len(ds)>=7 else ''
        if p:by[p]={'period':p,'revenue':rv,'date':x.get('date')}
    # Merge base series, never discard it.
    for x in (d.get('revenue') or {}).get('series') or []:
        p=str(x.get('period') or ''); rv=_num(x.get('revenue'))
        if p and rv is not None:by.setdefault(p,{**x,'period':p,'revenue':rv})
    series=[by[k] for k in sorted(by)][-40:]
    mm={x['period']:x['revenue'] for x in series}; yoy=[]
    for x in series:
        p=x['period']
        try:base=mm.get(f'{int(p[:4])-1:04d}-{int(p[5:7]):02d}')
        except:continue
        if base not in (None,0):yoy.append({'period':p,'yoy':(x['revenue']/base-1)*100,'revenue':x['revenue']})
    rev=d.get('revenue') or {}
    if series:rev['series']=series
    rev['yoy_series']=yoy[-12:]; rev['history_months']=len(series); rev['yoy_months']=len(yoy[-12:]); rev['yoy_bar_ready']=len(yoy)>=12
    rev['series_source']='FinMind TaiwanStockMonthRevenue + preserved base series'
    d['revenue']=rev; return d

_sem=asyncio.Semaphore(4)
async def _twse_json(client,url,params):
    async with _sem:
        try:
            r=await client.get(url,params=params,timeout=15)
            if r.status_code>=400:return None
            j=r.json(); return j if isinstance(j,dict) else None
        except:return None

async def _t86(client,ticker,ds,close):
    j=await _twse_json(client,'https://www.twse.com.tw/rwd/zh/fund/T86',{'date':ds.replace('-',''),'selectType':'ALLBUT0999','response':'json'})
    q=_row(j,ticker)
    if not q:return None
    fb=_pick(q,('外陸資','買進股數')) or _pick(q,('外資','買進股數')); fs=_pick(q,('外陸資','賣出股數')) or _pick(q,('外資','賣出股數'))
    tb=_pick(q,('投信','買進股數')); ts=_pick(q,('投信','賣出股數'))
    db=(_pick(q,('自營商','買進股數','自行買賣')) or 0)+(_pick(q,('自營商','買進股數','避險')) or 0)
    dsell=(_pick(q,('自營商','賣出股數','自行買賣')) or 0)+(_pick(q,('自營商','賣出股數','避險')) or 0)
    out={'date':ds}
    for k,b,s in [('foreign',fb,fs),('trust',tb,ts),('dealer',db,dsell)]:
        if b is not None and s is not None:out[k]={'buy':b*close,'sell':s*close,'net':(b-s)*close}
    return out

async def _marg(client,ticker,ds):
    for sel in ('ALL','MS'):
        j=await _twse_json(client,'https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN',{'date':ds.replace('-',''),'selectType':sel,'response':'json'})
        q=_row(j,ticker)
        if q:
            mb=_pick(q,('融資','今日餘額')); sb=_pick(q,('融券','今日餘額'))
            if mb is not None or sb is not None:return {'date':ds,'margin_balance':mb,'short_balance':sb}
    return None

def _window(rows,n,key):
    xs=rows[-n:] if rows else []
    if not xs:return None
    b=sum((x.get(key) or {}).get('buy',0) for x in xs); s=sum((x.get(key) or {}).get('sell',0) for x in xs)
    return {'buy':b,'sell':s,'net':b-s,'days':len(xs)}

def _chg(hist,n,key):
    if len(hist)<=n:return None
    now=hist[-1].get(key); old=hist[-1-n].get(key)
    return None if now is None or old in (None,0) else (now/old-1)*100

async def _cash(ticker,d):
    tech=[x for x in (d.get('technical') or {}).get('series',[]) if x.get('date') and _num(x.get('close')) is not None][-25:]
    if not tech:return d
    async with httpx.AsyncClient(follow_redirects=True,headers={'User-Agent':'Mozilla/5.0 AI-Stock-Research/5.5.6'}) as c:
        trows=await asyncio.gather(*(_t86(c,ticker,str(x['date']),float(x['close'])) for x in tech[-20:]))
        mrows=await asyncio.gather(*(_marg(c,ticker,str(x['date'])) for x in tech[-21:]))
    trows=[x for x in trows if x]; mrows=[x for x in mrows if x]
    cf=d.get('cashflow') or {}
    if trows:
        inst={}
        for k in ('foreign','trust','dealer'):inst[k]={str(n):_window(trows,n,k) for n in (1,5,20)}
        cf['institutional']=inst; cf['institutional_rows']=len(trows)
    if len(mrows)>=2:
        ms={str(n):{'margin_pct':_chg(mrows,n,'margin_balance'),'short_pct':_chg(mrows,n,'short_balance'),'margin_balance':mrows[-1].get('margin_balance'),'short_balance':mrows[-1].get('short_balance')} for n in (1,5,20)}
        # Only replace if at least one real change exists.
        if any(v.get('margin_pct') is not None or v.get('short_pct') is not None for v in ms.values()):
            cf['margin_short']=ms; cf['margin_short_rows']=len(mrows); cf['margin_short_as_of']=mrows[-1]['date']
    cf['amount_note']='法人估算成交金額＝TWSE T86 買進/賣出股數 × 各交易日收盤價；單位新台幣，非逐筆成交金額。'
    d['cashflow']=cf; return d

async def build_stock_v556(ticker:str,force_refresh:bool=False):
    d=await _base_build(ticker,force_refresh=force_refresh)
    if isinstance(d,dict):
        try:d=await _revenue(ticker,d)
        except Exception:pass
        try:d=await _cash(ticker,d)
        except Exception:pass
        d['version']=VERSION
        d['data_policy']=(d.get('data_policy') or '')+' V5.5.6：非破壞式資料補強；不足資料不覆蓋可用基礎資料。'
    return d
server.build_stock=build_stock_v556

_UI=r'''
function v556Money(v){if(v==null||!Number.isFinite(Number(v)))return '—';const n=Number(v)/1e8;return `${n>0?'+':''}${n.toFixed(Math.abs(n)>=100?0:Math.abs(n)>=10?1:2)}億`;}
function v556Pct(v,reverse=false){if(v==null||!Number.isFinite(Number(v)))return '<span>—</span>';const n=Number(v),bad=reverse?n>0:n<0;return `<span class="${bad?'neg':'pos'}">${n>0?'+':''}${n.toFixed(1)}%</span>`;}
function v556Revenue(r){const data=(r?.yoy_series||[]).slice(-12);if(data.length<12)return `<div class="empty bordered">營收歷史資料不足：目前 ${data.length}/12 個月，暫不顯示不完整柱狀圖</div>`;const w=720,h=230,p=40,vals=data.map(x=>Number(x.yoy)),mn=Math.min(0,...vals),mx=Math.max(0,...vals),span=(mx-mn)||1,y=v=>20+(mx-v)*(h-60)/span,z=y(0),step=(w-p-10)/12,bw=step*.58,bars=data.map((x,i)=>{const v=Number(x.yoy),cx=p+(i+.5)*step,yy=y(v),top=Math.min(yy,z),bh=Math.max(1,Math.abs(yy-z));return `<g><rect class="revenue-bar ${v>=0?'positive':'negative'}" x="${cx-bw/2}" y="${top}" width="${bw}" height="${bh}" rx="3"><title>${x.period} ${v.toFixed(1)}%</title></rect><text class="revenue-xlabel" x="${cx}" y="${h-10}" text-anchor="middle">${x.period.slice(5)}</text></g>`}).join('');return `<div class="revenue-yoy-head"><b>逐月營收 YoY</b><span>12/12 月｜最新 ${Number(data[11].yoy)>=0?'+':''}${Number(data[11].yoy).toFixed(1)}%</span></div><svg class="revenue-bar-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line class="revenue-zero" x1="${p}" y1="${z}" x2="${w-10}" y2="${z}"/>${bars}</svg>`;}
function v556Cash(cf){const inst=cf?.institutional||{},labs=[['外資','foreign'],['投信','trust'],['自營商','dealer']],blk=(kind,title)=>`<div class="cash-subhead">${title}</div><div class="flow-matrix"><div class="flow-head"><b>法人</b><b>1日</b><b>5日</b><b>20日</b></div>${labs.map(([n,k])=>`<div class="flow-matrix-row"><b>${n}</b>${[1,5,20].map(x=>`<span>${v556Money(inst?.[k]?.[String(x)]?.[kind])}</span>`).join('')}</div>`).join('')}</div>`;const ms=cf?.margin_short||{};return `<div class="cashflow-stack">${blk('buy','買進金額')}${blk('sell','賣出金額')}${blk('net','淨買賣金額')}<div class="cash-subhead">融資融券餘額變化</div><div class="flow-matrix"><div class="flow-head"><b>項目</b><b>1日</b><b>5日</b><b>20日</b></div><div class="flow-matrix-row"><b>融資</b>${[1,5,20].map(n=>v556Pct(ms?.[String(n)]?.margin_pct,true)).join('')}</div><div class="flow-matrix-row"><b>融券</b>${[1,5,20].map(n=>v556Pct(ms?.[String(n)]?.short_pct,false)).join('')}</div></div><small class="chart-note">${cf?.amount_note||''}</small></div>`;}
'''
_oldjs=run_v551._patched_app_js
def _js556():
    t=_oldjs()
    if 'function v556Revenue' not in t:t=_UI+'\n'+t
    t=t.replace("$('fundChart').innerHTML=revenueBarSvgCore(r.series||[]);","$('fundChart').innerHTML=v556Revenue(r);")
    t=t.replace("$('flowTable').innerHTML=flowMatrix(fl);","$('flowTable').innerHTML=v556Cash(d.cashflow||{});")
    return t
run_v551._patched_app_js=_js556
run_v551.CORE_CSS += '.cashflow-stack{display:grid;gap:10px;width:100%}.cash-subhead{font-size:11px;font-weight:800;color:var(--text);margin-top:4px}.cashflow-stack .flow-matrix{width:100%!important;max-width:none!important}.cashflow-stack .flow-head,.cashflow-stack .flow-matrix-row{grid-template-columns:48px repeat(3,minmax(0,1fr))!important}.cashflow-stack .flow-matrix-row span{font-size:10px!important;white-space:nowrap!important}'
_oldidx=run_v551._patched_index
def _idx556():return _oldidx().replace('5.5.2','5.5.6').replace('5.5.1','5.5.6')
run_v551._patched_index=_idx556
_oldsw=run_v551._patched_sw
def _sw556():return _oldsw().replace('ai-stock-v5.5.1','ai-stock-v5.5.6')
run_v551._patched_sw=_sw556

@server.app.middleware('http')
async def v556_runtime(request:Request,call_next):
    if request.url.path=='/health':return JSONResponse({'status':'ok','version':VERSION,'mode':'stable-nondestructive-data-pipeline'},headers={'Cache-Control':'no-store'})
    resp=await call_next(request);resp.headers['X-AI-Stock-Version']=VERSION;return resp
app=server.app
