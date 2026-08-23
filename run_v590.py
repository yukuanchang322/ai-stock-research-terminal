"""V5.9.0 Data Pipeline Rebuild.
Independent provider status, fast latest T86/MI_MARGN recovery, persistent history cache,
and explicit data-completeness separate from Research Score.
"""
from __future__ import annotations
import asyncio
from datetime import date, timedelta, datetime
from typing import Any
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

import run_v582, run_v551, server

VERSION="5.9.0"
server.app.version=VERSION
_hist_cache:dict[str,dict[str,Any]]={}
_hist_tasks:dict[str,asyncio.Task]={}


def num(v):
    try:
        if v is None:return None
        s=str(v).replace(',','').replace('--','').strip()
        return None if s in ('','-','None','null') else float(s)
    except:return None

def pick(r,*ks):
    for k in ks:
        if isinstance(r,dict) and r.get(k) not in (None,'','--','-'):return r.get(k)
    return None

def code(r):return str(pick(r,'證券代號','股票代號','Code','stock_id') or '').strip()

async def get_json(url,params=None,timeout=8):
    async with httpx.AsyncClient(timeout=timeout,follow_redirects=True,headers={'User-Agent':'Mozilla/5.0','Accept':'application/json'}) as c:
        r=await c.get(url,params=params);r.raise_for_status();return r.json()

def rows_from_rwd(j):
    if not isinstance(j,dict):return []
    out=[]
    if j.get('tables'):
        for t in j.get('tables') or []:
            fs=t.get('fields') or []
            for x in t.get('data') or []:out.append(x if isinstance(x,dict) else dict(zip(fs,x)))
    else:
        fs=j.get('fields') or []
        for x in j.get('data') or []:out.append(x if isinstance(x,dict) else dict(zip(fs,x)))
    return out

async def latest_t86(ticker,d):
    ds=str(d.get('price_as_of') or date.today().isoformat())[:10]
    q=ds.replace('-','')
    j=await get_json('https://www.twse.com.tw/rwd/zh/fund/T86',{'date':q,'selectType':'ALL','response':'json'},10)
    rs=rows_from_rwd(j);r=next((x for x in rs if code(x)==ticker),None)
    if not r:return None,{'status':'empty','date':ds,'rows':len(rs)}
    px=num(d.get('price'))
    fb=num(pick(r,'外陸資買進股數(不含外資自營商)','外陸資買進股數(不含自營商)'))
    fs=num(pick(r,'外陸資賣出股數(不含外資自營商)','外陸資賣出股數(不含自營商)'))
    tb=num(pick(r,'投信買進股數'));ts=num(pick(r,'投信賣出股數'))
    db=(num(pick(r,'自營商買進股數(自行買賣)')) or 0)+(num(pick(r,'自營商買進股數(避險)')) or 0)
    dsell=(num(pick(r,'自營商賣出股數(自行買賣)')) or 0)+(num(pick(r,'自營商賣出股數(避險)')) or 0)
    inst={}
    for k,b,s in [('foreign',fb,fs),('trust',tb,ts),('dealer',db,dsell)]:
        if b is not None and s is not None:
            inst[k]={'1':{'buy':b*px if px else None,'sell':s*px if px else None,'net':(b-s)*px if px else b-s,'shares_net':b-s,'days':1}}
    return {'institutional':inst,'institutional_rows':1,'institutional_source':'TWSE T86 official','last_date':ds}, {'status':'ok','date':ds,'rows':len(rs)}

