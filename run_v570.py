"""V5.7.0 Resilient Segmented Data Pipeline.

A slow provider must never turn a usable stock report into a whole-page failure.
V5.6.6 core is allowed to finish in background; foreground returns the best verified
snapshot available. Price bootstrap is independent and no fake zero score is emitted.
"""
from __future__ import annotations
import asyncio
from typing import Any
from fastapi import Request
from fastapi.responses import JSONResponse
import run_v566, run_v564, run_v551, server

VERSION='5.7.0'
server.app.version=VERSION
_cache:dict[str,dict[str,Any]]={}
_tasks:dict[str,asyncio.Task]={}

def _usable(v:Any)->bool:
    return v not in (None,'',[],{})

def _module_state(d:dict[str,Any], key:str)->str:
    v=d.get(key)
    if key=='technical': return 'ready' if isinstance(v,dict) and len(v.get('series') or [])>=2 else 'updating'
    if key=='revenue': return 'ready' if isinstance(v,dict) and len(v.get('series') or [])>=1 else 'updating'
    if key=='financial':
        return 'ready' if _usable(v) or _usable(d.get('official_financial')) or _usable(d.get('eps_stack')) else 'updating'
    if key=='cashflow': return 'ready' if isinstance(v,dict) and bool(v) else 'updating'
    if key=='valuation': return 'ready' if isinstance(v,dict) and bool(v.get('scenarios')) else 'waiting_for_eps'
    return 'ready' if _usable(v) else 'updating'

def _sanitize(d:dict[str,Any])->dict[str,Any]:
    # Missing evidence is unknown, never a synthetic bearish/zero score.
    s=d.get('scores')
    evidence=any(_module_state(d,k)=='ready' for k in ('technical','revenue','financial','cashflow'))
    if not evidence or (isinstance(s,dict) and s and all((v in (0,0.0,None) for v in s.values() if isinstance(v,(int,float,type(None)))))):
        d.pop('scores',None)
        d['research_score']=None
    if not _usable(d.get('eps_stack')):
        d['valuation']={**(d.get('valuation') or {}),'scenarios':[],'status':'waiting_for_eps','eps_basis':'資料取得中；不以缺值估價'}
    states={k:_module_state(d,k) for k in ('price','technical','revenue','financial','cashflow','valuation')}
    d['module_status']=states
    d['report_partial']=any(v!='ready' for v in states.values())
    d['version']=VERSION
    return d

async def _finish(ticker:str, force_refresh:bool=False):
    try:
        d=await run_v566.build_stock_v566(ticker,force_refresh=force_refresh)
        if isinstance(d,dict): _cache[ticker]=_sanitize(d)
    except Exception as e:
        old=_cache.get(ticker) or {}
        old['background_error']=f'{type(e).__name__}: {str(e)[:160]}'
        if old:_cache[ticker]=_sanitize(old)
    finally:_tasks.pop(ticker,None)

def _schedule(ticker:str,force_refresh:bool=False):
    t=_tasks.get(ticker)
    if t and not t.done():return
    _tasks[ticker]=asyncio.create_task(_finish(ticker,force_refresh))

async def build_stock_v570(ticker:str,force_refresh:bool=False):
    # Give the complete pipeline a short foreground opportunity. Timeout does NOT cancel it:
    # shield lets the same work continue and populate cache instead of starting over.
    t=_tasks.get(ticker)
    if not t or t.done():
        t=asyncio.create_task(_finish(ticker,force_refresh));_tasks[ticker]=t
    try:
        await asyncio.wait_for(asyncio.shield(t),timeout=12.0)
    except asyncio.TimeoutError:pass
    except Exception:pass
    if ticker in _cache:
        d=dict(_cache[ticker]);d['pipeline_state']='ready' if not d.get('report_partial') else 'partial_updating';return _sanitize(d)

    # Independent price shell: truthful partial response, never whole-page error.
    d:dict[str,Any]={'ticker':ticker,'version':VERSION,'pipeline_state':'loading','report_partial':True,
                     'technical':{},'revenue':{},'financial':{},'cashflow':{},'eps_stack':{},
                     'valuation':{'scenarios':[],'status':'waiting_for_eps','eps_basis':'資料取得中；不以缺值估價'}}
    try:
        boot,attempts=await asyncio.wait_for(run_v564._bootstrap_price(ticker),timeout=6.0)
        d['price_bootstrap_attempts']=attempts
        if boot:
            ds,p,source=boot;d=run_v564._apply(d,p,ds,source)
    except Exception as e:d['price_bootstrap_error']=f'{type(e).__name__}: {str(e)[:120]}'
    return _sanitize(d)

server.build_stock=build_stock_v570

_oldidx=run_v551._patched_index
def _idx():
    text=_oldidx()
    for v in ('5.6.6','5.6.5','5.6.4','5.6.3','5.6.2','5.6.1','5.6.0','5.5.9','5.5.1'):text=text.replace(v,VERSION)
    return text
run_v551._patched_index=_idx
_oldsw=run_v551._patched_sw
def _sw():
    text=_oldsw()
    for v in ('5.6.6','5.6.5','5.6.4','5.6.3','5.6.2','5.6.1','5.6.0','5.5.9','5.5.1'):text=text.replace(f'ai-stock-v{v}',f'ai-stock-v{VERSION}')
    return text
run_v551._patched_sw=_sw

@server.app.middleware('http')
async def runtime(request:Request,call_next):
    if request.url.path=='/health':return JSONResponse({'status':'ok','version':VERSION,'mode':'segmented-resilient','whole_report_timeout_failure':False,'partial_reports':True,'missing_score_is_null':True},headers={'Cache-Control':'no-store'})
    resp=await call_next(request);resp.headers['X-AI-Stock-Version']=VERSION;return resp
app=server.app
