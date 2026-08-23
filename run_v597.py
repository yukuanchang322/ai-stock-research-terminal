"""V5.9.7 Direct Financial OpenAPI Recovery.
- Adds a direct TWSE OpenAPI financial fallback for listed-company income statements.
- Rebuilds financial/EPS payload from the recovered official snapshot.
- Removes the duplicate static version marker at response time and cache-busts assets.
"""
from __future__ import annotations

import asyncio
import re
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

import run_v596, run_v594, server

VERSION = "5.9.7"
server.app.version = VERSION

FIN_PATHS = [
    ("/opendata/t187ap06_L_ci", "一般業"),
    ("/opendata/t187ap06_L_mim", "異業"),
    ("/opendata/t187ap06_L_basi", "金融業"),
    ("/opendata/t187ap06_L_bd", "證券期貨業"),
    ("/opendata/t187ap06_L_fh", "金控業"),
    ("/opendata/t187ap06_L_ins", "保險業"),
    ("/opendata/t187ap06_X_ci", "公發一般業"),
    ("/opendata/t187ap06_X_mim", "公發異業"),
]


def _period_key(x: dict):
    y = x.get("fiscal_year") or 0
    q = x.get("fiscal_quarter") or 0
    return (int(y or 0), int(q or 0), int(x.get("completeness") or 0))


async def _direct_twse_financial(ticker: str):
    hits=[]; meta=[]
    for path, label in FIN_PATHS:
        try:
            rows = await asyncio.wait_for(server.openapi_json(server.TWSE_OPENAPI, path), timeout=12)
            found=0
            for row in rows:
                snap = server._official_row_to_snapshot(row, f"TWSE OpenAPI {label}", path, "上市/公發", "detail")
                if snap and str(snap.get("company_code") or "").strip() == str(ticker).strip():
                    hits.append(snap); found += 1
            meta.append({"path":path,"rows":len(rows),"found":found})
        except Exception as e:
            meta.append({"path":path,"error":f"{type(e).__name__}: {str(e)[:120]}"})
    if not hits:
        return None, meta
    hits.sort(key=_period_key, reverse=True)
    return hits[0], meta


async def _apply_direct_financial(ticker: str, d: dict):
    diag=d.setdefault("pipeline_diagnostics",{})
    snap, meta = await _direct_twse_financial(ticker)
    diag["financial_openapi_v597"] = meta
    if not snap:
        diag["financial_openapi_v597_status"]="no_match"
        return d
    try:
        snap = await asyncio.wait_for(server.reconcile_official_financial_snapshot(ticker, snap), timeout=10)
    except Exception:
        pass
    try:
        eps = await asyncio.wait_for(server.build_eps_stack(ticker, [], snap, {}), timeout=12)
    except Exception:
        eps = {}
    try:
        integ = server.assess_financial_integrity(snap, eps, __import__('datetime').datetime.now().date())
    except Exception:
        integ = {}
    fin = {
        "statement_date": snap.get("statement_date") or snap.get("period"),
        "period": snap.get("period"),
        "source": snap.get("source") or "TWSE OpenAPI official financial",
        "official": True,
        "ytd_eps": snap.get("ytd_eps"),
        "quarter_eps": eps.get("quarter_eps"),
        "ttm_eps": eps.get("ttm_eps"),
        "revenue": snap.get("revenue_ytd"),
        "gross_profit": snap.get("gross_profit_ytd"),
        "operating_income": snap.get("operating_income_ytd"),
        "net_income": snap.get("net_income_ytd"),
    }
    if fin.get("revenue"):
        if fin.get("gross_profit") is not None: fin["gross_margin"] = fin["gross_profit"] / fin["revenue"] * 100
        if fin.get("operating_income") is not None: fin["operating_margin"] = fin["operating_income"] / fin["revenue"] * 100
        if fin.get("net_income") is not None: fin["net_margin"] = fin["net_income"] / fin["revenue"] * 100
    d["official_financial"] = snap
    d["eps_stack"] = eps
    d["financial_integrity"] = integ
    d["financial"] = fin
    try:
        d["valuation"] = server.model_valuation(d.get("price"), d.get("per") or {}, eps, d.get("research") or {}, integ)
    except Exception:
        pass
    diag["financial_openapi_v597_status"]="ok"
    diag["financial_openapi_v597_period"]=fin.get("period")
    return d


async def build_stock_v597(ticker: str, force_refresh: bool=False):
    d = await run_v596.build_stock_v596(ticker, force_refresh=force_refresh)
    has_fin = bool(d.get("financial") or d.get("official_financial") or d.get("eps_stack"))
    if not has_fin:
        try:
            d = await asyncio.wait_for(_apply_direct_financial(ticker, d), timeout=90)
        except Exception as e:
            d.setdefault("pipeline_diagnostics",{})["financial_openapi_v597_fatal"] = f"{type(e).__name__}: {str(e)[:180]}"
    d = run_v596._sync_financial_status(d)
    try:
        d["scores"] = server.scores(d.get("technical") or {},d.get("revenue") or {},d.get("flow") or {},d.get("per") or {},d.get("financial") or {},d.get("research") or {})
    except Exception:
        pass
    try:
        d = run_v594._reconfidence(d)
    except Exception:
        pass
    d["version"] = VERSION
    return d

app=server.app
for r in list(app.routes):
    if getattr(r,"path",None) in ("/api/stock/{ticker}","/"):
        try: app.routes.remove(r)
        except ValueError: pass

@app.get("/api/stock/{ticker}")
async def api_stock_v597(ticker:str, refresh:int=0):
    return await build_stock_v597(ticker.strip(), force_refresh=bool(refresh))

@app.get("/api/v597/stock/{ticker}")
async def api_v597(ticker:str, refresh:int=0):
    return await build_stock_v597(ticker.strip(), force_refresh=bool(refresh))

@app.get("/", response_class=HTMLResponse)
async def root_v597():
    text=(server.ROOT/"index.html").read_text(encoding="utf-8")
    # /health is the only visible runtime version source.
    text=re.sub(r'<span class="status-sep">•</span>\s*<span data-app-version>.*?</span>\s*<span class="status-sep">•</span>', '<span class="status-sep">•</span>', text, flags=re.S)
    text=re.sub(r'<span data-app-version>.*?</span>', '', text, flags=re.S)
    text=re.sub(r'AI Stock Research Terminal V5\.\d+(?:\.\d+)?', f'AI Stock Research Terminal V{VERSION}', text)
    for asset in ("styles.css","app.js","recovery.js","v547_hotfix.js"):
        text=re.sub(rf'{re.escape(asset)}(?:\?v=[^\"\']+)?', f'{asset}?v={VERSION}', text)
    text=re.sub(r'/sw\.js(?:\?v=[^\"\']+)?', f'/sw.js?v={VERSION}', text)
    return HTMLResponse(text,headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0","Pragma":"no-cache","Expires":"0","X-App-Version":VERSION})

@app.middleware("http")
async def runtime_v597(request:Request,call_next):
    if request.url.path=="/health":
        return JSONResponse({"status":"ok","version":VERSION,"mode":"direct-financial-openapi","financial":"TWSE OpenAPI t187ap06 schemas + existing MOPS repair","institutional":"TWSE T86 recovered"},headers={"Cache-Control":"no-store"})
    resp=await call_next(request); resp.headers["X-AI-Stock-Version"]=VERSION; return resp
