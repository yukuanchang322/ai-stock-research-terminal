"""V5.6.1 Non-blocking Official Pipeline.

Fixes V5.6.0 request-time 502s by moving multi-day TWSE T86 / MI_MARGN
history enrichment out of the foreground report request. The report returns from the
V5.5.9 stable pipeline immediately; official enrichment warms an in-process cache
and is applied on later refreshes. Failed/partial enrichment never overwrites usable data.
"""
from __future__ import annotations

import asyncio
import copy
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

import run_v559
import run_v560
import run_v551
import server

VERSION = "5.6.1"
server.app.version = VERSION
_base = run_v559.build_stock_v559

_cache: dict[str, dict[str, Any]] = {}
_tasks: dict[str, asyncio.Task] = {}


def _merge_cached(d: dict[str, Any], cached: dict[str, Any] | None) -> dict[str, Any]:
    if not cached:
        return d
    ccf = cached.get("cashflow") or {}
    if ccf:
        cf = d.get("cashflow") or {}
        for k, v in ccf.items():
            if v not in (None, [], {}, ""):
                cf[k] = v
        d["cashflow"] = cf
    d["official_cache_status"] = cached.get("status", "ready")
    return d


async def _warm_official(ticker: str, snapshot: dict[str, Any]):
    try:
        work = copy.deepcopy(snapshot)
        diag: dict[str, Any] = {}
        try:
            work = await run_v560.repair_institutional_official(ticker, work)
        except Exception as e:
            diag["institutional_error"] = type(e).__name__
        try:
            work = await run_v560.repair_margin_official(ticker, work)
        except Exception as e:
            diag["margin_error"] = type(e).__name__
        cf = work.get("cashflow") or {}
        _cache[ticker] = {
            "status": "ready",
            "cashflow": cf,
            "diagnostics": {
                **diag,
                "institutional_rows": cf.get("institutional_rows"),
                "institutional_source": cf.get("institutional_source"),
                "margin_rows": cf.get("margin_short_rows"),
                "margin_source": cf.get("margin_short_source"),
            },
        }
    finally:
        _tasks.pop(ticker, None)


def _schedule_warm(ticker: str, d: dict[str, Any]):
    task = _tasks.get(ticker)
    if task and not task.done():
        return
    _tasks[ticker] = asyncio.create_task(_warm_official(ticker, d))


async def build_stock_v561(ticker: str, force_refresh: bool = False):
    d = await _base(ticker, force_refresh=force_refresh)
    if isinstance(d, dict):
        d = _merge_cached(d, _cache.get(ticker))
        if ticker not in _cache:
            d["official_cache_status"] = "warming"
        _schedule_warm(ticker, d)
        rev = d.get("revenue") or {}
        cf = d.get("cashflow") or {}
        cached = _cache.get(ticker) or {}
        d["pipeline_diagnostics"] = {
            "mode": "non_blocking_official",
            "official_cache_status": d.get("official_cache_status"),
            "revenue_months": len(rev.get("series") or []),
            "revenue_source": rev.get("series_source"),
            "institutional_rows": cf.get("institutional_rows"),
            "institutional_source": cf.get("institutional_source"),
            "margin_rows": cf.get("margin_short_rows"),
            "margin_source": cf.get("margin_short_source"),
            "official_background": cached.get("diagnostics"),
        }
        d["version"] = VERSION
        d["data_policy"] = (d.get("data_policy") or "") + " V5.6.1：TWSE 多日官方歷史改為背景暖快取，不阻塞研究報告；成功後刷新套用，失敗不得覆蓋可用資料。"
    return d


server.build_stock = build_stock_v561

_oldidx = run_v551._patched_index
def _idx():
    return _oldidx().replace("5.6.0", VERSION).replace("5.5.9", VERSION).replace("5.5.1", VERSION)
run_v551._patched_index = _idx

_oldsw = run_v551._patched_sw
def _sw():
    return _oldsw().replace("ai-stock-v5.6.0", "ai-stock-v5.6.1").replace("ai-stock-v5.5.9", "ai-stock-v5.6.1").replace("ai-stock-v5.5.1", "ai-stock-v5.6.1")
run_v551._patched_sw = _sw


@server.app.middleware("http")
async def runtime(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({
            "status": "ok",
            "version": VERSION,
            "mode": "non-blocking-official",
            "foreground": "V5.5.9 stable pipeline",
            "official_history": "background cache",
            "pwa": True,
        }, headers={"Cache-Control": "no-store"})
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp

app = server.app
