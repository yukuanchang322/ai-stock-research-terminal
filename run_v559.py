"""V5.5.9 Data Pipeline Recovery.
Use stable V5.5.2 research output, then rebuild revenue/institutional/margin datasets
from FinMind per-stock history. V4 first, V3 fallback. Existing usable data is never
replaced by blanks. V5.5.8 UI is retained for revenue bars and margin dual charts.
"""
from __future__ import annotations
import os
from datetime import date,timedelta
from typing import Any
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
import run_v558, run_v552, run_v551, server

VERSION='5.5.9'
server.app.version=VERSION
_base=run_v552.build_stock_v552

def num(v:Any):
    try:
        if v is None:return None
        s=str(v).replace(',','').replace('--','').strip()
        return float(s) if s else None
    except:return None

async def fm4(dataset,ticker,start,end=None):
    params={'dataset':dataset,'data_id':ticker,'start_date':start}
    if end:params['end_date']=end
    headers={'Accept':'application/json'}
    tok=os.getenv('FINMIND_TOKEN')
    if tok:headers['Authorization']=f'Bearer {tok}'
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=True,headers=headers) as c:
            r=await c.get('https://api.finmindtrade.com/api/v4/data',params=params)
            if r.status_code>=400:return []
            j=r.json();return (j.get('data') or []) if isinstance(j,dict) else []
    except:return []

async def fm3(dataset,ticker,start):
    params={'dataset':dataset,'stock_id':ticker,'date':start}
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=True) as c:
            r=await c.get('https://api.finmindtrade.com/api/v3/data',params=params)
            if r.status_code>=400:return []
            j=r.json();return (j.get('data') or []) if isinstance(j,dict) else []
    except:return []

async def rows(dataset,ticker,start,end=None,min_rows=1):
    x=await fm4(dataset,ticker,start,end)
    if len(x)>=min_rows:return x
    y=await fm3(dataset,ticker,start)
    return y if len(y)>len(x) else x

def window(vals,n):
    xs=vals[-n:]
    if not xs:return {'buy':None,'sell':None,'net':None}
    b=sum(x.get('buy',0) for x in xs);s=sum(x.get('sell',0) for x in xs)
    return {'buy':b,'sell':s,'net':b-s,'days':len(xs)}

def cat(name):
    s=str(name or '').lower()
    if any(k in s for k in ('foreign','外資','外陸資')):return 'foreign'
    if any(k in s for k in ('investment_trust','投信','trust')):return 'trust'
    if any(k in s for k in ('dealer','自營')):return 'dealer'
    return None

async def repair_revenue(ticker,d):
    start=(date.today()-timedelta(days=520)).isoformat()
    rs=await rows('TaiwanStockMonthRevenue',ticker,start,min_rows=12)
    by={}
    for x in rs:
        rv=num(x.get('revenue'))
        try:p=f"{int(x.get('revenue_year')):04d}-{int(x.get('revenue_month')):02d}"
        except:
            ds=str(x.get('date') or '');p=ds[:7] if len(ds)>=7 else ''
        if p and rv is not None:by[p]={'period':p,'revenue':rv,'date':x.get('date')}
    for x in (d.get('revenue') or {}).get('series') or []:
        p=str(x.get('period') or '');rv=num(x.get('revenue'))
        if p and rv is not None:by.setdefault(p,{**x,'period':p,'revenue':rv})
    series=[by[k] for k in sorted(by)][-12:]
    if series:
        rev=d.get('revenue') or {};rev['series']=series;rev['history_months']=len(series);rev['series_source']='FinMind per-stock historical monthly revenue (V4/V3 recovery)';d['revenue']=rev
    return d

