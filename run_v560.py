"""V5.6.0 Official-first data pipeline.
TWSE official endpoints are attempted first for institutional and margin/short data;
FinMind V4/V3 remains fallback. Revenue keeps the proven V5.5.9 recovery while
preserving any usable official/base series. Never replace usable data with blanks.
"""
from __future__ import annotations
from datetime import date,timedelta
from typing import Any
import asyncio,httpx
from fastapi import Request
from fastapi.responses import JSONResponse
import run_v559,run_v551,server

VERSION='5.6.0'
server.app.version=VERSION
_base=run_v559.build_stock_v559

def num(v:Any):
    try:
        if v is None:return None
        s=str(v).replace(',','').replace('--','').strip()
        return float(s) if s not in ('','-') else None
    except:return None

def pick(row,*keys):
    for k in keys:
        if k in row and row.get(k) not in (None,'','--','-'): return row.get(k)
    return None

async def twse_json(path,params):
    urls=[f'https://www.twse.com.tw/rwd/zh/{path}',f'https://www.twse.com.tw/{path}']
    async with httpx.AsyncClient(timeout=20,follow_redirects=True,headers={'User-Agent':'Mozilla/5.0','Accept':'application/json'}) as c:
        for u in urls:
            try:
                r=await c.get(u,params=params)
                if r.status_code>=400:continue
                j=r.json()
                if isinstance(j,dict) and (j.get('data') or j.get('tables')):return j
            except:pass
    return {}

def table_rows(j):
    out=[]
    if not isinstance(j,dict):return out
    tables=j.get('tables') or []
    if tables:
        for t in tables:
            fs=t.get('fields') or []
            for r in t.get('data') or []:
                if isinstance(r,dict):out.append(r)
                elif fs and isinstance(r,list):out.append(dict(zip(fs,r)))
    else:
        fs=j.get('fields') or []
        for r in j.get('data') or []:
            if isinstance(r,dict):out.append(r)
            elif fs and isinstance(r,list):out.append(dict(zip(fs,r)))
    return out

def roc_date(d):return f'{d.year-1911:03d}{d.month:02d}{d.day:02d}'

async def official_day(path,d):
    return table_rows(await twse_json(path,{'date':d.strftime('%Y%m%d'),'response':'json','selectType':'ALLBUT0999'}))

async def collect_days(path,want=60,lookback=130):
    sem=asyncio.Semaphore(6)
    async def one(d):
        async with sem:return d,await official_day(path,d)
    today=date.today(); ds=[today-timedelta(days=i) for i in range(lookback) if (today-timedelta(days=i)).weekday()<5]
    batches=[]
    for i in range(0,len(ds),18):
        got=await asyncio.gather(*(one(d) for d in ds[i:i+18]))
        batches.extend([(d,r) for d,r in got if r])
        if len(batches)>=want:break
    return sorted(batches,key=lambda x:x[0])[-want:]

def code_of(r):return str(pick(r,'證券代號','股票代號','Code','stock_id') or '').strip()

async def repair_institutional_official(ticker,d):
    tech=(d.get('technical') or {}).get('series') or []
    px={str(x.get('date'))[:10]:num(x.get('close')) for x in tech if x.get('date') and num(x.get('close')) is not None}
    days=await collect_days('fund/T86',20,55)
    vals=[]
    for dt,rows in days:
        row=next((r for r in rows if code_of(r)==ticker),None); p=px.get(dt.isoformat())
        if not row or p is None:continue
        fb=num(pick(row,'外陸資買進股數(不含外資自營商)','外陸資買進股數(不含自營商)'));fs=num(pick(row,'外陸資賣出股數(不含外資自營商)','外陸資賣出股數(不含自營商)'))
        tb=num(pick(row,'投信買進股數'));ts=num(pick(row,'投信賣出股數'))
        db=(num(pick(row,'自營商買進股數(自行買賣)','自營商(自行買賣)買進股數')) or 0)+(num(pick(row,'自營商買進股數(避險)','自營商(避險)買進股數')) or 0)
        ds=(num(pick(row,'自營商賣出股數(自行買賣)','自營商(自行買賣)賣出股數')) or 0)+(num(pick(row,'自營商賣出股數(避險)','自營商(避險)賣出股數')) or 0)
        z={'date':dt.isoformat()}
        if fb is not None and fs is not None:z['foreign']={'buy':fb*p,'sell':fs*p}
        if tb is not None and ts is not None:z['trust']={'buy':tb*p,'sell':ts*p}
        z['dealer']={'buy':db*p,'sell':ds*p};vals.append(z)
    if not vals:return d
    def win(k,n):
        xs=[x[k] for x in vals if k in x][-n:]
        if not xs:return {'buy':None,'sell':None,'net':None}
        b=sum(x['buy'] for x in xs);s=sum(x['sell'] for x in xs);return {'buy':b,'sell':s,'net':b-s,'days':len(xs)}
    inst={k:{str(n):win(k,n) for n in (1,5,20)} for k in ('foreign','trust','dealer')}
    cf=d.get('cashflow') or {};cf['institutional']=inst;cf['institutional_rows']=len(vals);cf['institutional_source']='TWSE T86 official';cf['amount_note']='法人估算成交金額＝TWSE T86 官方買進/賣出股數 × 各交易日收盤價；單位新台幣。';d['cashflow']=cf
    return d

