"""V5.7.3 Persistent Core Build.
Removes the hidden V5.6.6 28-second core timeout from the background path so
partial reports can actually converge to a complete cached report.
"""
from __future__ import annotations
import asyncio
from typing import Any
from fastapi import Request
from fastapi.responses import JSONResponse
import run_v572, run_v570, run_v566, run_v564, run_v551, server

VERSION='5.7.3'
server.app.version=VERSION
_cache:dict[str,dict[str,Any]]={}
_tasks:dict[str,asyncio.Task]={}


def _compat(d:dict[str,Any])->dict[str,Any]:
    d=run_v572._compat(d)
    d['version']=VERSION
    return d

async def _complete_core(ticker:str, force_refresh:bool=False):
    try:
        # Critical fix: call the underlying core directly with NO outer 28s timeout.
        d=await run_v566._build_core(ticker, force_refresh=force_refresh)
        if not isinstance(d,dict):
            raise RuntimeError('core report payload invalid')

        # Revenue enrichment is bounded independently and cannot erase core data.
        try:
            d=await run_v566._repair_revenue_bounded(ticker,d)
        except Exception:
            pass

        # Independent official/latest close bootstrap.
        try:
            boot,attempts=await asyncio.wait_for(run_v564._bootstrap_price(ticker),timeout=8.0)
            d['price_bootstrap_attempts']=attempts
            if boot:
                ds,p,source=boot
                d=run_v564._apply(d,p,ds,source)
                d=run_v566._recompute_after_price(d)
        except Exception as e:
            d['price_bootstrap_error']=f'{type(e).__name__}: {str(e)[:160]}'

        # Reuse / schedule official institutional & margin history enrichment.
        try:
            d=run_v566._merge_official_cache(d,run_v566._official_cache.get(ticker))
            if ticker not in run_v566._official_cache:
                d['official_cache_status']='warming'
            run_v566._schedule_official(ticker,d)
        except Exception:
            pass

        # Scores only when real evidence exists; keep UI contract otherwise.
        if not isinstance(d.get('scores'),dict) or not d.get('scores'):
            try:
                d['scores']=server.scores(d.get('technical') or {},d.get('revenue') or {},d.get('flow') or {},d.get('per') or {},d.get('financial') or {},d.get('research') or {})
            except Exception:
                pass

        d['pipeline_state']='ready'
        d['report_partial']=False
        diag=d.get('pipeline_diagnostics') if isinstance(d.get('pipeline_diagnostics'),dict) else {}
        diag.update({'mode':'persistent_core','core_background_timeout':None,'core_complete':True})
        d['pipeline_diagnostics']=diag
        _cache[ticker]=_compat(d)
    except Exception as e:
        # Keep any previous good cache and expose compact diagnostics.
        old=_cache.get(ticker)
        if old:
            old=dict(old);old['background_error']=f'{type(e).__name__}: {str(e)[:180]}';_cache[ticker]=_compat(old)
    finally:
        _tasks.pop(ticker,None)


def _schedule(ticker:str,force_refresh:bool=False):
    t=_tasks.get(ticker)
    if t and not t.done(): return t
    t=asyncio.create_task(_complete_core(ticker,force_refresh))
    _tasks[ticker]=t
    return t

async def build_stock_v573(ticker:str,force_refresh:bool=False):
    t=_schedule(ticker,force_refresh)
    # Give the persistent task a short foreground window without cancelling it.
    try:
        await asyncio.wait_for(asyncio.shield(t),timeout=10.0)
    except Exception:
        pass
    if ticker in _cache:
        return _compat(dict(_cache[ticker]))

    # Truthful partial shell while the same persistent core continues running.
    d:dict[str,Any]={
        'ticker':ticker,'version':VERSION,'pipeline_state':'loading','report_partial':True,
        'technical':{'series':[]},'revenue':{'series':[]},'financial':{},'cashflow':{},'eps_stack':{},
        'valuation':{'scenarios':[],'status':'waiting_for_eps','eps_basis':'資料取得中；不以缺值估價'},
        'source_status':[], 'scores':{'綜合':None,'基本面':None,'籌碼面':None,'技術面':None,'估值':None},
    }
    try:
        boot,attempts=await asyncio.wait_for(run_v564._bootstrap_price(ticker),timeout=6.0)
        d['price_bootstrap_attempts']=attempts
        if boot:
            ds,p,source=boot;d=run_v564._apply(d,p,ds,source)
    except Exception as e:
        d['price_bootstrap_error']=f'{type(e).__name__}: {str(e)[:120]}'
    d['status_text']='核心資料背景建置中，請稍後按更新'
    return _compat(d)

server.build_stock=build_stock_v573

_oldidx=run_v551._patched_index
def _idx():
    text=_oldidx()
    for v in ('5.7.2','5.7.1','5.7.0','5.6.6','5.6.5','5.6.4','5.6.3','5.6.2','5.6.1','5.6.0','5.5.9','5.5.1'):
        text=text.replace(v,VERSION)
    return text
run_v551._patched_index=_idx
_oldsw=run_v551._patched_sw
def _sw():
    text=_oldsw()
    for v in ('5.7.2','5.7.1','5.7.0','5.6.6','5.6.5','5.6.4','5.6.3','5.6.2','5.6.1','5.6.0','5.5.9','5.5.1'):
        text=text.replace(f'ai-stock-v{v}',f'ai-stock-v{VERSION}')
    return text
run_v551._patched_sw=_sw

@server.app.middleware('http')
async def runtime(request:Request,call_next):
    if request.url.path=='/health':
        return JSONResponse({'status':'ok','version':VERSION,'mode':'persistent-core','core_background_timeout':None,'partial_to_complete_cache':True},headers={'Cache-Control':'no-store'})
    resp=await call_next(request);resp.headers['X-AI-Stock-Version']=VERSION;return resp

app=server.app
