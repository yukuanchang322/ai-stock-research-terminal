"""V5.6.5 Fast Official Price Path.
Avoid the slow legacy report chain causing mobile fetch Load failed/timeout.
Price is fetched first from official TWSE snapshot; the legacy research pipeline is
allowed a short foreground budget. If it times out, return a usable price-first report
immediately and keep diagnostics explicit instead of returning HTTP 503.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

import run_v564
import run_v551
import server

VERSION = "5.6.5"
server.app.version = VERSION
_base = run_v564.build_stock_v564


def _num(v: Any):
    try:
        if v is None: return None
        x=float(str(v).replace(",", "").strip())
        return x if x > 0 else None
    except Exception:
        return None


def _minimal_report(ticker: str, bootstrap, attempts, reason: str):
    ds, price, source = bootstrap
    now=datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "ticker": ticker,
        "name": ticker,
        "industry": "—",
        "price": price,
        "current_price": price,
        "latest_price": price,
        "price_source": source,
        "price_as_of": ds,
        "change_pct": None,
        "generated_at": now,
        "version": VERSION,
        "status": "partial",
        "message": "已取得 TWSE 官方最新收盤價；完整研究資料仍在上游來源恢復中，可稍後按更新重試。",
        "quote": {"price": price, "close": price, "date": ds, "source": source},
        "snapshot": {"price": price, "close": price, "date": ds, "price_source": source},
        "technical": {"last": price, "last_date": ds, "series": [], "ma": {}, "trend": "資料載入中"},
        "revenue": {"series": []},
        "cashflow": {},
        "flow": {},
        "per": {},
        "financial": {},
        "official_financial": {},
        "financial_integrity": {},
        "eps_stack": {},
        "research": {"reports": [], "count": 0},
        "company_events": {"rows": [], "earnings_calls": [], "material_info": []},
        "valuation": {"scenarios": [], "fallback_reason": "完整研究資料載入中"},
        "scores": {"綜合": 0, "基本面": 0, "籌碼面": 0, "技術面": 0},
        "stance": "資料載入中",
        "thesis": "官方價格已取得；其餘研究模組暫時降級，不以空白或錯誤資料推論。",
        "catalysts": [], "risks": [],
        "source_status": [{"name":"股價","dataset":source,"as_of":ds,"status":"ok","scheduled_update":"交易日收盤後"}],
        "pipeline_diagnostics": {
            "mode": "fast_price_first_partial",
            "legacy_reason": reason,
            "price_bootstrap_attempts": attempts,
            "price_valid": True,
            "price": price,
            "price_as_of": ds,
            "price_source": source,
        },
        "errors": [reason],
        "data_policy": "V5.6.5：TWSE 官方價格優先；完整研究鏈超時時回傳 partial 200，避免前端 Load failed。",
    }


async def build_stock_v565(ticker: str, force_refresh: bool=False):
    # Price bootstrap is independent and fast.
    bootstrap=None; attempts=[]
    try:
        bootstrap, attempts = await run_v564._bootstrap_price(ticker)
    except Exception as e:
        attempts=[{"source":"price_bootstrap","ok":False,"error":f"{type(e).__name__}: {str(e)[:160]}"}]

    # Keep mobile request comfortably below common proxy/browser timeouts.
    try:
        d = await asyncio.wait_for(_base(ticker, force_refresh=force_refresh), timeout=22.0)
    except asyncio.TimeoutError:
        if bootstrap:
            return _minimal_report(ticker, bootstrap, attempts, "完整研究鏈超過 22 秒，已切換官方價格快速模式")
        raise
    except Exception as e:
        if bootstrap:
            return _minimal_report(ticker, bootstrap, attempts, f"完整研究鏈暫時失敗：{type(e).__name__}")
        raise

    if not isinstance(d, dict):
        if bootstrap:
            return _minimal_report(ticker, bootstrap, attempts, "完整研究鏈回傳格式異常")
        return d

    # Ensure official bootstrap wins even when legacy payload contains stale aliases.
    if bootstrap:
        ds, p, source = bootstrap
        d = run_v564._apply(d, p, ds, source)
    diag=d.get("pipeline_diagnostics") if isinstance(d.get("pipeline_diagnostics"),dict) else {}
    diag["foreground_budget_seconds"]=22
    diag["price_bootstrap_attempts"]=attempts
    d["pipeline_diagnostics"]=diag
    d["version"]=VERSION
    d["data_policy"]=(d.get("data_policy") or "")+" V5.6.5：完整研究鏈設 22 秒前景預算；TWSE 官方價格可用時，超時改回 partial HTTP 200，不再讓手機前端顯示 Load failed。"
    return d


server.build_stock = build_stock_v565

_oldidx=run_v551._patched_index
def _idx():
    return _oldidx().replace("5.6.4",VERSION).replace("5.6.3",VERSION).replace("5.6.2",VERSION).replace("5.6.1",VERSION).replace("5.6.0",VERSION).replace("5.5.9",VERSION).replace("5.5.1",VERSION)
run_v551._patched_index=_idx

_oldsw=run_v551._patched_sw
def _sw():
    text=_oldsw()
    for v in ("5.6.4","5.6.3","5.6.2","5.6.1","5.6.0","5.5.9","5.5.1"):
        text=text.replace(f"ai-stock-v{v}",f"ai-stock-v{VERSION}")
    return text
run_v551._patched_sw=_sw

@server.app.middleware("http")
async def runtime(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({"status":"ok","version":VERSION,"mode":"fast-official-price-first","foreground_budget_seconds":22,"partial_200_on_legacy_timeout":True,"pwa":True},headers={"Cache-Control":"no-store"})
    resp=await call_next(request)
    resp.headers["X-AI-Stock-Version"]=VERSION
    return resp

app=server.app