async def repair_margin_official(ticker,d):
    days=await collect_days('marginTrading/MI_MARGN',60,135);hist=[]
    for dt,rows in days:
        row=next((r for r in rows if code_of(r)==ticker),None)
        if not row:continue
        mb=num(pick(row,'融資今日餘額','融資餘額','MarginPurchaseTodayBalance'));sb=num(pick(row,'融券今日餘額','融券餘額','ShortSaleTodayBalance'))
        if mb is not None or sb is not None:hist.append({'date':dt.isoformat(),'margin_balance':mb,'short_balance':sb})
    if len(hist)<2:return d
    out=[];pm=ps=None
    for x in hist:
        mb=x['margin_balance'];sb=x['short_balance'];out.append({**x,'margin_change':None if pm is None or mb is None else mb-pm,'short_change':None if ps is None or sb is None else sb-ps})
        if mb is not None:pm=mb
        if sb is not None:ps=sb
    def ch(n,key):
        if len(out)<=n:return None
        a=num(out[-1].get(key));b=num(out[-1-n].get(key));return None if a is None or b is None else a-b
    cf=d.get('cashflow') or {};cf['margin_history']=out[-60:];cf['margin_balance']=out[-1].get('margin_balance');cf['short_balance']=out[-1].get('short_balance');cf['margin_short_abs']={str(n):{'margin_change':ch(n,'margin_balance'),'short_change':ch(n,'short_balance')} for n in (1,5,20)};cf['margin_short_rows']=len(out);cf['margin_short_as_of']=out[-1]['date'];cf['margin_short_source']='TWSE MI_MARGN official';d['cashflow']=cf
    return d

async def build_stock_v560(ticker:str,force_refresh:bool=False):
    d=await _base(ticker,force_refresh=force_refresh)
    if isinstance(d,dict):
        diag={}
        try:d=await repair_institutional_official(ticker,d)
        except Exception as e:diag['institutional_official_error']=type(e).__name__
        try:d=await repair_margin_official(ticker,d)
        except Exception as e:diag['margin_official_error']=type(e).__name__
        rev=d.get('revenue') or {};cf=d.get('cashflow') or {}
        diag.update({'revenue_months':len(rev.get('series') or []),'revenue_source':rev.get('series_source'),'institutional_rows':cf.get('institutional_rows'),'institutional_source':cf.get('institutional_source'),'margin_rows':cf.get('margin_short_rows'),'margin_source':cf.get('margin_short_source')})
        d['pipeline_diagnostics']=diag;d['version']=VERSION;d['data_policy']=(d.get('data_policy') or '')+' V5.6.0：TWSE 官方 T86/MI_MARGN 優先，FinMind 僅作備援；保留可用資料並提供 pipeline diagnostics。'
    return d
server.build_stock=build_stock_v560

_oldidx=run_v551._patched_index
def idx():return _oldidx().replace('5.5.9','5.6.0').replace('5.5.8','5.6.0').replace('5.5.1','5.6.0')
run_v551._patched_index=idx
_oldsw=run_v551._patched_sw
def sw():return _oldsw().replace('ai-stock-v5.5.9','ai-stock-v5.6.0').replace('ai-stock-v5.5.8','ai-stock-v5.6.0').replace('ai-stock-v5.5.1','ai-stock-v5.6.0')
run_v551._patched_sw=sw

@server.app.middleware('http')
async def runtime(request:Request,call_next):
    if request.url.path=='/health':return JSONResponse({'status':'ok','version':VERSION,'mode':'official-first','institutional':'TWSE-T86->FinMind','margin_short':'TWSE-MI_MARGN->FinMind','revenue':'base/FinMind-preserve','diagnostics':True},headers={'Cache-Control':'no-store'})
    resp=await call_next(request);resp.headers['X-AI-Stock-Version']=VERSION;return resp
app=server.app