async def latest_margin(ticker,d):
    j=await get_json('https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN',timeout=8)
    rs=j if isinstance(j,list) else []
    r=next((x for x in rs if code(x)==ticker),None)
    if not r:return None,{'status':'empty','rows':len(rs)}
    mb=num(pick(r,'融資今日餘額','MarginPurchaseTodayBalance'));pm=num(pick(r,'融資前日餘額','MarginPurchaseYesterdayBalance'))
    sb=num(pick(r,'融券今日餘額','ShortSaleTodayBalance'));ps=num(pick(r,'融券前日餘額','ShortSaleYesterdayBalance'))
    asof=str(d.get('price_as_of') or date.today().isoformat())[:10]
    out={'margin_balance':mb,'short_balance':sb,'margin_short_as_of':asof,'margin_last_date':asof,'margin_short_source':'TWSE OpenAPI MI_MARGN','margin_short_rows':1,
         'margin_short_abs':{'1':{'margin_change':None if mb is None or pm is None else mb-pm,'short_change':None if sb is None or ps is None else sb-ps}}}
    return out,{'status':'ok','rows':len(rs),'date':asof}

def merge_cf(d,extra):
    if not extra:return d
    cf=d.get('cashflow') if isinstance(d.get('cashflow'),dict) else {}
    for k,v in extra.items():
        if v not in (None,'',[],{}):cf[k]=v
    d['cashflow']=cf
    flow=d.get('flow') if isinstance(d.get('flow'),dict) else {}
    inst=cf.get('institutional') or {}
    for who,prefix in [('foreign','foreign'),('trust','trust'),('dealer','dealer')]:
        for n in (1,5,20):
            cell=((inst.get(who) or {}).get(str(n)) or {})
            if cell.get('net') is not None:flow[f'{prefix}_{n}']=cell['net']
    msa=cf.get('margin_short_abs') or {}
    for n in (1,5,20):
        c=msa.get(str(n)) or {}
        if c.get('margin_change') is not None:flow[f'margin_{n}_change']=c['margin_change']
        if c.get('short_change') is not None:flow[f'short_{n}_change']=c['short_change']
    d['flow']=flow
    return d

async def _one_day(dt,ticker,pxmap):
    q=dt.strftime('%Y%m%d');ds=dt.isoformat();out={}
    try:
        j=await get_json('https://www.twse.com.tw/rwd/zh/fund/T86',{'date':q,'selectType':'ALL','response':'json'},7)
        r=next((x for x in rows_from_rwd(j) if code(x)==ticker),None);px=pxmap.get(ds)
        if r and px:
            out['inst']={}
            pairs={
              'foreign':(num(pick(r,'外陸資買進股數(不含外資自營商)')),num(pick(r,'外陸資賣出股數(不含外資自營商)'))),
              'trust':(num(pick(r,'投信買進股數')),num(pick(r,'投信賣出股數'))),
              'dealer':((num(pick(r,'自營商買進股數(自行買賣)')) or 0)+(num(pick(r,'自營商買進股數(避險)')) or 0),(num(pick(r,'自營商賣出股數(自行買賣)')) or 0)+(num(pick(r,'自營商賣出股數(避險)')) or 0))}
            for k,(b,s) in pairs.items():
                if b is not None and s is not None:out['inst'][k]={'buy':b*px,'sell':s*px,'net':(b-s)*px}
    except:pass
    try:
        j=await get_json('https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN',{'date':q,'selectType':'ALL','response':'json'},7)
        r=next((x for x in rows_from_rwd(j) if code(x)==ticker),None)
        if r:out['margin']={'margin_balance':num(pick(r,'融資今日餘額')),'short_balance':num(pick(r,'融券今日餘額'))}
    except:pass
    return ds,out

