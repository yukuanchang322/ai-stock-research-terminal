"""V5.6.6 Core-First Research.

Remove V5.6.5's empty minimal-report timeout fallback. Build the investable core
(price, technicals, revenue, financials/EPS, valuation and base positioning) first,
while optional public-web research, company-event search and MCP cross-check are
made non-blocking for the foreground request. Official price bootstrap remains
highest priority. V5.6.1 background official T86/MI_MARGN enrichment is retained.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

import run_v564
import run_v559
import run_v560
import run_v551
import server

VERSION = "5.6.6"
server.app.version = VERSION

# Stable research core before V5.5.9's extra foreground FinMind repair calls.
_core_base = run_v559._base

_official_cache: dict[str, dict[str, Any]] = {}
_official_tasks: dict[str, asyncio.Task] = {}


async def _empty_public_research(ticker: str, company_name: str):
    return {"rows": [], "errors": ["deferred_v566"], "queries": [], "fetched_at": None}


async def _empty_company_events(ticker: str, company_name: str):
    return {"rows": [], "earnings_calls": [], "material_info": [], "company_updates": [],
            "errors": ["deferred_v566"], "queries": [], "fetched_at": None}


async def _empty_mcp(ticker: str):
    return {"provider": "TWStock MCP", "status": "deferred", "records": [], "errors": ["deferred_v566"]}


async def _build_core(ticker: str, force_refresh: bool = False):
    """Run the stable core while deferring optional slow providers.

    server.build_stock's provider functions are resolved from module globals at runtime,
    so replacing these three optional functions removes their network latency without
    changing the core FinMind/TWSE/MOPS financial pipeline.
    Render is configured with WEB_CONCURRENCY=1, avoiding cross-request mutation races.
    """
    old_pub = server.fetch_public_research
    old_evt = server.fetch_company_events
    old_mcp = server.fetch_twstock_mcp_snapshot
    server.fetch_public_research = _empty_public_research
    server.fetch_company_events = _empty_company_events
    server.fetch_twstock_mcp_snapshot = _empty_mcp
    try:
        return await _core_base(ticker, force_refresh=force_refresh)
    finally:
        server.fetch_public_research = old_pub
        server.fetch_company_events = old_evt
        server.fetch_twstock_mcp_snapshot = old_mcp


def _merge_official_cache(d: dict[str, Any], cached: dict[str, Any] | None):
    if not cached:
        return d
    cf = d.get("cashflow") if isinstance(d.get("cashflow"), dict) else {}
    for k, v in (cached.get("cashflow") or {}).items():
        if v not in (None, [], {}, ""):
            cf[k] = v
    d["cashflow"] = cf
    d["official_cache_status"] = cached.get("status", "ready")
    return d


async def _warm_official(ticker: str, snapshot: dict[str, Any]):
    try:
        work = dict(snapshot)
        try:
            work = await run_v560.repair_institutional_official(ticker, work)
        except Exception:
            pass
        try:
            work = await run_v560.repair_margin_official(ticker, work)
        except Exception:
            pass
        _official_cache[ticker] = {"status": "ready", "cashflow": work.get("cashflow") or {}}
    finally:
        _official_tasks.pop(ticker, None)


def _schedule_official(ticker: str, d: dict[str, Any]):
    task = _official_tasks.get(ticker)
    if task and not task.done():
        return
    _official_tasks[ticker] = asyncio.create_task(_warm_official(ticker, d))


async def _repair_revenue_bounded(ticker: str, d: dict[str, Any]):
    try:
        return await asyncio.wait_for(run_v559.repair_revenue(ticker, d), timeout=7.0)
    except Exception:
        return d


def _recompute_after_price(d: dict[str, Any]):
    """Price bootstrap can change the current price; refresh valuation aliases safely."""
    try:
        stack = d.get("eps_stack") or {}
        integrity = d.get("financial_integrity") or {}
        d["valuation"] = server.model_valuation(d.get("price"), d.get("per") or {}, stack,
                                                d.get("research") or {}, integrity)
    except Exception:
        pass
    return d


async def build_stock_v566(ticker: str, force_refresh: bool = False):
    bootstrap = None
    attempts = []
    try:
        bootstrap, attempts = await asyncio.wait_for(run_v564._bootstrap_price(ticker), timeout=8.0)
    except Exception as e:
        attempts = [{"source": "price_bootstrap", "ok": False,
                     "error": f"{type(e).__name__}: {str(e)[:160]}"}]

    # Core report gets a realistic budget, but no optional web/MCP calls are inside it.
    d = await asyncio.wait_for(_build_core(ticker, force_refresh=force_refresh), timeout=28.0)
    if not isinstance(d, dict):
        raise RuntimeError("core report payload invalid")

    # Bounded monthly-revenue history enrichment; never erase usable core revenue.
    d = await _repair_revenue_bounded(ticker, d)

    # Reuse any completed official institutional/margin background cache.
    d = _merge_official_cache(d, _official_cache.get(ticker))
    if ticker not in _official_cache:
        d["official_cache_status"] = "warming"
    _schedule_official(ticker, d)

    # Authoritative latest close wins over stale aliases.
    if bootstrap:
        ds, p, source = bootstrap
        d = run_v564._apply(d, p, ds, source)
        d = _recompute_after_price(d)

    # Never intentionally replace a complete core report by an all-zero shell.
    scores = d.get("scores") if isinstance(d.get("scores"), dict) else {}
    if not scores:
        try:
            scores = server.scores(d.get("technical") or {}, d.get("revenue") or {}, d.get("flow") or {},
                                   d.get("per") or {}, d.get("financial") or {}, d.get("research") or {})
            d["scores"] = scores
        except Exception:
            pass

    diag = d.get("pipeline_diagnostics") if isinstance(d.get("pipeline_diagnostics"), dict) else {}
    diag.update({
        "mode": "core_first",
        "price_bootstrap_attempts": attempts,
        "price_valid": d.get("price") is not None,
        "price_source": d.get("price_source"),
        "price_as_of": d.get("price_as_of"),
        "optional_public_research": "deferred",
        "optional_company_events": "deferred",
        "optional_mcp": "deferred",
        "official_positioning": d.get("official_cache_status", "warming"),
        "revenue_months": len((d.get("revenue") or {}).get("series") or []),
        "technical_days": len((d.get("technical") or {}).get("series") or []),
    })
    d["pipeline_diagnostics"] = diag
    d["version"] = VERSION
    d["data_policy"] = (d.get("data_policy") or "") + (
        " V5.6.6：取消空白 minimal report；價格/技術/營收/財報/EPS/估值為前景核心，"
        "公開研究、公司事件、MCP 改為非阻塞；T86/MI_MARGN 於背景補齊。"
    )
    return d


server.build_stock = build_stock_v566

_oldidx = run_v551._patched_index
def _idx():
    text = _oldidx()
    for v in ("5.6.5","5.6.4","5.6.3","5.6.2","5.6.1","5.6.0","5.5.9","5.5.1"):
        text = text.replace(v, VERSION)
    return text
run_v551._patched_index = _idx

_oldsw = run_v551._patched_sw
def _sw():
    text = _oldsw()
    for v in ("5.6.5","5.6.4","5.6.3","5.6.2","5.6.1","5.6.0","5.5.9","5.5.1"):
        text = text.replace(f"ai-stock-v{v}", f"ai-stock-v{VERSION}")
    return text
run_v551._patched_sw = _sw


@server.app.middleware("http")
async def runtime(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({
            "status": "ok", "version": VERSION, "mode": "core-first",
            "minimal_report_fallback": False,
            "core": ["price","technical","revenue","financial","eps","valuation"],
            "deferred": ["public_research","company_events","mcp","official_positioning_history"],
            "pwa": True,
        }, headers={"Cache-Control": "no-store"})
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp

app = server.app
