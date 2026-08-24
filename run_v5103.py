"""V5.10.3 Data Source Repair

Goals:
- keep V5.10.2 shell/cache behavior,
- preserve working price/revenue/PER-PBR data,
- independently repair TWSE T86 institutional flow,
- independently repair TWSE/MOPS financial + EPS payloads,
- recompute score/confidence only after targeted repairs,
- expose diagnostics without letting one provider failure break the report.
"""
from __future__ import annotations

import asyncio
from fastapi import Request
from fastapi.responses import JSONResponse

import run_v5102, run_v5100, run_v595, run_v596, run_v582, run_v594, run_v590, server

VERSION = "5.10.3"
server.app.version = VERSION
app = server.app


def _has_financial(d: dict) -> bool:
    return bool(
        (isinstance(d.get("financial"), dict) and d.get("financial")) or
        (isinstance(d.get("official_financial"), dict) and d.get("official_financial")) or
        (isinstance(d.get("eps_stack"), dict) and d.get("eps_stack"))
    )


def _has_institutional(d: dict) -> bool:
    cf = d.get("cashflow") if isinstance(d.get("cashflow"), dict) else {}
    return bool(cf.get("institutional"))


def _sync_institutional_status(d: dict):
    cf = d.get("cashflow") if isinstance(d.get("cashflow"), dict) else {}
    rows = d.get("source_status") if isinstance(d.get("source_status"), list) else []
    found = False
    for row in rows:
        if row.get("name") == "三大法人":
            found = True
            row.update({
                "status": "ok" if cf.get("institutional") else "missing",
                "as_of": cf.get("last_date"),
                "dataset": cf.get("institutional_source") or "TWSE T86",
            })
    if not found:
        rows.append({
            "name": "三大法人",
            "dataset": cf.get("institutional_source") or "TWSE T86",
            "as_of": cf.get("last_date"),
            "status": "ok" if cf.get("institutional") else "missing",
        })
    d["source_status"] = rows
    return d


async def _repair_institutional(ticker: str, d: dict):
    diag = d.setdefault("pipeline_diagnostics", {})
    if _has_institutional(d):
        diag["t86_v5103"] = {"status": "already_available"}
        return d

    extra = None
    try:
        extra, meta = await asyncio.wait_for(run_v595._openapi_t86(ticker, d), timeout=12)
        diag["t86_openapi_v5103"] = meta
    except Exception as e:
        diag["t86_openapi_v5103"] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:180]}"}

    if not extra:
        try:
            extra, meta = await asyncio.wait_for(run_v595._daily_t86(ticker, d), timeout=26)
            diag["t86_daily_v5103"] = meta
        except Exception as e:
            diag["t86_daily_v5103"] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:180]}"}

    if extra:
        try:
            d = run_v590.merge_cf(d, extra)
            diag["t86_v5103"] = {"status": "recovered"}
        except Exception as e:
            diag["t86_v5103"] = {"status": "merge_error", "error": f"{type(e).__name__}: {str(e)[:180]}"}
    else:
        diag["t86_v5103"] = {"status": "missing_after_repair"}

    return _sync_institutional_status(d)


async def _repair_financial(ticker: str, d: dict):
    diag = d.setdefault("pipeline_diagnostics", {})
    if _has_financial(d):
        diag["financial_v5103"] = {"status": "already_available", "as_of": run_v596._financial_asof(d)}
        return run_v596._sync_financial_status(d)

    try:
        repaired = await asyncio.wait_for(run_v582._repair_financial(ticker, d), timeout=38)
        if isinstance(repaired, dict):
            d = repaired
        diag["financial_repair_v5103"] = {
            "status": "recovered" if _has_financial(d) else "empty",
            "as_of": run_v596._financial_asof(d),
        }
    except Exception as e:
        diag["financial_repair_v5103"] = {
            "status": "error",
            "error": f"{type(e).__name__}: {str(e)[:220]}",
        }

    d = run_v596._sync_financial_status(d)
    diag["financial_v5103"] = {
        "status": "ok" if _has_financial(d) else "missing",
        "as_of": run_v596._financial_asof(d),
    }
    return d


async def build_stock_v5103(ticker: str, force_refresh: bool = False):
    # Start from the stabilized V5.10.0 data aggregation so working sources remain untouched.
    d = await run_v5100.build_stock_v5100(ticker, force_refresh=force_refresh)
    if not isinstance(d, dict):
        return d

    # Targeted repairs are isolated. A failure in either source cannot erase the rest of the report.
    d = await _repair_institutional(ticker, d)
    d = await _repair_financial(ticker, d)

    # Recompute only after the repair pass.
    try:
        d["scores"] = server.scores(
            d.get("technical") or {}, d.get("revenue") or {}, d.get("flow") or {},
            d.get("per") or {}, d.get("financial") or {}, d.get("research") or {}
        )
    except Exception as e:
        d.setdefault("pipeline_diagnostics", {})["score_recompute_v5103"] = f"{type(e).__name__}: {str(e)[:160]}"

    try:
        d = run_v594._reconfidence(d)
    except Exception as e:
        d.setdefault("pipeline_diagnostics", {})["confidence_recompute_v5103"] = f"{type(e).__name__}: {str(e)[:160]}"

    d["version"] = VERSION
    d.setdefault("pipeline_diagnostics", {})["v5103_summary"] = {
        "financial": "ok" if _has_financial(d) else "missing",
        "institutional": "ok" if _has_institutional(d) else "missing",
        "revenue_preserved": bool((d.get("revenue") or {}).get("rows") or (d.get("revenue") or {}).get("latest_revenue")),
        "price_preserved": d.get("price") is not None,
        "per_preserved": bool(d.get("per")),
    }
    return d


# Own API routes while preserving the V5.10.2 shell/service-worker/cache-break behavior.
for r in list(app.routes):
    if getattr(r, "path", None) in ("/api/stock/{ticker}", "/api/v5103/stock/{ticker}"):
        try:
            app.routes.remove(r)
        except ValueError:
            pass


@app.get("/api/stock/{ticker}")
async def api_stock_v5103(ticker: str, refresh: int = 0):
    d = await build_stock_v5103(ticker.strip(), force_refresh=bool(refresh))
    return JSONResponse(d, headers={"Cache-Control": "no-store, max-age=0", "X-AI-Stock-Version": VERSION})


@app.get("/api/v5103/stock/{ticker}")
async def api_v5103(ticker: str, refresh: int = 0):
    return await api_stock_v5103(ticker, refresh)


@app.get("/api/v5103/diagnostics/{ticker}")
async def diagnostics_v5103(ticker: str, refresh: int = 0):
    d = await build_stock_v5103(ticker.strip(), force_refresh=bool(refresh))
    return JSONResponse({
        "version": VERSION,
        "ticker": ticker,
        "financial_available": _has_financial(d),
        "institutional_available": _has_institutional(d),
        "financial_asof": run_v596._financial_asof(d),
        "source_status": d.get("source_status") or [],
        "pipeline_diagnostics": d.get("pipeline_diagnostics") or {},
    }, headers={"Cache-Control": "no-store, max-age=0"})


@app.middleware("http")
async def v5103_runtime_header(request: Request, call_next):
    # Override health response so the shell always sees the actual runtime version.
    if request.url.path == "/health":
        return JSONResponse({
            "status": "ok",
            "version": VERSION,
            "mode": "targeted-data-source-repair",
            "financial": "TWSE/MOPS official financial + EPS repair",
            "institutional": "TWSE T86 OpenAPI + nearest official daily fallback",
            "shell": "V5.10.2 cache-break behavior preserved",
        }, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "X-AI-Stock-Version": VERSION})
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp
