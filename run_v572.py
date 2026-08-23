"""V5.7.2 Complete partial-report compatibility contract.
Guarantees legacy UI collections/objects/date fields are always present while keeping
unknown financial evidence as null/empty rather than fabricated zeroes.
"""
from __future__ import annotations
from datetime import datetime, timezone
from fastapi import Request
from fastapi.responses import JSONResponse
import run_v571, run_v551, server

VERSION='5.7.2'
server.app.version=VERSION
_base=run_v571.build_stock_v571


def _compat(d):
    if not isinstance(d,dict): return d
    # Collections the legacy renderer directly iterates/maps.
    if not isinstance(d.get('source_status'),list): d['source_status']=[]
    if not isinstance(d.get('evidence'),list): d['evidence']=[]
    if not isinstance(d.get('events'),list): d['events']=[]
    if not isinstance(d.get('research_sources'),list): d['research_sources']=[]
    if not isinstance(d.get('estimates'),list): d['estimates']=[]
    # Objects the renderer dereferences directly.
    for k in ('technical','revenue','financial','cashflow','eps_stack','valuation','quote','snapshot','per','research','official_financial','module_status'):
        if not isinstance(d.get(k),dict): d[k]={}
    d['technical'].setdefault('series',[])
    d['revenue'].setdefault('series',[])
    d['valuation'].setdefault('scenarios',[])
    # Valid timestamp fallback: prevents `Invalid Date` while still marking partial data.
    raw=d.get('generated_at') or d.get('as_of') or d.get('updated_at')
    try:
        datetime.fromisoformat(str(raw).replace('Z','+00:00'))
        stamp=str(raw)
    except Exception:
        stamp=datetime.now(timezone.utc).isoformat()
    d['generated_at']=stamp
    d['as_of']=d.get('as_of') or stamp
    # UI text fields should be safe strings.
    d['company_name']=d.get('company_name') or d.get('name') or str(d.get('ticker') or '')
    d['status_text']=d.get('status_text') or ('資料載入中' if d.get('report_partial') else '資料已更新')
    d['version']=VERSION
    d['ui_partial_contract']='v2'
    return d

async def build_stock_v572(ticker:str,force_refresh:bool=False):
    return _compat(await _base(ticker,force_refresh=force_refresh))

server.build_stock=build_stock_v572

_oldidx=run_v551._patched_index
def _idx():
    text=_oldidx()
    for v in ('5.7.1','5.7.0','5.6.6','5.6.5','5.6.4','5.6.3','5.6.2','5.6.1','5.6.0','5.5.9','5.5.1'):
        text=text.replace(v,VERSION)
    return text
run_v551._patched_index=_idx
_oldsw=run_v551._patched_sw
def _sw():
    text=_oldsw()
    for v in ('5.7.1','5.7.0','5.6.6','5.6.5','5.6.4','5.6.3','5.6.2','5.6.1','5.6.0','5.5.9','5.5.1'):
        text=text.replace(f'ai-stock-v{v}',f'ai-stock-v{VERSION}')
    return text
run_v551._patched_sw=_sw

@server.app.middleware('http')
async def runtime(request:Request,call_next):
    if request.url.path=='/health':
        return JSONResponse({'status':'ok','version':VERSION,'mode':'segmented-resilient-ui-contract','source_status_array':True,'valid_dates':True,'legacy_collections_safe':True},headers={'Cache-Control':'no-store'})
    resp=await call_next(request);resp.headers['X-AI-Stock-Version']=VERSION;return resp
app=server.app
