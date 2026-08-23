"""V5.8.3 Institutional & Margin Recovery.
Run bounded TWSE T86 / MI_MARGN repair in the foreground, then preserve background cache.
"""
from __future__ import annotations
import asyncio
from typing import Any
from fastapi import Request
from fastapi.responses import JSONResponse
import run_v582, run_v566, run_v560, run_v551, server

VERSION = "5.8.3"
server.app.version = VERSION


def _has_inst(d: dict[str, Any]) -> bool:
    cf=d.get("cashflow") or {}
    return bool(cf.get("institutional")) or bool(cf.get("institutional_rows"))

def _has_margin(d: dict[str, Any]) -> bool:
    cf=d.get("cashflow") or {}
    return bool(cf.get("margin_history")) or cf.get("margin_balance") is not None

def _sync_flow(d: dict[str, Any]):
    cf=d.get("cashflow") if isinstance(d.get("cashflow"),dict) else {}
    flow=d.get("flow") if isinstance(d.get("flow"),dict) else {}
    for k,v in cf.items():
        if v not in (None,"",[],{}): flow[k]=v
    d["cashflow"]=cf; d["flow"]=flow
    return d

async def build_stock_v583(ticker: str, force_refresh: bool=False):
    d=await run_v582.build_stock_v582(ticker, force_refresh=force_refresh)
    diag=d.get("pipeline_diagnostics") if isinstance(d.get("pipeline_diagnostics"),dict) else {}

    # First merge any completed background result.
    d=run_v566._merge_official_cache(d, run_v566._official_cache.get(ticker))

    # If still missing, make one bounded foreground official attempt so users do not
    # need a second request merely to see positioning data.
    if not _has_inst(d):
        try:
            d=await asyncio.wait_for(run_v560.repair_institutional_official(ticker,d), timeout=12.0)
            diag["institutional_foreground"]="ok" if _has_inst(d) else "empty"
        except Exception as e:
            diag["institutional_foreground"]=f"{type(e).__name__}"
    if not _has_margin(d):
        try:
            d=await asyncio.wait_for(run_v560.repair_margin_official(ticker,d), timeout=12.0)
            diag["margin_foreground"]="ok" if _has_margin(d) else "empty"
        except Exception as e:
            diag["margin_foreground"]=f"{type(e).__name__}"

    d=_sync_flow(d)
    cf=d.get("cashflow") or {}
    # Normalize aliases expected by UI/source-status.
    if cf.get("institutional_rows"):
        inst=cf.get("institutional") or {}
        for who,prefix in (("foreign","foreign"),("trust","trust"),("dealer","dealer")):
            for n in (1,5,20):
                net=((inst.get(who) or {}).get(str(n)) or {}).get("net")
                if net is not None: cf[f"{prefix}_{n}"]=net
        cf["last_date"]=cf.get("last_date") or d.get("price_as_of")
    if cf.get("margin_short_as_of"):
        cf["margin_last_date"]=cf.get("margin_short_as_of")
    d["cashflow"]=cf; d=_sync_flow(d)

    try:
        d["scores"]=server.scores(d.get("technical") or {},d.get("revenue") or {},d.get("flow") or {},d.get("per") or {},d.get("financial") or {},d.get("research") or {})
    except Exception: pass
    try: d=run_v582._evidence_confidence(d)
    except Exception: pass

    for row in d.get("source_status") or []:
        if row.get("name")=="三大法人":
            row["as_of"]=cf.get("last_date")
            row["status"]="ok" if _has_inst(d) else "updating"
        elif row.get("name")=="融資融券":
            row["as_of"]=cf.get("margin_short_as_of") or cf.get("margin_last_date")
            row["status"]="ok" if _has_margin(d) else "updating"

    # Keep background refresh alive even when foreground endpoint was temporarily empty.
    run_v566._schedule_official(ticker,d)
    diag.update({"mode":"institutional-margin-recovery","institutional_recovered":_has_inst(d),"margin_recovered":_has_margin(d),"institutional_rows":cf.get("institutional_rows"),"margin_rows":cf.get("margin_short_rows"),"confidence":d.get("confidence")})
    d["pipeline_diagnostics"]=diag; d["version"]=VERSION
    d["data_policy"]=(d.get("data_policy") or "")+" V5.8.3：T86 三大法人與 MI_MARGN 融資融券改為限時前景官方抓取，背景快取持續作備援，並同步 flow/可信度。"
    return d

server.build_stock=build_stock_v583
_oldidx=run_v551._patched_index
def _idx():
    text=_oldidx()
    for v in ("5.8.2","5.8.1","5.8.0","5.7.4","5.7.3","5.7.2","5.7.1","5.7.0","5.6.6","5.5.1"): text=text.replace(v,VERSION)
    return text
run_v551._patched_index=_idx
_oldsw=run_v551._patched_sw
def _sw():
    text=_oldsw()
    for v in ("5.8.2","5.8.1","5.8.0","5.7.4","5.7.3","5.7.2","5.7.1","5.7.0","5.6.6","5.5.1"): text=text.replace(f"ai-stock-v{v}",f"ai-stock-v{VERSION}")
    return text
run_v551._patched_sw=_sw

@server.app.middleware("http")
async def runtime(request:Request,call_next):
    if request.url.path=="/health": return JSONResponse({"status":"ok","version":VERSION,"mode":"institutional-margin-recovery","institutional":"TWSE T86 foreground+cache","margin_short":"TWSE MI_MARGN foreground+cache"},headers={"Cache-Control":"no-store"})
    resp=await call_next(request); resp.headers["X-AI-Stock-Version"]=VERSION; return resp
app=server.app