async def repair_institutional(ticker,d):
    tech=(d.get('technical') or {}).get('series') or []
    px={str(x.get('date')):num(x.get('close')) for x in tech if x.get('date') and num(x.get('close')) is not None}
    if not px:return d
    start=sorted(px)[max(0,len(px)-30)]
    rs=await rows('TaiwanStockInstitutionalInvestorsBuySell',ticker,start,min_rows=20)
    daily={}
    for x in rs:
        ds=str(x.get('date') or '')[:10];k=cat(x.get('name'));p=px.get(ds)
        b=num(x.get('buy'));s=num(x.get('sell'))
        if not ds or not k or p is None or b is None or s is None:continue
        z=daily.setdefault(ds,{}).setdefault(k,{'buy':0.0,'sell':0.0})
        z['buy']+=b*p;z['sell']+=s*p
    days=[]
    for ds in sorted(daily):
        row={'date':ds}
        for k,z in daily[ds].items():row[k]={'buy':z['buy'],'sell':z['sell'],'net':z['buy']-z['sell']}
        days.append(row)
    if days:
        inst={}
        for k in ('foreign','trust','dealer'):
            kvals=[]
            for x in days:
                if k in x:kvals.append({'buy':x[k]['buy'],'sell':x[k]['sell']})
            inst[k]={str(n):window(kvals,n) for n in (1,5,20)}
        cf=d.get('cashflow') or {};cf['institutional']=inst;cf['institutional_rows']=len(days);cf['amount_note']='法人估算成交金額＝FinMind/TWSE 法人買賣股數 × 各交易日收盤價；單位新台幣，非逐筆成交金額。';d['cashflow']=cf
    return d

async def repair_margin(ticker,d):
    start=(date.today()-timedelta(days=120)).isoformat()
    rs=await rows('TaiwanStockMarginPurchaseShortSale',ticker,start,min_rows=21)
    hist=[]
    for x in rs:
        ds=str(x.get('date') or '')[:10];mb=num(x.get('MarginPurchaseTodayBalance'));sb=num(x.get('ShortSaleTodayBalance'))
        if ds and (mb is not None or sb is not None):hist.append({'date':ds,'margin_balance':mb,'short_balance':sb})
    by={x['date']:x for x in hist};hist=[by[k] for k in sorted(by)][-60:]
    if len(hist)>=2:
        out=[];pm=ps=None
        for x in hist:
            mb=x['margin_balance'];sb=x['short_balance']
            out.append({**x,'margin_change':None if pm is None or mb is None else mb-pm,'short_change':None if ps is None or sb is None else sb-ps})
            if mb is not None:pm=mb
            if sb is not None:ps=sb
        def ch(n,key):
            if len(out)<=n:return None
            a=num(out[-1].get(key));b=num(out[-1-n].get(key));return None if a is None or b is None else a-b
        cf=d.get('cashflow') or {};cf['margin_history']=out;cf['margin_balance']=out[-1].get('margin_balance');cf['short_balance']=out[-1].get('short_balance');cf['margin_short_abs']={str(n):{'margin_change':ch(n,'margin_balance'),'short_change':ch(n,'short_balance')} for n in (1,5,20)};cf['margin_short_rows']=len(out);cf['margin_short_as_of']=out[-1]['date'];cf['margin_short_source']='FinMind TaiwanStockMarginPurchaseShortSale V4/V3';d['cashflow']=cf
    return d

async def build_stock_v559(ticker:str,force_refresh:bool=False):
    d=await _base(ticker,force_refresh=force_refresh)
    if isinstance(d,dict):
        for fn in (repair_revenue,repair_institutional,repair_margin):
            try:d=await fn(ticker,d)
            except Exception:pass
        d['version']=VERSION
        d['data_policy']=(d.get('data_policy') or '')+' V5.5.9：從穩定 V5.5.2 基線重建月營收、法人與融資融券歷史；V4 失敗回退 V3；空資料不得覆蓋可用資料。'
    return d
server.build_stock=build_stock_v559

_oldidx=run_v551._patched_index
def idx():return _oldidx().replace('5.5.8','5.5.9').replace('5.5.1','5.5.9')
run_v551._patched_index=idx
_oldsw=run_v551._patched_sw
def sw():return _oldsw().replace('ai-stock-v5.5.8','ai-stock-v5.5.9').replace('ai-stock-v5.5.1','ai-stock-v5.5.9')
run_v551._patched_sw=sw

@server.app.middleware('http')
async def runtime(request:Request,call_next):
    if request.url.path=='/health':return JSONResponse({'status':'ok','version':VERSION,'mode':'data-pipeline-recovery','revenue':'finmind-v4-v3','institutional':'finmind-v4-v3','margin_short':'finmind-v4-v3'},headers={'Cache-Control':'no-store'})
    resp=await call_next(request);resp.headers['X-AI-Stock-Version']=VERSION;return resp
app=server.app
