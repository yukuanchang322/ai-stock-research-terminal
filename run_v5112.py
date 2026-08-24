"""V5.11.2 hard repair.

Goals:
- force a network-only shell and retire legacy service workers/cache,
- preserve all successful V5.11.0 datasets,
- retry official financial and T86 institutional repair independently,
- normalize partial official financial payloads so UI/source status can use them,
- keep company identity stable.
"""
from __future__ import annotations

import asyncio
import copy
import re
from fastapi.responses import HTMLResponse, JSONResponse

import run_v5110, run_v5103, run_v5104, run_v596, run_v594, server

VERSION = "5.11.2"
app = server.app
server.app.version = VERSION


def _has_financial(d: dict) -> bool:
    return bool(
        (isinstance(d.get("financial"), dict) and d.get("financial")) or
        (isinstance(d.get("official_financial"), dict) and d.get("official_financial")) or
        (isinstance(d.get("eps_stack"), dict) and d.get("eps_stack"))
    )


def _has_inst(d: dict) -> bool:
    cf = d.get("cashflow") if isinstance(d.get("cashflow"), dict) else {}
    if cf.get("institutional"):
        return True
    fl = d.get("flow") if isinstance(d.get("flow"), dict) else {}
    return any(fl.get(k) is not None for k in (
        "foreign_1","foreign_5","foreign_20","trust_1","trust_5","trust_20","dealer_1","dealer_5","dealer_20"
    ))


