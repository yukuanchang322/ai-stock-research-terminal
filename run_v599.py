"""V5.9.9 Core Data Stabilization.

- Restores the proven V5.9.7 primary report pipeline for name/revenue/price/T86/margin.
- Uses server.fetch_official_income_statement() for financial recovery instead of the V5.9.8 duplicate parser.
- Retries the V5.9.7 core pipeline once only when critical core fields (company name/revenue) are missing.
- Keeps financial diagnostics as an explicit endpoint so diagnostics never block/degrade the main report.
- Makes /health the single visible runtime version source and aggressively removes legacy version badges.
"""
from __future__ import annotations

import asyncio
import re
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

import run_v597, run_v596, run_v594, server

VERSION = "5.9.9"
server.app.version = VERSION


def _has_revenue(d: dict) -> bool:
    r=d.get("revenue")
    return isinstance(r,dict) and bool(r.get("series"))


def _has_name(d: dict) -> bool:
    for k in ("name","company_name"):
        v=d.get(k)
        if v and str(v).strip() not in (str(d.get("ticker") or ""), "—"):
            return True
    return False


def _merge_missing_core(base: dict, retry: dict) -> dict:
    out=dict(base)
    keys=("name","company_name","revenue","technical","flow","cashflow","price_as_of","per","market","sector","market_type")
    for k in keys:
        if k not in out or out.get(k) in (None,"",{},[]):
            if retry.get(k) not in (None,"",{},[]):
                out[k]=retry.get(k)
    # Prefer a real company name over ticker echo.
    if not _has_name(out) and _has_name(retry):
        if retry.get("name"): out["name"]=retry.get("name")
        if retry.get("company_name"): out["company_name"]=retry.get("company_name")
    return out


async def _recover_financial(ticker: str, d: dict) -> dict:
    if d.get("financial") or d.get("official_financial"):
        return d
    diag=d.setdefault("pipeline_diagnostics",{})
    try:
        snap=await asyncio.wait_for(server.fetch_official_income_statement(ticker),timeout=38)
    except Exception as e:
        diag["financial_v599"]={"status":"error","error":f"{type(e).__name__}: {str(e)[:160]}"}
        return d
    if not isinstance(snap,dict) or not snap.get("official") or not (snap.get("ytd_eps") is not None or snap.get("revenue_ytd") is not None):
        diag["financial_v599"]={"status":"missing","candidate_hits":snap.get("candidate_hits") if isinstance(snap,dict) else None,
                                "errors":snap.get("errors") if isinstance(snap,dict) else None}
        return d
    try:
        eps_stack=await asyncio.wait_for(server.build_eps_stack(ticker,[],snap,{}),timeout=12)
    except Exception:
        eps_stack={"ytd_eps":snap.get("ytd_eps"),"quarter_period":snap.get("period"),"source":snap.get("source")}
    try:
        integrity=server.assess_financial_integrity(snap,eps_stack,__import__('datetime').date.today())
    except Exception:
        integrity={}
    fin={
        "statement_date":snap.get("statement_date") or snap.get("period"),"period":snap.get("period"),
        "source":snap.get("source"),"official":True,"partial":False,
        "ytd_eps":snap.get("ytd_eps"),"quarter_eps":eps_stack.get("quarter_eps"),"ttm_eps":eps_stack.get("ttm_eps"),
        "revenue":snap.get("revenue_ytd"),"gross_profit":snap.get("gross_profit_ytd"),
        "operating_income":snap.get("operating_income_ytd"),"net_income":snap.get("net_income_ytd"),
    }
    rev=fin.get("revenue")
    if rev not in (None,0):
        if fin.get("gross_profit") is not None: fin["gross_margin"]=fin["gross_profit"]/rev*100
        if fin.get("operating_income") is not None: fin["operating_margin"]=fin["operating_income"]/rev*100
        if fin.get("net_income") is not None: fin["net_margin"]=fin["net_income"]/rev*100
    d["official_financial"]=snap; d["eps_stack"]=eps_stack; d["financial_integrity"]=integrity; d["financial"]=fin
    diag["financial_v599"]={"status":"ok","period":snap.get("period"),"source":snap.get("source"),"endpoint":snap.get("endpoint"),"completeness":snap.get("completeness")}
    try: d["valuation"]=server.model_valuation(d.get("price"),d.get("per") or {},eps_stack,d.get("research") or {},integrity)
    except Exception: pass
    return d


