"""V5.6.3 Official Price Recovery.
Use official TWSE daily history as the primary rescue when the report has no valid price,
then fall back to the V5.6.2 FinMind recovery. Existing usable research is preserved.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

import run_v562
import run_v551
import server

VERSION = "5.6.3"
server.app.version = VERSION
_base = run_v562.build_stock_v562


def _num(v: Any):
    try:
        if v is None:
            return None
        s = str(v).replace(",", "").strip()
        if not s or s in ("--", "-", "None", "null", "X"):
            return None
        x = float(s)
        return x if x > 0 else None
    except Exception:
        return None


def _existing_price(d: dict[str, Any]):
    for obj in (d, d.get("snapshot") or {}, d.get("quote") or {}):
        if not isinstance(obj, dict):
            continue
        for k in ("price", "close", "latest_price", "current_price", "last", "last_price"):
            p = _num(obj.get(k))
            if p is not None:
                return p, obj.get("date") or obj.get("as_of") or d.get("price_as_of")
    tech = (d.get("technical") or {}).get("series") or []
    for row in reversed(tech):
        if isinstance(row, dict):
            p = _num(row.get("close"))
            if p is not None:
                return p, row.get("date")
    return None, None


def _apply(d: dict[str, Any], price: float, as_of: Any, source: str):
    d["price"] = price
    d["current_price"] = price
    d["latest_price"] = price
    d["price_source"] = source
    d["price_as_of"] = str(as_of)[:10] if as_of else None
    q = d.get("quote") if isinstance(d.get("quote"), dict) else {}
    q.update({"price": price, "close": price, "source": source})
    if as_of: q["date"] = str(as_of)[:10]
    d["quote"] = q
    s = d.get("snapshot") if isinstance(d.get("snapshot"), dict) else {}
    s.update({"price": price, "close": price, "price_source": source})
    if as_of: s["date"] = str(as_of)[:10]
    d["snapshot"] = s
    return d


async def _twse_month(ticker: str, yyyymmdd: str):
    url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
    params = {"response": "json", "date": yyyymmdd, "stockNo": ticker}
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 AI-Stock-Research-Terminal/5.6.3"}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as c:
            r = await c.get(url, params=params)
            if r.status_code >= 400:
                return []
            j = r.json()
            fields = j.get("fields") or []
            data = j.get("data") or []
            close_i = next((i for i,x in enumerate(fields) if "收盤" in str(x)), 6 if len(fields) > 6 else -1)
            out = []
            for row in data:
                if not isinstance(row, list) or close_i < 0 or len(row) <= close_i:
                    continue
                p = _num(row[close_i])
                raw = str(row[0]) if row else ""
                parts = raw.split("/")
                ds = raw
                if len(parts) == 3:
                    try: ds = f"{int(parts[0])+1911:04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
                    except Exception: pass
                if p is not None:
                    out.append((ds, p))
            return out
    except Exception:
        return []


async def _official_price(ticker: str):
    today = date.today()
    for offset in (0, 32, 63):
        d = today - timedelta(days=offset)
        rows = await _twse_month(ticker, d.strftime("%Y%m01"))
        if rows:
            return sorted(rows, key=lambda z: z[0])[-1]
    return None


async def build_stock_v563(ticker: str, force_refresh: bool = False):
    d = await _base(ticker, force_refresh=force_refresh)
    if not isinstance(d, dict):
        return d
    p, ds = _existing_price(d)
    # V5.6.2 may still return no valid price. In that case independently query TWSE.
    if p is None:
        try:
            official = await _official_price(ticker)
            if official:
                ds, p = official
                d = _apply(d, p, ds, "TWSE official STOCK_DAY")
        except Exception as e:
            d["official_price_recovery_error"] = type(e).__name__
    p, ds = _existing_price(d)
    diag = d.get("pipeline_diagnostics") if isinstance(d.get("pipeline_diagnostics"), dict) else {}
    diag.update({"price_valid": p is not None, "price": p, "price_as_of": d.get("price_as_of") or ds, "price_source": d.get("price_source")})
    d["pipeline_diagnostics"] = diag
    d["version"] = VERSION
    d["data_policy"] = (d.get("data_policy") or "") + " V5.6.3：價格缺失時新增 TWSE 官方 STOCK_DAY 月資料救援，保留 V5.6.2/FinMind 備援與價格來源日期診斷。"
    return d


server.build_stock = build_stock_v563

_oldidx = run_v551._patched_index
def _idx():
    return _oldidx().replace("5.6.2", VERSION).replace("5.6.1", VERSION).replace("5.6.0", VERSION).replace("5.5.9", VERSION).replace("5.5.1", VERSION)
run_v551._patched_index = _idx

_oldsw = run_v551._patched_sw
def _sw():
    return _oldsw().replace("ai-stock-v5.6.2", "ai-stock-v5.6.3").replace("ai-stock-v5.6.1", "ai-stock-v5.6.3").replace("ai-stock-v5.6.0", "ai-stock-v5.6.3").replace("ai-stock-v5.5.9", "ai-stock-v5.6.3").replace("ai-stock-v5.5.1", "ai-stock-v5.6.3")
run_v551._patched_sw = _sw

@server.app.middleware("http")
async def runtime(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({"status":"ok","version":VERSION,"mode":"official-price-recovery","price_recovery":"existing -> technical -> FinMind -> TWSE official STOCK_DAY","pwa":True}, headers={"Cache-Control":"no-store"})
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp

app = server.app
