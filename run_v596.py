"""V5.9.6 Financial + Version Integrity.
- Retries official financial/EPS recovery after the V5.9.5 pipeline completes.
- Synchronizes financial source status from actual recovered payload.
- Uses a single runtime version source (/health) for the frontend.
"""
from __future__ import annotations

import asyncio
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

import run_v595, run_v582, run_v594, server

VERSION = "5.9.6"
server.app.version = VERSION


def _financial_asof(d: dict):
    fin = d.get("financial") if isinstance(d.get("financial"), dict) else {}
    off = d.get("official_financial") if isinstance(d.get("official_financial"), dict) else {}
    eps = d.get("eps_stack") if isinstance(d.get("eps_stack"), dict) else {}
    integ = d.get("financial_integrity") if isinstance(d.get("financial_integrity"), dict) else {}
    return (
        fin.get("period") or fin.get("statement_date") or
        off.get("period") or off.get("statement_date") or
        integ.get("latest_period") or eps.get("quarter_period") or eps.get("ytd_period")
    )


def _sync_financial_status(d: dict):
    fin = d.get("financial") if isinstance(d.get("financial"), dict) else {}
    off = d.get("official_financial") if isinstance(d.get("official_financial"), dict) else {}
    eps = d.get("eps_stack") if isinstance(d.get("eps_stack"), dict) else {}
    ok = bool(fin or off or eps)
    asof = _financial_asof(d)
    rows = d.get("source_status") if isinstance(d.get("source_status"), list) else []
    found = False
    for row in rows:
        if row.get("name") == "財務報表":
            found = True
            row.update({
                "status": "ok" if ok else "missing",
                "as_of": asof,
                "dataset": (fin.get("source") or off.get("source") or "TWSE/MOPS official financial") if ok else "TWSE/MOPS official financial",
            })
    if not found:
        rows.append({"name":"財務報表","dataset":"TWSE/MOPS official financial","as_of":asof,"status":"ok" if ok else "missing"})
    d["source_status"] = rows
    return d


async def build_stock_v596(ticker: str, force_refresh: bool = False):
    d = await run_v595.build_stock_v595(ticker, force_refresh=force_refresh)
    diag = d.setdefault("pipeline_diagnostics", {})

    before = bool(d.get("financial") or d.get("official_financial") or d.get("eps_stack"))
    if not before:
        try:
            d = await asyncio.wait_for(run_v582._repair_financial(ticker, d), timeout=34.0)
            diag["financial_retry_v596"] = "ok" if (d.get("financial") or d.get("official_financial") or d.get("eps_stack")) else "empty"
        except Exception as e:
            diag["financial_retry_v596"] = f"{type(e).__name__}: {str(e)[:180]}"
    else:
        diag["financial_retry_v596"] = "already_available"

    d = _sync_financial_status(d)
    try:
        d["scores"] = server.scores(
            d.get("technical") or {}, d.get("revenue") or {}, d.get("flow") or {},
            d.get("per") or {}, d.get("financial") or {}, d.get("research") or {}
        )
    except Exception:
        pass
    try:
        d = run_v594._reconfidence(d)
    except Exception:
        pass
    d["version"] = VERSION
    diag["financial_available_v596"] = bool(d.get("financial") or d.get("official_financial") or d.get("eps_stack"))
    diag["financial_asof_v596"] = _financial_asof(d)
    return d


app = server.app
for r in list(app.routes):
    if getattr(r, "path", None) in ("/api/stock/{ticker}", "/"):
        try: app.routes.remove(r)
        except ValueError: pass

@app.get("/api/stock/{ticker}")
async def api_stock_v596(ticker: str, refresh: int = 0):
    return await build_stock_v596(ticker.strip(), force_refresh=bool(refresh))

@app.get("/api/v596/stock/{ticker}")
async def api_v596(ticker: str, refresh: int = 0):
    return await build_stock_v596(ticker.strip(), force_refresh=bool(refresh))

@app.get("/", response_class=HTMLResponse)
async def root_v596():
    text = (server.ROOT / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(text, headers={
        "Cache-Control":"no-store, no-cache, must-revalidate, max-age=0",
        "Pragma":"no-cache", "Expires":"0", "X-App-Version":VERSION,
    })

@app.middleware("http")
async def v596_runtime(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({
            "status":"ok", "version":VERSION, "mode":"financial-version-integrity",
            "financial_retry":"TWSE/MOPS official income statement + EPS stack",
            "institutional":"TWSE T86 OpenAPI + daily fallback",
        }, headers={"Cache-Control":"no-store"})
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp
