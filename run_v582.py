"""V5.8.2 Module Recovery.
Build on V5.8.1 official fast core and restore the remaining missing modules:
financial/EPS, institutional positioning, margin/short data, and confidence/evidence.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from fastapi import Request
from fastapi.responses import JSONResponse

import run_v581
import run_v566
import run_v551
import server

VERSION = "5.8.2"
server.app.version = VERSION


def _merge_cashflow_into_flow(d: dict[str, Any]) -> dict[str, Any]:
    cf = d.get("cashflow") if isinstance(d.get("cashflow"), dict) else {}
    flow = d.get("flow") if isinstance(d.get("flow"), dict) else {}
    for k, v in cf.items():
        if v not in (None, "", [], {}):
            flow[k] = v
    d["cashflow"] = cf
    d["flow"] = flow
    return d


async def _repair_financial(ticker: str, d: dict[str, Any]) -> dict[str, Any]:
    if d.get("financial") or d.get("eps_stack"):
        return d
    try:
        official = await asyncio.wait_for(server.fetch_official_income_statement(ticker), timeout=24.0)
    except Exception:
        return d
    if not isinstance(official, dict) or not official.get("official"):
        return d
    try:
        official = await asyncio.wait_for(server.reconcile_official_financial_snapshot(ticker, official), timeout=8.0)
    except Exception:
        pass
    try:
        eps = await asyncio.wait_for(server.build_eps_stack(ticker, [], official, {}), timeout=10.0)
    except Exception:
        eps = {}
    try:
        integrity = server.assess_financial_integrity(official, eps, datetime.now().date())
    except Exception:
        integrity = {}
    fin = {
        "statement_date": official.get("statement_date") or official.get("period"),
        "period": official.get("period"),
        "source": official.get("source"),
        "official": True,
        "ytd_eps": official.get("ytd_eps"),
        "quarter_eps": eps.get("quarter_eps"),
        "ttm_eps": eps.get("ttm_eps"),
        "revenue": official.get("revenue_ytd"),
        "gross_profit": official.get("gross_profit_ytd"),
        "operating_income": official.get("operating_income_ytd"),
        "net_income": official.get("net_income_ytd"),
    }
    if fin.get("revenue"):
        if fin.get("gross_profit") is not None:
            fin["gross_margin"] = fin["gross_profit"] / fin["revenue"] * 100
        if fin.get("operating_income") is not None:
            fin["operating_margin"] = fin["operating_income"] / fin["revenue"] * 100
        if fin.get("net_income") is not None:
            fin["net_margin"] = fin["net_income"] / fin["revenue"] * 100
    d["official_financial"] = official
    d["eps_stack"] = eps
    d["financial_integrity"] = integrity
    d["financial"] = fin
    try:
        d["valuation"] = server.model_valuation(d.get("price"), d.get("per") or {}, eps, d.get("research") or {}, integrity)
    except Exception:
        pass
    return d


def _evidence_confidence(d: dict[str, Any]) -> dict[str, Any]:
    try:
        ev = server.build_evidence_graph(
            d.get("ticker"), d.get("technical") or {}, d.get("revenue") or {}, d.get("flow") or {},
            d.get("per") or {}, d.get("financial") or {}, d.get("eps_stack") or {},
            d.get("research") or {}, d.get("company_events") or {}, d.get("financial_integrity") or {}, {}
        )
        d["evidence"] = ev
        score = ((ev.get("summary") or {}).get("evidence_score"))
        if score is not None:
            d["confidence"] = score
    except Exception:
        pass
    return d


async def build_stock_v582(ticker: str, force_refresh: bool = False):
    d = await run_v581.build_stock_v581(ticker, force_refresh=force_refresh)

    # Merge previously completed T86 / MI_MARGN background cache BEFORE scoring.
    d = run_v566._merge_official_cache(d, run_v566._official_cache.get(ticker))
    d = _merge_cashflow_into_flow(d)
    if ticker not in run_v566._official_cache:
        d["official_cache_status"] = "warming"
    run_v566._schedule_official(ticker, d)

    # Financial source gets a second, independent repair budget. It cannot erase other modules.
    try:
        d = await _repair_financial(ticker, d)
    except Exception:
        pass

    # Recompute score with restored flow/financial data.
    try:
        d["scores"] = server.scores(
            d.get("technical") or {}, d.get("revenue") or {}, d.get("flow") or {},
            d.get("per") or {}, d.get("financial") or {}, d.get("research") or {}
        )
    except Exception:
        pass

    d = _evidence_confidence(d)

    # Update source rows with real module state.
    for row in d.get("source_status") or []:
        if row.get("name") == "財務報表":
            row["as_of"] = (d.get("financial") or {}).get("period") or (d.get("financial") or {}).get("statement_date")
            row["status"] = "ok" if d.get("financial") else "updating"
        elif row.get("name") == "三大法人":
            cf = d.get("cashflow") or {}
            row["as_of"] = cf.get("last_date")
            row["status"] = "ok" if cf.get("last_date") or any(cf.get(k) is not None for k in ("foreign_1","foreign_5","foreign_20")) else "updating"
        elif row.get("name") == "融資融券":
            cf = d.get("cashflow") or {}
            row["as_of"] = cf.get("margin_last_date")
            row["status"] = "ok" if cf.get("margin_last_date") or cf.get("margin_balance") is not None else "updating"

    diag = d.get("pipeline_diagnostics") if isinstance(d.get("pipeline_diagnostics"), dict) else {}
    diag.update({
        "mode": "module_recovery",
        "financial_recovered": bool(d.get("financial")),
        "eps_recovered": bool(d.get("eps_stack")),
        "official_positioning_cache": d.get("official_cache_status", "warming"),
        "flow_fields": len(d.get("flow") or {}),
        "confidence": d.get("confidence"),
    })
    d["pipeline_diagnostics"] = diag
    d["version"] = VERSION
    d["data_policy"] = (d.get("data_policy") or "") + " V5.8.2：補回官方財報/EPS、T86 三大法人與 MI_MARGN 融資融券背景快取，並重建 Evidence confidence。"
    return d

server.build_stock = build_stock_v582

_oldidx = run_v551._patched_index
def _idx():
    text = _oldidx()
    for v in ("5.8.1","5.8.0","5.7.4","5.7.3","5.7.2","5.7.1","5.7.0","5.6.6","5.6.5","5.6.4","5.6.3","5.6.2","5.6.1","5.6.0","5.5.9","5.5.1"):
        text = text.replace(v, VERSION)
    return text
run_v551._patched_index = _idx

_oldsw = run_v551._patched_sw
def _sw():
    text = _oldsw()
    for v in ("5.8.1","5.8.0","5.7.4","5.7.3","5.7.2","5.7.1","5.7.0","5.6.6","5.6.5","5.6.4","5.6.3","5.6.2","5.6.1","5.6.0","5.5.9","5.5.1"):
        text = text.replace(f"ai-stock-v{v}", f"ai-stock-v{VERSION}")
    return text
run_v551._patched_sw = _sw

@server.app.middleware("http")
async def runtime(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({
            "status": "ok", "version": VERSION, "mode": "module-recovery",
            "core": "TWSE/MOPS official fast core",
            "recover": ["financial_eps", "institutional", "margin_short", "confidence"],
        }, headers={"Cache-Control": "no-store"})
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp

app = server.app
