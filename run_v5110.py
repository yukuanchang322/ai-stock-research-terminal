"""V5.11.0 unified data pipeline.

Stabilizes V5.10.4 by preserving successful datasets, independently repairing
identity/financial/institutional sources, and serving a clean cache-free shell.
"""
from __future__ import annotations

import asyncio
import copy
import re
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

import run_v5104, run_v5103, run_v594, server

VERSION = "5.11.0"
app = server.app
server.app.version = VERSION


def _ok(v):
    if v is None: return False
    if isinstance(v, (dict, list, tuple, set, str)): return bool(v)
    return True


def _merge_preserve(base: dict, repaired: dict) -> dict:
    """Never let a failed/empty repair erase a dataset that already succeeded."""
    out = copy.deepcopy(base)
    for k, v in (repaired or {}).items():
        if k in ("pipeline_diagnostics", "source_status"):
            continue
        if _ok(v) or not _ok(out.get(k)):
            out[k] = v
    diag = out.setdefault("pipeline_diagnostics", {})
    diag.update((repaired or {}).get("pipeline_diagnostics") or {})
    # source_status is rebuilt from the final merged payload below.
    return out


def _source_status(d: dict):
    rows = {r.get("name"): dict(r) for r in (d.get("source_status") or []) if isinstance(r, dict) and r.get("name")}
    checks = {
        "財務報表": (d.get("financial"), (d.get("financial") or {}).get("as_of") or (d.get("financial") or {}).get("quarter")),
        "三大法人": ((d.get("cashflow") or {}).get("institutional"), (d.get("cashflow") or {}).get("last_date")),
    }
    for name, (value, as_of) in checks.items():
        row = rows.get(name, {"name": name})
        row["status"] = "ok" if _ok(value) else "missing"
        if as_of: row["as_of"] = as_of
        rows[name] = row
    d["source_status"] = list(rows.values())
    return d


async def build_stock_v5110(ticker: str, force_refresh: bool = False):
    ticker = ticker.strip()
    # Start with the complete V5.10.3 result. Each recovery is isolated so one
    # provider failure cannot destroy another provider's successful payload.
    base = await run_v5103.build_stock_v5103(ticker, force_refresh=force_refresh)
    if not isinstance(base, dict): return base
    d = copy.deepcopy(base)

    try:
        ident = await run_v5104._repair_identity(ticker, copy.deepcopy(d))
        d = _merge_preserve(d, ident)
    except Exception as e:
        d.setdefault("pipeline_diagnostics", {})["identity_v5110"] = {"status":"error","error":str(e)[:180]}

    # Financial recovery is already implemented in V5.10.3; retry it once only
    # when the final payload is actually empty, then preserve all good fields.
    if not _ok(d.get("financial")):
        try:
            retry = await run_v5103.build_stock_v5103(ticker, force_refresh=True)
            d = _merge_preserve(d, retry)
        except Exception as e:
            d.setdefault("pipeline_diagnostics", {})["financial_retry_v5110"] = {"status":"error","error":str(e)[:180]}

    if not _ok((d.get("cashflow") or {}).get("institutional")):
        try:
            repaired = await run_v5104._repair_institutional_v5104(ticker, copy.deepcopy(d))
            d = _merge_preserve(d, repaired)
        except Exception as e:
            d.setdefault("pipeline_diagnostics", {})["institutional_v5110"] = {"status":"error","error":str(e)[:180]}

    d = _source_status(d)
    try:
        d["scores"] = server.scores(d.get("technical") or {}, d.get("revenue") or {}, d.get("flow") or {}, d.get("per") or {}, d.get("financial") or {}, d.get("research") or {})
    except Exception as e:
        d.setdefault("pipeline_diagnostics", {})["score_v5110"] = {"status":"error","error":str(e)[:160]}
    try: d = run_v594._reconfidence(d)
    except Exception: pass
    d["version"] = VERSION
    d.setdefault("pipeline_diagnostics", {})["pipeline_v5110"] = {"status":"ok","policy":"independent-source-preserve-success"}
    return d


# V5.11 owns all public entry routes. Legacy handlers remain importable only as
# internal providers; they can no longer win FastAPI route order.
for r in list(app.routes):
    if getattr(r, "path", None) in ("/", "/health", "/api/stock/{ticker}"):
        try: app.routes.remove(r)
        except ValueError: pass


@app.get("/api/stock/{ticker}")
async def api_stock_v5110(ticker: str, refresh: int = 0):
    d = await build_stock_v5110(ticker, bool(refresh))
    return JSONResponse(d, headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0","X-AI-Stock-Version":VERSION})

@app.get("/api/v5110/stock/{ticker}")
async def api_v5110(ticker: str, refresh: int = 0):
    return await api_stock_v5110(ticker, refresh)

@app.get("/health")
async def health_v5110():
    return JSONResponse({"status":"ok","version":VERSION,"mode":"unified-independent-data-pipeline"}, headers={"Cache-Control":"no-store","X-AI-Stock-Version":VERSION})

@app.get("/", response_class=HTMLResponse)
async def root_v5110():
    text = (server.ROOT / "index.html").read_text(encoding="utf-8")
    # Strip every legacy hotfix/runtime script. V5.11 uses app.js only.
    text = re.sub(r'\s*<script[^>]+src="(?:recovery|v\d+_hotfix)\.js[^\"]*"[^>]*></script>', '', text, flags=re.I)
    text = re.sub(r'\s*<script[^>]+src="app\.js[^\"]*"[^>]*></script>', '', text, flags=re.I)
    text = re.sub(r'\s*<script>if\(\'serviceWorker\'.*?</script>', '', text, flags=re.S|re.I)
    text = re.sub(r'<span class="status-sep">•</span>\s*<span[^>]*data-app-version[^>]*>.*?</span>', '', text, flags=re.S|re.I)
    text = re.sub(r'<span[^>]*data-app-version[^>]*>.*?</span>\s*<span class="status-sep">•</span>', '', text, flags=re.S|re.I)
    text = re.sub(r'href="(?:/static/)?styles\.css(?:\?v=[^"]+)?"', f'href="/static/styles.css?v={VERSION}"', text)
    text = re.sub(r'href="/static/manifest\.webmanifest(?:\?v=[^"]+)?"', f'href="/static/manifest.webmanifest?v={VERSION}"', text)
    scripts = f'\n<script>window.AI_STOCK_VERSION="{VERSION}";</script>\n<script src="/static/app.js?v={VERSION}"></script>\n<script>if("serviceWorker" in navigator){{navigator.serviceWorker.register("/sw.js?v={VERSION}",{{updateViaCache:"none"}}).then(r=>r.update()).catch(()=>{{}});}}</script>\n'
    text = text.replace("</body>", scripts + "</body>")
    return HTMLResponse(text, headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0","Pragma":"no-cache","Expires":"0","X-AI-Stock-Version":VERSION})

@app.middleware("http")
async def runtime_v5110(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp
