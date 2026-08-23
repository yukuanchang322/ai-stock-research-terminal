"""V5.8.1 Official Fast Core.
Build the visible research core directly from official TWSE/MOPS sources so an
unavailable/slow FinMind endpoint cannot hold the entire report for 25-45 seconds.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from fastapi import Request
from fastapi.responses import JSONResponse

import run_v546
import run_v548
import run_v564
import run_v566
import run_v572
import run_v551
import server

VERSION = "5.8.1"
server.app.version = VERSION

async def _bounded(coro, seconds: float, fallback):
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except Exception:
        return fallback


def _score_safe(tech, revenue, flow, perdata, financial):
    try:
        return server.scores(tech or {}, revenue or {}, flow or {}, perdata or {}, financial or {}, {})
    except Exception:
        return {"綜合": None, "基本面": None, "籌碼面": None, "技術面": None, "估值": None}


async def build_stock_v581(ticker: str, force_refresh: bool = False):
    # Tier-1 official sources are launched together. No FinMind call is on this critical path.
    info_task = _bounded(run_v546._official_company_info(), 8.0, [])
    price_task = _bounded(server.fetch_twse_stock_day_history(ticker, 13), 12.0, [])
    revenue_task = _bounded(run_v546._official_revenue(ticker), 8.0, [])
    per_task = _bounded(run_v548._official_per(ticker), 8.0, [])
    fin_task = _bounded(server.fetch_official_income_statement(ticker), 14.0, {"official": False, "errors": ["official_financial_timeout"]})

    infos, prices, rev_rows, per_rows, official_financial = await asyncio.gather(
        info_task, price_task, revenue_task, per_task, fin_task
    )

    info = next((x for x in infos if str(x.get("stock_id")) == ticker), {}) if isinstance(infos, list) else {}
    tech = server.calc_technical(prices or {}) if prices else {}
    revenue = server.calc_revenue(rev_rows or []) if rev_rows else {}
    perdata = server.calc_per(per_rows or []) if per_rows else {}
    financial: dict[str, Any] = {}
    eps_stack: dict[str, Any] = {}
    integrity: dict[str, Any] = {}

    if isinstance(official_financial, dict) and official_financial.get("official"):
        try:
            official_financial = await _bounded(
                server.reconcile_official_financial_snapshot(ticker, official_financial), 5.0, official_financial
            )
        except Exception:
            pass
        try:
            eps_stack = await _bounded(server.build_eps_stack(ticker, [], official_financial, {}), 8.0, {})
        except Exception:
            eps_stack = {}
        try:
            integrity = server.assess_financial_integrity(official_financial, eps_stack, datetime.now().date())
        except Exception:
            integrity = {}
        financial = {
            "statement_date": official_financial.get("statement_date") or official_financial.get("period"),
            "period": official_financial.get("period"),
            "source": official_financial.get("source"),
            "official": True,
            "ytd_eps": official_financial.get("ytd_eps"),
            "quarter_eps": eps_stack.get("quarter_eps"),
            "ttm_eps": eps_stack.get("ttm_eps"),
            "revenue": official_financial.get("revenue_ytd"),
            "gross_profit": official_financial.get("gross_profit_ytd"),
            "operating_income": official_financial.get("operating_income_ytd"),
            "net_income": official_financial.get("net_income_ytd"),
        }
        if financial.get("revenue"):
            if financial.get("gross_profit") is not None:
                financial["gross_margin"] = financial["gross_profit"] / financial["revenue"] * 100
            if financial.get("operating_income") is not None:
                financial["operating_margin"] = financial["operating_income"] / financial["revenue"] * 100
            if financial.get("net_income") is not None:
                financial["net_margin"] = financial["net_income"] / financial["revenue"] * 100

    flow = {}
    scores = _score_safe(tech, revenue, flow, perdata, financial)
    price = tech.get("last") if isinstance(tech, dict) else None

    # Independent latest official bootstrap remains final authority for headline price.
    bootstrap = None
    attempts = []
    try:
        bootstrap, attempts = await _bounded(run_v564._bootstrap_price(ticker), 6.0, (None, []))
    except Exception:
        bootstrap = None
    if bootstrap:
        ds, p, source = bootstrap
        price = p
        if not tech:
            tech = {"last": p, "last_date": ds, "series": [], "ma": {}, "trend": "資料不足"}
        else:
            tech["last"] = p
            tech["last_date"] = ds
    else:
        ds = tech.get("last_date") if isinstance(tech, dict) else None
        source = "TWSE STOCK_DAY official history" if price else None

    valuation = {}
    try:
        valuation = server.model_valuation(price, perdata or {}, eps_stack or {}, {}, integrity or {}) if price else {}
    except Exception:
        valuation = {}

    d = {
        "ticker": ticker,
        "name": info.get("stock_name") or ticker,
        "industry": info.get("industry_category") or "—",
        "price": price,
        "current_price": price,
        "latest_price": price,
        "price_source": source,
        "price_as_of": ds,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "version": VERSION,
        "technical": tech or {"series": []},
        "revenue": revenue or {"series": []},
        "flow": flow,
        "cashflow": {},
        "per": perdata or {},
        "financial": financial,
        "official_financial": official_financial if isinstance(official_financial, dict) else {},
        "financial_integrity": integrity,
        "eps_stack": eps_stack,
        "research": {"reports": [], "count": 0},
        "company_events": {"rows": [], "earnings_calls": [], "material_info": []},
        "valuation": valuation if isinstance(valuation, dict) else {},
        "scores": scores,
        "source_status": [
            {"name": "股價", "dataset": source or "TWSE", "as_of": ds, "status": "ok" if price else "missing", "scheduled_update": "交易日收盤後"},
            {"name": "月營收", "dataset": "TWSE OpenAPI t187ap05_L", "as_of": revenue.get("last_date") or revenue.get("revenue_period"), "status": "ok" if revenue else "missing", "scheduled_update": "依公司公告"},
            {"name": "PER/PBR", "dataset": "TWSE BWIBBU", "as_of": perdata.get("last_date"), "status": "ok" if perdata else "missing", "scheduled_update": "交易日收盤後"},
            {"name": "財務報表", "dataset": financial.get("source") or "TWSE/MOPS", "as_of": financial.get("period") or financial.get("statement_date"), "status": "ok" if financial else "updating", "scheduled_update": "依季報公告"},
            {"name": "三大法人", "dataset": "TWSE T86 background", "as_of": None, "status": "updating", "scheduled_update": "背景補齊"},
            {"name": "融資融券", "dataset": "TWSE MI_MARGN background", "as_of": None, "status": "updating", "scheduled_update": "背景補齊"},
        ],
        "pipeline_state": "official_core_ready",
        "report_partial": not bool(price and (tech or revenue or financial)),
        "pipeline_diagnostics": {
            "mode": "official_fast_core",
            "finmind_on_critical_path": False,
            "company_info": bool(info),
            "price_history_rows": len(prices or []),
            "revenue_rows": len(rev_rows or []),
            "per_rows": len(per_rows or []),
            "financial_official": bool(financial),
            "price_bootstrap_attempts": attempts,
        },
        "data_policy": "V5.8.1：可視核心改為 TWSE/MOPS 官方來源直接並行取得；FinMind 不再位於首屏研究的阻塞路徑。",
    }

    # Background positioning history; a later refresh can merge it through existing official cache.
    try:
        run_v566._schedule_official(ticker, d)
    except Exception:
        pass

    return run_v572._compat(d)

server.build_stock = build_stock_v581

_oldidx = run_v551._patched_index
def _idx():
    text = _oldidx()
    for v in ("5.8.0","5.7.4","5.7.3","5.7.2","5.7.1","5.7.0","5.6.6","5.6.5","5.6.4","5.6.3","5.6.2","5.6.1","5.6.0","5.5.9","5.5.1"):
        text = text.replace(v, VERSION)
    return text
run_v551._patched_index = _idx

_oldsw = run_v551._patched_sw
def _sw():
    text = _oldsw()
    for v in ("5.8.0","5.7.4","5.7.3","5.7.2","5.7.1","5.7.0","5.6.6","5.6.5","5.6.4","5.6.3","5.6.2","5.6.1","5.6.0","5.5.9","5.5.1"):
        text = text.replace(f"ai-stock-v{v}", f"ai-stock-v{VERSION}")
    return text
run_v551._patched_sw = _sw

@server.app.middleware("http")
async def runtime(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({"status": "ok", "version": VERSION, "mode": "official-fast-core", "finmind_critical_path": False}, headers={"Cache-Control": "no-store"})
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp

app = server.app
