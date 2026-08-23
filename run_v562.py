"""V5.6.2 Price Recovery.
Adds an independent FinMind daily-price rescue before returning a report.
V5.6.1 remains the baseline; missing/invalid current price is repaired from
TaiwanStockPrice and technical series without discarding other usable research data.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

import run_v561
import run_v559
import run_v551
import server

VERSION = "5.6.2"
server.app.version = VERSION
_base = run_v561.build_stock_v561


def _num(v: Any):
    try:
        if v is None:
            return None
        s = str(v).replace(",", "").strip()
        if not s or s in ("--", "-", "None", "null"):
            return None
        x = float(s)
        return x if x > 0 else None
    except Exception:
        return None


def _price_from_report(d: dict[str, Any]):
    # Accept common top-level/snapshot quote fields first.
    containers = [d, d.get("snapshot") or {}, d.get("quote") or {}, d.get("price") or {}]
    keys = ("price", "close", "latest_price", "current_price", "last", "last_price")
    for obj in containers:
        if isinstance(obj, dict):
            for k in keys:
                p = _num(obj.get(k))
                if p is not None:
                    return p, obj.get("date") or obj.get("as_of"), "existing"
    tech = (d.get("technical") or {}).get("series") or []
    for row in reversed(tech):
        if isinstance(row, dict):
            p = _num(row.get("close"))
            if p is not None:
                return p, row.get("date"), "technical_series"
    return None, None, None


def _apply_price(d: dict[str, Any], price: float, as_of: Any, source: str):
    # Populate aliases used by different generations of the UI/report pipeline.
    d["price"] = price
    d["current_price"] = price
    d["latest_price"] = price
    quote = d.get("quote") if isinstance(d.get("quote"), dict) else {}
    quote["price"] = price
    quote["close"] = price
    if as_of:
        quote["date"] = str(as_of)[:10]
    quote["source"] = source
    d["quote"] = quote
    snap = d.get("snapshot") if isinstance(d.get("snapshot"), dict) else {}
    snap["price"] = price
    snap["close"] = price
    if as_of:
        snap["date"] = str(as_of)[:10]
    snap["price_source"] = source
    d["snapshot"] = snap
    d["price_source"] = source
    d["price_as_of"] = str(as_of)[:10] if as_of else None
    return d


async def _recover_price(ticker: str, d: dict[str, Any]):
    p, ds, src = _price_from_report(d)
    if p is not None:
        return _apply_price(d, p, ds, src or "existing")

    start = (date.today() - timedelta(days=45)).isoformat()
    rows = await run_v559.rows("TaiwanStockPrice", ticker, start, min_rows=1)
    candidates = []
    for x in rows:
        if not isinstance(x, dict):
            continue
        px = _num(x.get("close"))
        ds = str(x.get("date") or "")[:10]
        if px is not None and ds:
            candidates.append((ds, px))
    if candidates:
        ds, p = sorted(candidates, key=lambda z: z[0])[-1]
        return _apply_price(d, p, ds, "FinMind TaiwanStockPrice V4/V3 recovery")
    return d


async def build_stock_v562(ticker: str, force_refresh: bool = False):
    d = await _base(ticker, force_refresh=force_refresh)
    if isinstance(d, dict):
        try:
            d = await _recover_price(ticker, d)
        except Exception as e:
            d["price_recovery_error"] = type(e).__name__
        p, ds, src = _price_from_report(d)
        diag = d.get("pipeline_diagnostics") if isinstance(d.get("pipeline_diagnostics"), dict) else {}
        diag.update({
            "price_valid": p is not None,
            "price": p,
            "price_as_of": d.get("price_as_of") or ds,
            "price_source": d.get("price_source") or src,
        })
        d["pipeline_diagnostics"] = diag
        d["version"] = VERSION
        d["data_policy"] = (d.get("data_policy") or "") + " V5.6.2：新增獨立股價救援；既有報價無效時以 FinMind TaiwanStockPrice 歷史最新有效收盤價補回，並保留價格日期與來源診斷。"
    return d


server.build_stock = build_stock_v562

_oldidx = run_v551._patched_index
def _idx():
    return _oldidx().replace("5.6.1", VERSION).replace("5.6.0", VERSION).replace("5.5.9", VERSION).replace("5.5.1", VERSION)
run_v551._patched_index = _idx

_oldsw = run_v551._patched_sw
def _sw():
    return _oldsw().replace("ai-stock-v5.6.1", "ai-stock-v5.6.2").replace("ai-stock-v5.6.0", "ai-stock-v5.6.2").replace("ai-stock-v5.5.9", "ai-stock-v5.6.2").replace("ai-stock-v5.5.1", "ai-stock-v5.6.2")
run_v551._patched_sw = _sw


@server.app.middleware("http")
async def runtime(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({
            "status": "ok",
            "version": VERSION,
            "mode": "price-recovery",
            "price_recovery": "existing -> technical -> FinMind TaiwanStockPrice",
            "official_history": "background cache",
            "pwa": True,
        }, headers={"Cache-Control": "no-store"})
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp

app = server.app