async def warm_history(ticker,d):
    try:
        tech=(d.get('technical') or {}).get('series') or []
        pxmap={str(x.get('date'))[:10]:num(x.get('close')) for x in tech if x.get('date') and num(x.get('close')) is not None}
        end=date.fromisoformat(str(d.get('price_as_of') or date.today().isoformat())[:10])
        dates=[end-timedelta(days=i) for i in range(55) if (end-timedelta(days=i)).weekday()<5]
        sem=asyncio.Semaphore(4)
        async def go(dt):
            async with sem:return await _one_day(dt,ticker,pxmap)
        got=await asyncio.gather(*(go(x) for x in dates))
        valid=[(ds,x) for ds,x in got if x]
        instrows=[(ds,x['inst']) for ds,x in valid if x.get('inst')]
        marrows=[(ds,x['margin']) for ds,x in valid if x.get('margin')]
        extra={}
        if instrows:
            inst={}
            for who in ('foreign','trust','dealer'):
                inst[who]={}
                arr=[v[who] for _,v in instrows if who in v]
                for n in (1,5,20):
                    xs=arr[-n:]
                    if xs:
                        b=sum(z['buy'] for z in xs);s=sum(z['sell'] for z in xs);inst[who][str(n)]={'buy':b,'sell':s,'net':b-s,'days':len(xs)}
            extra.update({'institutional':inst,'institutional_rows':len(instrows),'institutional_source':'TWSE T86 official history','last_date':instrows[-1][0]})
        if len(marrows)>=2:
            hist=[]
            for ds,z in marrows:hist.append({'date':ds,**z})
            def ch(n,key):
                if len(hist)<=n:return None
                a=num(hist[-1].get(key));b=num(hist[-1-n].get(key));return None if a is None or b is None else a-b
            extra.update({'margin_history':hist[-40:],'margin_balance':hist[-1].get('margin_balance'),'short_balance':hist[-1].get('short_balance'),'margin_short_as_of':hist[-1]['date'],'margin_last_date':hist[-1]['date'],'margin_short_rows':len(hist),'margin_short_source':'TWSE MI_MARGN official history','margin_short_abs':{str(n):{'margin_change':ch(n,'margin_balance'),'short_change':ch(n,'short_balance')} for n in (1,5,20)}})
        _hist_cache[ticker]=extra
    finally:_hist_tasks.pop(ticker,None)

def schedule_history(ticker,d):
    t=_hist_tasks.get(ticker)
    if t and not t.done():return
    _hist_tasks[ticker]=asyncio.create_task(warm_history(ticker,dict(d)))

def completeness(d):
    checks=[(15,d.get('price') is not None),(5,bool(d.get('name') and d.get('name')!=d.get('ticker'))),(15,bool((d.get('revenue') or {}).get('series'))),(10,bool(d.get('per'))),(20,bool(d.get('financial') or d.get('official_financial'))),(15,len((d.get('technical') or {}).get('series') or [])>=20),(10,bool((d.get('cashflow') or {}).get('institutional'))),(10,(d.get('cashflow') or {}).get('margin_balance') is not None)]
    return sum(w for w,ok in checks if ok)

def provider_row(name,status,detail='',asof=None):return {'name':name,'status':status,'detail':detail,'as_of':asof}