def _preserve_merge(base: dict, new: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (new or {}).items():
        if k in ("pipeline_diagnostics", "source_status"):
            continue
        if v not in (None, "", [], {}):
            out[k] = v
    out.setdefault("pipeline_diagnostics", {}).update((new or {}).get("pipeline_diagnostics") or {})
    return out


def _normalize_financial(d: dict) -> dict:
    if not isinstance(d.get("financial"), dict) or not d.get("financial"):
        src = d.get("official_financial") if isinstance(d.get("official_financial"), dict) else None
        if src:
            d["financial"] = copy.deepcopy(src)
        elif isinstance(d.get("eps_stack"), dict) and d.get("eps_stack"):
            asof = None
            try:
                asof = run_v596._financial_asof(d)
            except Exception:
                pass
            d["financial"] = {"as_of": asof, "quarter": asof, "eps_stack": copy.deepcopy(d.get("eps_stack"))}
    try:
        d = run_v596._sync_financial_status(d)
    except Exception:
        pass
    return d


def _normalize_inst_status(d: dict) -> dict:
    rows = d.get("source_status") if isinstance(d.get("source_status"), list) else []
    cf = d.get("cashflow") if isinstance(d.get("cashflow"), dict) else {}
    ok = _has_inst(d)
    found = False
    for r in rows:
        if isinstance(r, dict) and r.get("name") == "三大法人":
            found = True
            r["status"] = "ok" if ok else "missing"
            if cf.get("last_date"): r["as_of"] = cf.get("last_date")
            r["dataset"] = cf.get("institutional_source") or "TWSE T86 official"
    if not found:
        rows.append({"name":"三大法人","status":"ok" if ok else "missing","as_of":cf.get("last_date"),"dataset":cf.get("institutional_source") or "TWSE T86 official"})
    d["source_status"] = rows
    return d


async def build_stock_v5112(ticker: str, force_refresh: bool = False):
    ticker = ticker.strip()
    d = await run_v5110.build_stock_v5110(ticker, force_refresh=force_refresh)
    if not isinstance(d, dict):
        return d
    d = copy.deepcopy(d)
    diag = d.setdefault("pipeline_diagnostics", {})

    # Identity is cheap and independent.
    try:
        d = await asyncio.wait_for(run_v5104._repair_identity(ticker, d), timeout=15)
    except Exception as e:
        diag["identity_v5112"] = {"status":"error","error":str(e)[:180]}

    # Financial: two bounded attempts, preserving the rest of the payload.
    if not _has_financial(d):
        for attempt in (1, 2):
            try:
                repaired = await asyncio.wait_for(run_v5103._repair_financial(ticker, copy.deepcopy(d)), timeout=48)
                if isinstance(repaired, dict):
                    d = _preserve_merge(d, repaired)
                if _has_financial(d):
                    diag["financial_v5112"] = {"status":"recovered","attempt":attempt}
                    break
            except Exception as e:
                diag[f"financial_v5112_attempt_{attempt}"] = {"status":"error","error":str(e)[:200]}
    d = _normalize_financial(d)

    # Institutional: retry latest + daily + history path independently.
    if not _has_inst(d):
        for attempt in (1, 2):
            try:
                repaired = await asyncio.wait_for(run_v5104._repair_institutional_v5104(ticker, copy.deepcopy(d)), timeout=58)
                if isinstance(repaired, dict):
                    d = _preserve_merge(d, repaired)
                if _has_inst(d):
                    diag["institutional_v5112"] = {"status":"recovered","attempt":attempt}
                    break
            except Exception as e:
                diag[f"institutional_v5112_attempt_{attempt}"] = {"status":"error","error":str(e)[:200]}
    d = _normalize_inst_status(d)

    try:
        d["scores"] = server.scores(d.get("technical") or {}, d.get("revenue") or {}, d.get("flow") or {}, d.get("per") or {}, d.get("financial") or {}, d.get("research") or {})
    except Exception as e:
        diag["score_v5112"] = {"status":"error","error":str(e)[:180]}
    try:
        d = run_v594._reconfidence(d)
    except Exception:
        pass

    d["version"] = VERSION
    diag["pipeline_v5112"] = {
        "status":"ok",
        "financial":"ok" if _has_financial(d) else "missing",
        "institutional":"ok" if _has_inst(d) else "missing",
        "policy":"preserve-success + isolated official retries",
    }
    return d


for r in list(app.routes):
    if getattr(r, "path", None) in ("/", "/health", "/api/stock/{ticker}"):
        try: app.routes.remove(r)
        except ValueError: pass


@app.get("/api/stock/{ticker}")
async def api_stock_v5112(ticker: str, refresh: int = 0):
    d = await build_stock_v5112(ticker, bool(refresh))
    return JSONResponse(d, headers={
        "Cache-Control":"no-store, no-cache, must-revalidate, max-age=0",
        "Pragma":"no-cache",
        "X-AI-Stock-Version":VERSION,
    })


@app.get("/api/v5112/diagnostics/{ticker}")
async def diagnostics_v5112(ticker: str, refresh: int = 1):
    d = await build_stock_v5112(ticker, bool(refresh))
    return JSONResponse({
        "version":VERSION,
        "ticker":ticker,
        "name":d.get("name") or d.get("company_name"),
        "financial_available":_has_financial(d),
        "financial_asof":run_v596._financial_asof(d),
        "institutional_available":_has_inst(d),
        "flow":d.get("flow") or {},
        "source_status":d.get("source_status") or [],
        "pipeline_diagnostics":d.get("pipeline_diagnostics") or {},
    }, headers={"Cache-Control":"no-store"})


@app.get("/health")
async def health_v5112():
    return JSONResponse({"status":"ok","version":VERSION,"mode":"hard-data-repair-network-shell"}, headers={"Cache-Control":"no-store","X-AI-Stock-Version":VERSION})


@app.get("/", response_class=HTMLResponse)
async def root_v5112():
    text = (server.ROOT / "index.html").read_text(encoding="utf-8")
    # Rewrite every visible/static legacy version token, remove all legacy scripts,
    # and boot only one app.js from the current runtime.
    text = re.sub(r'V5\.\d+(?:\.\d+)?', f'V{VERSION}', text)
    text = re.sub(r'v=5\.\d+(?:\.\d+)?', f'v={VERSION}', text)
    text = re.sub(r'\s*<script[^>]+src="(?:recovery|v\d+_hotfix)\.js[^\"]*"[^>]*></script>', '', text, flags=re.I)
    text = re.sub(r'\s*<script[^>]+src="app\.js[^\"]*"[^>]*></script>', '', text, flags=re.I)
    text = re.sub(r'\s*<script>if\(\'serviceWorker\'.*?</script>', '', text, flags=re.S|re.I)
    text = re.sub(r'href="styles\.css(?:\?v=[^"]+)?"', f'href="/static/styles.css?v={VERSION}"', text)
    text = re.sub(r'href="/static/manifest\.webmanifest(?:\?v=[^"]+)?"', f'href="/static/manifest.webmanifest?v={VERSION}"', text)
    boot = f'''\n<script>\nwindow.AI_STOCK_VERSION="{VERSION}";\n(async()=>{{try{{for(const r of await navigator.serviceWorker?.getRegistrations?.()||[])await r.unregister();for(const k of await caches.keys())await caches.delete(k);}}catch(e){{}}}})();\nwindow.addEventListener('DOMContentLoaded',()=>{{document.querySelectorAll('[data-app-version]').forEach(x=>x.textContent='V{VERSION}')}});\n</script>\n<script src="/static/app.js?v={VERSION}"></script>\n'''
    text = text.replace("</body>", boot + "</body>")
    return HTMLResponse(text, headers={
        "Cache-Control":"no-store, no-cache, must-revalidate, max-age=0",
        "Pragma":"no-cache","Expires":"0","Clear-Site-Data":"\"cache\"",
        "X-AI-Stock-Version":VERSION,
    })


@app.middleware("http")
async def runtime_v5112(request, call_next):
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp
