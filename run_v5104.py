"""V5.10.4 Shell + Identity + Institutional Recovery

- Owns the root route so inherited V5.9.x HTML/version badges cannot win route order.
- Repairs listed-company name from TWSE official company registry when name falls back to ticker.
- Repairs T86 institutional data, then rebuilds d.flow through run_v590.merge_cf.
- If latest T86 is unavailable, waits briefly for the existing official-history warmer and merges its cache.
- Preserves V5.10.3 financial recovery and all already-good price/revenue/PER/margin data.
"""
from __future__ import annotations

import asyncio
import re
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

import run_v5103, run_v5102, run_v595, run_v590, run_v594, server

VERSION = "5.10.4"
server.app.version = VERSION
app = server.app


def _bad_name(v, ticker: str) -> bool:
    s = str(v or "").strip()
    return (not s) or s == ticker or s.replace(" ", "") == ticker


async def _repair_identity(ticker: str, d: dict) -> dict:
    if not _bad_name(d.get("name"), ticker):
        return d
    diag = d.setdefault("pipeline_diagnostics", {})
    try:
        rows = await asyncio.wait_for(
            server.openapi_json(server.TWSE_OPENAPI, "/opendata/t187ap03_L"), timeout=12
        )
        row = next((r for r in rows if str(r.get("公司代號") or r.get("SecuritiesCompanyCode") or "").strip() == ticker), None)
        if row:
            name = (row.get("公司簡稱") or row.get("公司名稱") or row.get("CompanyName") or "").strip()
            if name:
                d["name"] = name
                d["company_name"] = name
                if not d.get("industry"):
                    d["industry"] = row.get("產業別") or row.get("產業類別") or d.get("industry")
                diag["identity_v5104"] = {"status": "ok", "source": "TWSE t187ap03_L", "name": name}
                return d
        diag["identity_v5104"] = {"status": "ticker_not_found", "rows": len(rows)}
    except Exception as e:
        diag["identity_v5104"] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:160]}"}
    return d


def _institutional_present(d: dict) -> bool:
    return bool(((d.get("cashflow") or {}).get("institutional")))


async def _repair_institutional_v5104(ticker: str, d: dict) -> dict:
    diag = d.setdefault("pipeline_diagnostics", {})

    # First reuse the semantic latest/daily parser from V5.9.5.
    if not _institutional_present(d):
        extra = None
        try:
            extra, meta = await asyncio.wait_for(run_v595._openapi_t86(ticker, d), timeout=12)
            diag["t86_openapi_v5104"] = meta
        except Exception as e:
            diag["t86_openapi_v5104"] = {"status":"error","error":f"{type(e).__name__}: {str(e)[:160]}"}
        if not extra:
            try:
                extra, meta = await asyncio.wait_for(run_v595._daily_t86(ticker, d), timeout=24)
                diag["t86_daily_v5104"] = meta
            except Exception as e:
                diag["t86_daily_v5104"] = {"status":"error","error":f"{type(e).__name__}: {str(e)[:160]}"}
        if extra:
            # Critical: merge_cf also rebuilds d.flow, which the UI and score engine consume.
            d = run_v590.merge_cf(d, extra)

    # If latest-only recovery still failed, synchronously give the existing official-history warmer
    # one bounded chance; it fetches T86/MI_MARGN concurrently and stores 1/5/20 aggregates.
    if not _institutional_present(d):
        try:
            await asyncio.wait_for(run_v590.warm_history(ticker, d), timeout=34)
            hist = run_v590._hist_cache.get(ticker)
            if hist:
                d = run_v590.merge_cf(d, hist)
                diag["t86_history_v5104"] = {"status":"merged", "institutional_rows": hist.get("institutional_rows"), "last_date": hist.get("last_date")}
            else:
                diag["t86_history_v5104"] = {"status":"empty"}
        except Exception as e:
            diag["t86_history_v5104"] = {"status":"error", "error":f"{type(e).__name__}: {str(e)[:160]}"}

    cf = d.get("cashflow") if isinstance(d.get("cashflow"), dict) else {}
    rows = d.get("source_status") if isinstance(d.get("source_status"), list) else []
    found = False
    for row in rows:
        if row.get("name") == "三大法人":
            found = True
            row.update({
                "status": "ok" if cf.get("institutional") else "missing",
                "as_of": cf.get("last_date"),
                "dataset": cf.get("institutional_source") or "TWSE T86 official",
            })
    if not found:
        rows.append({"name":"三大法人","status":"ok" if cf.get("institutional") else "missing","as_of":cf.get("last_date"),"dataset":cf.get("institutional_source") or "TWSE T86 official"})
    d["source_status"] = rows
    diag["t86_v5104"] = {
        "status": "ok" if cf.get("institutional") else "missing",
        "flow_keys": [k for k in (d.get("flow") or {}).keys() if k.startswith(("foreign_","trust_","dealer_"))],
    }
    return d