async def build_stock_v599(ticker: str, force_refresh: bool=False):
    d=await run_v597.build_stock_v597(ticker,force_refresh=force_refresh)
    # Critical core-data circuit breaker: if company name or monthly revenue is missing,
    # retry the known-stable core pipeline once with fresh upstream calls and merge only missing fields.
    if (not _has_revenue(d) or not _has_name(d)) and not force_refresh:
        try:
            retry=await asyncio.wait_for(run_v597.build_stock_v597(ticker,force_refresh=True),timeout=55)
            d=_merge_missing_core(d,retry)
            d.setdefault("pipeline_diagnostics",{})["core_retry_v599"]={"status":"done","revenue":_has_revenue(d),"name":_has_name(d)}
        except Exception as e:
            d.setdefault("pipeline_diagnostics",{})["core_retry_v599"]={"status":"error","error":f"{type(e).__name__}: {str(e)[:140]}"}
    d=await _recover_financial(ticker,d)
    try: d=run_v596._sync_financial_status(d)
    except Exception: pass
    try:
        d["scores"]=server.scores(d.get("technical") or {},d.get("revenue") or {},d.get("flow") or {},d.get("per") or {},d.get("financial") or {},d.get("research") or {})
    except Exception: pass
    try: d=run_v594._reconfidence(d)
    except Exception: pass
    d["version"]=VERSION
    return d


app=server.app
for r in list(app.routes):
    if getattr(r,"path",None) in ("/api/stock/{ticker}","/","/health"):
        try: app.routes.remove(r)
        except ValueError: pass

@app.get("/api/stock/{ticker}")
async def api_stock_v599(ticker:str,refresh:int=0):
    return await build_stock_v599(ticker.strip(),force_refresh=bool(refresh))

@app.get("/api/v599/stock/{ticker}")
async def api_v599(ticker:str,refresh:int=0):
    return await build_stock_v599(ticker.strip(),force_refresh=bool(refresh))

@app.get("/api/v599/financial-diagnostics/{ticker}")
async def financial_diagnostics_v599(ticker:str):
    # Existing server diagnostic probes every official source. It is deliberately isolated
    # from the normal report path so it cannot suppress monthly revenue/name/technical data.
    return await server.diagnose_official_financial_sources(ticker.strip())

@app.get("/health")
async def health_v599():
    return JSONResponse({"status":"ok","version":VERSION,"mode":"stable-core-plus-isolated-financial-recovery",
                         "core":"V5.9.7 stable pipeline","financial":"server proven official MOPS pipeline","diagnostics":"isolated"},
                        headers={"Cache-Control":"no-store"})

@app.get("/",response_class=HTMLResponse)
async def root_v599():
    text=(server.ROOT/"index.html").read_text(encoding="utf-8")
    text=re.sub(r'<span[^>]*data-app-version[^>]*>.*?</span>','',text,flags=re.S|re.I)
    text=re.sub(r'AI Stock Research Terminal V5\.\d+(?:\.\d+)?',f'AI Stock Research Terminal V{VERSION}',text)
    for asset in ("styles.css","app.js","recovery.js","v547_hotfix.js"):
        text=re.sub(rf'{re.escape(asset)}(?:\?v=[^\"\']+)?',f'{asset}?v={VERSION}',text)
    text=re.sub(r'/sw\.js(?:\?v=[^\"\']+)?',f'/sw.js?v={VERSION}',text)
    cleanup=f"""<script id='v599-cleanup'>
    (()=>{{const V='{VERSION}'; const clean=()=>{{
      const s=document.querySelector('.cloud-status'); if(!s)return;
      [...s.children].forEach(n=>{{const t=(n.textContent||'').trim(); if(/^V5\\.\\d+\\.\\d+$/.test(t))n.remove();}});
      document.title='AI Stock Research Terminal V'+V;
    }}; clean(); new MutationObserver(clean).observe(document.body,{{subtree:true,childList:true}});}})();
    </script>"""
    text=text.replace("</body>",cleanup+"</body>")
    return HTMLResponse(text,headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0","Pragma":"no-cache","Expires":"0","Clear-Site-Data":"\"cache\"","X-App-Version":VERSION})

@app.middleware("http")
async def v599_runtime(request:Request,call_next):
    resp=await call_next(request)
    resp.headers["X-AI-Stock-Version"]=VERSION
    return resp