async def build_stock_v590(ticker:str,force_refresh:bool=False):
    d=await run_v582.build_stock_v582(ticker,force_refresh=force_refresh)
    providers=[]
    # Merge persistent historical cache first.
    d=merge_cf(d,_hist_cache.get(ticker))
    if not (d.get('cashflow') or {}).get('institutional'):
        try:
            x,meta=await latest_t86(ticker,d);d=merge_cf(d,x);providers.append(provider_row('TWSE T86',meta['status'],f"rows={meta.get('rows')}",meta.get('date')))
        except Exception as e:providers.append(provider_row('TWSE T86','error',type(e).__name__))
    else:providers.append(provider_row('TWSE T86','ok','cache/history',(d.get('cashflow') or {}).get('last_date')))
    if (d.get('cashflow') or {}).get('margin_balance') is None:
        try:
            x,meta=await latest_margin(ticker,d);d=merge_cf(d,x);providers.append(provider_row('TWSE MI_MARGN',meta['status'],f"rows={meta.get('rows')}",meta.get('date')))
        except Exception as e:providers.append(provider_row('TWSE MI_MARGN','error',type(e).__name__))
    else:providers.append(provider_row('TWSE MI_MARGN','ok','cache/history',(d.get('cashflow') or {}).get('margin_last_date')))
    schedule_history(ticker,d)

    try:d['scores']=server.scores(d.get('technical') or {},d.get('revenue') or {},d.get('flow') or {},d.get('per') or {},d.get('financial') or {},d.get('research') or {})
    except:pass
    comp=completeness(d);d['data_completeness']=comp
    # Confidence is explicitly evidence/data confidence, not Research Score.
    evscore=None
    try:
        ev=server.build_evidence_graph(d.get('ticker'),d.get('technical') or {},d.get('revenue') or {},d.get('flow') or {},d.get('per') or {},d.get('financial') or {},d.get('eps_stack') or {},d.get('research') or {},d.get('company_events') or {},d.get('financial_integrity') or {},{})
        d['evidence']=ev;evscore=((ev.get('summary') or {}).get('evidence_score'))
    except:pass
    overall=round((float(evscore)*0.6+comp*0.4) if evscore is not None else comp)
    d['confidence']={'overall':overall,'data_completeness':comp,'evidence_score':evscore}

    cf=d.get('cashflow') or {}
    for row in d.get('source_status') or []:
        if row.get('name')=='三大法人':row.update({'as_of':cf.get('last_date'),'status':'ok' if cf.get('institutional') else 'missing','dataset':cf.get('institutional_source') or 'TWSE T86'})
        elif row.get('name')=='融資融券':row.update({'as_of':cf.get('margin_last_date') or cf.get('margin_short_as_of'),'status':'ok' if cf.get('margin_balance') is not None else 'missing','dataset':cf.get('margin_short_source') or 'TWSE MI_MARGN'})
    # Expose completeness in the freshness strip without pretending it is a market dataset.
    if not any(x.get('name')=='資料完整度' for x in d.get('source_status') or []):
        d.setdefault('source_status',[]).append({'name':'資料完整度','dataset':'V5.9 provider contract','as_of':f'{comp}/100','status':'ok' if comp>=80 else 'stale','scheduled_update':'隨報告重算'})

    diag=d.get('pipeline_diagnostics') if isinstance(d.get('pipeline_diagnostics'),dict) else {}
    diag.update({'mode':'provider-rebuild','providers':providers,'data_completeness':comp,'evidence_score':evscore,'history_cache_ready':ticker in _hist_cache,'history_task_running':ticker in _hist_tasks})
    d['pipeline_diagnostics']=diag;d['version']=VERSION
    d['data_policy']=(d.get('data_policy') or '')+' V5.9.0：資料源模組化；T86/MI_MARGN 最新快照前景取得、1/5/20 日歷史背景快取；Research Score 與資料完整度分離，provider diagnostics 保留實際成功/失敗狀態。'
    return d

server.build_stock=build_stock_v590
_oldidx=run_v551._patched_index
def _idx():
    text=_oldidx()
    for v in ('5.8.3','5.8.2','5.8.1','5.8.0','5.7.4','5.7.3','5.7.2','5.7.1','5.7.0','5.6.6','5.5.1'):text=text.replace(v,VERSION)
    return text
run_v551._patched_index=_idx
_oldsw=run_v551._patched_sw
def _sw():
    text=_oldsw()
    for v in ('5.8.3','5.8.2','5.8.1','5.8.0','5.7.4','5.7.3','5.7.2','5.7.1','5.7.0','5.6.6','5.5.1'):text=text.replace(f'ai-stock-v{v}',f'ai-stock-v{VERSION}')
    return text
run_v551._patched_sw=_sw

@server.app.middleware('http')
async def runtime(request:Request,call_next):
    if request.url.path=='/health':return JSONResponse({'status':'ok','version':VERSION,'mode':'provider-rebuild','data_completeness':True,'provider_diagnostics':True,'t86':'latest foreground + history cache','margin':'OpenAPI latest + history cache'},headers={'Cache-Control':'no-store'})
    resp=await call_next(request);resp.headers['X-AI-Stock-Version']=VERSION;return resp
app=server.app
