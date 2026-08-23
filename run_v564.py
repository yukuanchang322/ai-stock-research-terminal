"""V5.6.4 Direct TWSE Price Bootstrap.
Use TWSE OpenAPI whole-market latest snapshot as the primary listed-stock price source.
Fall back to RWD whole-market snapshot, then V5.6.3 monthly/FinMind recovery.
Keep provider diagnostics instead of silently swallowing every price failure.
"""
from __future__ import annotations

from typing import Any
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

import run_v563
import run_v551
import server

VERSION = "5.6.4"
server.app.version = VERSION
_base = run_v563.build_stock_v563


def _num(v: Any):
    try:
        if v is None:
            return None
        s = str(v).replace(",", "").strip()
        if not s or s in ("--", "-", "None", "null", "X", "—"):
            return None
        x = float(s)
        return x if x > 0 else None
    except Exception:
        return None


def _roc_to_iso(raw: Any):
    s = "".join(ch for ch in str(raw or "") if ch.isdigit())
    try:
        if len(s) == 7:
            return f"{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}"
        if len(s) == 8:
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    except Exception:
        pass
    return str(raw or "")


def _apply(d: dict[str, Any], price: float, as_of: Any, source: str):
    d["price"] = price
    d["current_price"] = price
    d["latest_price"] = price
    d["price_source"] = source
    d["price_as_of"] = str(as_of)[:10] if as_of else None
    q = d.get("quote") if isinstance(d.get("quote"), dict) else {}
    q.update({"price": price, "close": price, "source": source})
    if as_of:
        q["date"] = str(as_of)[:10]
    d["quote"] = q
    s = d.get("snapshot") if isinstance(d.get("snapshot"), dict) else {}
    s.update({"price": price, "close": price, "price_source": source})
    if as_of:
        s["date"] = str(as_of)[:10]
    d["snapshot"] = s
    return d


async def _twse_openapi_latest(ticker: str):
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 AI-Stock-Research-Terminal/5.6.4"}
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=headers) as c:
        r = await c.get(url)
        r.raise_for_status()
        rows = r.json()
    if not isinstance(rows, list):
        raise RuntimeError("TWSE OpenAPI payload is not a list")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("Code") or row.get("證券代號") or "").strip() != ticker:
            continue
        p = _num(row.get("ClosingPrice") or row.get("收盤價"))
        if p is None:
            raise RuntimeError("TWSE OpenAPI matched ticker but close is invalid")
        ds = _roc_to_iso(row.get("Date") or row.get("日期"))
        return ds, p
    raise RuntimeError("TWSE OpenAPI ticker not found")


async def _twse_rwd_latest(ticker: str):
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL"
    params = {"response": "json"}
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 AI-Stock-Research-Terminal/5.6.4"}
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=headers) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        j = r.json()
    fields = j.get("fields") or []
    data = j.get("data") or []
    code_i = next((i for i,x in enumerate(fields) if "證券代號" in str(x)), 0)
    close_i = next((i for i,x in enumerate(fields) if "收盤" in str(x)), 7 if len(fields) > 7 else -1)
    ds = _roc_to_iso(j.get("date") or j.get("Date"))
    for row in data:
        if not isinstance(row, list) or len(row) <= max(code_i, close_i):
            continue
        if str(row[code_i]).strip() != ticker:
            continue
        p = _num(row[close_i])
        if p is not None:
            return ds, p
    raise RuntimeError("TWSE RWD ticker not found or invalid close")


async def _bootstrap_price(ticker: str):
    attempts = []
    for name, fn in (("TWSE OpenAPI STOCK_DAY_ALL", _twse_openapi_latest), ("TWSE RWD STOCK_DAY_ALL", _twse_rwd_latest)):
        try:
            ds, p = await fn(ticker)
            attempts.append({"source": name, "ok": True, "date": ds, "price": p})
            return (ds, p, name), attempts
        except Exception as e:
            attempts.append({"source": name, "ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"})
    return None, attempts


async def build_stock_v564(ticker: str, force_refresh: bool = False):
    # Fetch the authoritative latest listed-stock price independently from the legacy report chain.
    bootstrap = None
    attempts = []
    try:
        bootstrap, attempts = await _bootstrap_price(ticker)
    except Exception as e:
        attempts.append({"source": "price_bootstrap", "ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"})

    d = await _base(ticker, force_refresh=force_refresh)
    if not isinstance(d, dict):
        d = {"ticker": ticker, "errors": ["legacy report payload invalid"]}

    # Always prefer the direct official latest snapshot when available.
    if bootstrap:
        ds, p, source = bootstrap
        d = _apply(d, p, ds, source)
        # A previous degraded marker was caused by missing price; clear only that stale message.
        if d.get("status") == "degraded" and "價格" in str(d.get("message") or ""):
            d.pop("status", None)
            d.pop("message", None)

    diag = d.get("pipeline_diagnostics") if isinstance(d.get("pipeline_diagnostics"), dict) else {}
    diag["price_bootstrap_attempts"] = attempts
    diag["price_valid"] = _num(d.get("price")) is not None
    diag["price"] = _num(d.get("price"))
    diag["price_as_of"] = d.get("price_as_of")
    diag["price_source"] = d.get("price_source")
    d["pipeline_diagnostics"] = diag
    d["version"] = VERSION
    d["data_policy"] = (d.get("data_policy") or "") + " V5.6.4：上市股最新價獨立走 TWSE OpenAPI STOCK_DAY_ALL；失敗再走 TWSE RWD，全程保留來源診斷；舊月資料/FinMind 僅作後備。"
    return d


server.build_stock = build_stock_v564

_oldidx = run_v551._patched_index
def _idx():
    return _oldidx().replace("5.6.3", VERSION).replace("5.6.2", VERSION).replace("5.6.1", VERSION).replace("5.6.0", VERSION).replace("5.5.9", VERSION).replace("5.5.1", VERSION)
run_v551._patched_index = _idx

_oldsw = run_v551._patched_sw
def _sw():
    return _oldsw().replace("ai-stock-v5.6.3", "ai-stock-v5.6.4").replace("ai-stock-v5.6.2", "ai-stock-v5.6.4").replace("ai-stock-v5.6.1", "ai-stock-v5.6.4").replace("ai-stock-v5.6.0", "ai-stock-v5.6.4").replace("ai-stock-v5.5.9", "ai-stock-v5.6.4").replace("ai-stock-v5.5.1", "ai-stock-v5.6.4")
run_v551._patched_sw = _sw


@server.app.middleware("http")
async def runtime(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({
            "status": "ok",
            "version": VERSION,
            "mode": "direct-twse-price-bootstrap",
            "price_primary": "TWSE OpenAPI STOCK_DAY_ALL",
            "price_fallback": "TWSE RWD STOCK_DAY_ALL -> V5.6.3 legacy recovery",
            "pwa": True,
        }, headers={"Cache-Control": "no-store"})
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp

app = server.app