async def build_stock_v5104(ticker: str, force_refresh: bool = False):
    d = await run_v5103.build_stock_v5103(ticker, force_refresh=force_refresh)
    if not isinstance(d, dict):
        return d
    d = await _repair_identity(ticker, d)
    d = await _repair_institutional_v5104(ticker, d)
    try:
        d["scores"] = server.scores(
            d.get("technical") or {}, d.get("revenue") or {}, d.get("flow") or {},
            d.get("per") or {}, d.get("financial") or {}, d.get("research") or {}
        )
    except Exception as e:
        d.setdefault("pipeline_diagnostics", {})["score_v5104"] = f"{type(e).__name__}: {str(e)[:140]}"
    try:
        d = run_v594._reconfidence(d)
    except Exception:
        pass
    d["version"] = VERSION
    return d


# Own API + root + health. Removing every inherited copy is important because imports across
# V5.9.x/V5.10.x otherwise leave several '/' handlers in FastAPI route order.
for r in list(app.routes):
    if getattr(r, "path", None) in ("/api/stock/{ticker}", "/api/v5104/stock/{ticker}", "/", "/health"):
        try:
            app.routes.remove(r)
        except ValueError:
            pass


@app.get("/api/stock/{ticker}")
async def api_stock_v5104(ticker: str, refresh: int = 0):
    d = await build_stock_v5104(ticker.strip(), force_refresh=bool(refresh))
    return JSONResponse(d, headers={"Cache-Control":"no-store, max-age=0", "X-AI-Stock-Version":VERSION})


@app.get("/api/v5104/stock/{ticker}")
async def api_v5104(ticker: str, refresh: int = 0):
    return await api_stock_v5104(ticker, refresh)


@app.get("/api/v5104/diagnostics/{ticker}")
async def diagnostics_v5104(ticker: str, refresh: int = 0):
    d = await build_stock_v5104(ticker.strip(), force_refresh=bool(refresh))
    return JSONResponse({
        "version": VERSION,
        "ticker": ticker,
        "name": d.get("name"),
        "financial": next((x for x in d.get("source_status") or [] if x.get("name")=="財務報表"), None),
        "institutional": next((x for x in d.get("source_status") or [] if x.get("name")=="三大法人"), None),
        "flow": d.get("flow") or {},
        "pipeline_diagnostics": d.get("pipeline_diagnostics") or {},
    }, headers={"Cache-Control":"no-store, max-age=0"})


@app.get("/health")
async def health_v5104():
    return JSONResponse({
        "status":"ok", "version":VERSION, "mode":"shell-identity-t86-repair",
        "financial":"V5.10.3 preserved", "institutional":"TWSE T86 latest + bounded official history",
    }, headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0", "X-AI-Stock-Version":VERSION})


@app.get("/", response_class=HTMLResponse)
async def root_v5104():
    text = (server.ROOT / "index.html").read_text(encoding="utf-8")
    # Permanently suppress any legacy visible version badge. Runtime version is rendered by /health only.
    text = re.sub(r'<span class="status-sep">•</span>\s*<span[^>]*data-app-version[^>]*>.*?</span>', '', text, flags=re.S|re.I)
    text = re.sub(r'<span[^>]*data-app-version[^>]*>.*?</span>\s*<span class="status-sep">•</span>', '', text, flags=re.S|re.I)
    text = re.sub(r'AI Stock Research Terminal V5\.\d+(?:\.\d+)?', f'AI Stock Research Terminal V{VERSION}', text)
    text = re.sub(r'href="styles\.css(?:\?v=[^"]+)?"', f'href="/static/styles.css?v={VERSION}"', text)
    text = re.sub(r'href="/static/manifest\.webmanifest(?:\?v=[^"]+)?"', f'href="/static/manifest.webmanifest?v={VERSION}"', text)
    text = re.sub(r'\s*<script[^>]+src="(?:recovery|v547_hotfix|v5100_hotfix|v5101_hotfix|v5102_hotfix)\.js[^\"]*"[^>]*></script>', '', text, flags=re.I)
    text = re.sub(r'\s*<script[^>]+src="app\.js[^\"]*"[^>]*></script>', '', text, flags=re.I)
    text = re.sub(r'\s*<script>if\(\'serviceWorker\'.*?</script>', '', text, flags=re.S|re.I)
    scripts = (
        f'\n<script src="/static/app.js?v={VERSION}"></script>'
        f'\n<script src="/static/v5101_hotfix.js?v={VERSION}"></script>'
        f'\n<script>if(\'serviceWorker\' in navigator){{navigator.serviceWorker.register(\'/sw.js?v={VERSION}\',{{updateViaCache:\'none\'}}).then(r=>r.update()).catch(()=>{{}});}}</script>\n'
    )
    text = text.replace("</body>", scripts + "</body>")
    return HTMLResponse(text, headers={
        "Cache-Control":"no-store, no-cache, must-revalidate, max-age=0", "Pragma":"no-cache", "Expires":"0",
        "X-App-Version":VERSION, "X-AI-Stock-Version":VERSION,
    })


@app.middleware("http")
async def v5104_runtime_header(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp
