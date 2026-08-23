"""V5.7.1 Partial-report frontend compatibility.
Ensures legacy UI always receives a scores object while preserving unknown values as null.
"""
from __future__ import annotations
from fastapi import Request
from fastapi.responses import JSONResponse
import run_v570, run_v551, server

VERSION='5.7.1'
server.app.version=VERSION
_base=run_v570.build_stock_v570

_SCORE_KEYS=('綜合','基本面','籌碼面','技術面','估值')

def _compat(d):
    if not isinstance(d,dict): return d
    s=d.get('scores')
    if not isinstance(s,dict): s={}
    d['scores']={k:s.get(k) for k in _SCORE_KEYS}
    # Never manufacture zero from missing evidence.
    if d['scores'].get('綜合') is None:
        d['research_score']=None
    d['version']=VERSION
    d['ui_partial_safe']=True
    return d

async def build_stock_v571(ticker:str,force_refresh:bool=False):
    return _compat(await _base(ticker,force_refresh=force_refresh))

server.build_stock=build_stock_v571

_oldidx=run_v551._patched_index
def _idx():
    text=_oldidx()
    for v in ('5.7.0','5.6.6','5.6.5','5.6.4','5.6.3','5.6.2','5.6.1','5.6.0','5.5.9','5.5.1'):
        text=text.replace(v,VERSION)
    return text
run_v551._patched_index=_idx

_oldsw=run_v551._patched_sw
def _sw():
    text=_oldsw()
    for v in ('5.7.0','5.6.6','5.6.5','5.6.4','5.6.3','5.6.2','5.6.1','5.6.0','5.5.9','5.5.1'):
        text=text.replace(f'ai-stock-v{v}',f'ai-stock-v{VERSION}')
    return text
run_v551._patched_sw=_sw

@server.app.middleware('http')
async def runtime(request:Request,call_next):
    if request.url.path=='/health':
        return JSONResponse({'status':'ok','version':VERSION,'mode':'segmented-resilient-ui-safe','scores_object_always_present':True},headers={'Cache-Control':'no-store'})
    resp=await call_next(request);resp.headers['X-AI-Stock-Version']=VERSION;return resp
app=server.app
