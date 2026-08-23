"""V5.8.0 Modular Recovery.

Critical architectural repair: the legacy research core must not fail merely because
its own price provider is unavailable. Fetch an authoritative TWSE price first, inject
that verified price into the legacy FinMind price dataset, then let the existing mature
research engine build technical/revenue/financial/EPS/valuation modules normally.
Optional web/MCP/event providers remain deferred in the foreground.
"""
from __future__ import annotations

import asyncio
from typing import Any
from fastapi import Request
from fastapi.responses import JSONResponse

import run_v564
import run_v566
import run_v572
import run_v551
import server

VERSION = "5.8.0"
server.app.version = VERSION
_core_base = run_v566._core_base
_real_finmind = server.finmind


def _price_rows(ticker: str, boot) -> list[dict[str, Any]]:
    if not boot:
        return []
    ds, price, source = boot
    return [{
        "date": ds,
        "stock_id": ticker,
        "open": price,
        "max": price,
        "min": price,
        "close": price,
        "Trading_Volume": 0,
        "Trading_money": 0,
        "spread": 0,
        "Trading_turnover": 0,
        "_source": source,
        "_verified_bootstrap": True,
    }]


async def _build_with_price_injection(ticker: str, boot, force_refresh: bool = False):
    """Run mature core while preventing the legacy price gate from aborting all modules."""
    old_finmind = server.finmind
    old_pub = server.fetch_public_research
    old_evt = server.fetch_company_events
    old_mcp = server.fetch_twstock_mcp_snapshot

    async def resilient_finmind(dataset: str, data_ticker: str | None = None, start=None, end=None):
        # The legacy server requires TaiwanStockPrice before it proceeds to every other
        # module. If that provider is down, supply the independently verified TWSE close
        # rather than aborting revenue/financial/EPS/institutional collection.
        if dataset == "TaiwanStockPrice" and data_ticker == ticker and boot:
            try:
                rows = await asyncio.wait_for(_real_finmind(dataset, data_ticker, start, end), timeout=5.0)
                if rows:
                    return rows
            except Exception:
                pass
            return _price_rows(ticker, boot)
        try:
            return await _real_finmind(dataset, data_ticker, start, end)
        except Exception:
            return []

    server.finmind = resilient_finmind
    server.fetch_public_research = run_v566._empty_public_research
    server.fetch_company_events = run_v566._empty_company_events
    server.fetch_twstock_mcp_snapshot = run_v566._empty_mcp
    try:
        return await _core_base(ticker, force_refresh=force_refresh)
    finally:
        server.finmind = old_finmind
        server.fetch_public_research = old_pub
        server.fetch_company_events = old_evt
        server.fetch_twstock_mcp_snapshot = old_mcp


def _compat(d: dict[str, Any]) -> dict[str, Any]:
    d = run_v572._compat(d)
    d["version"] = VERSION
    if not isinstance(d.get("source_status"), list):
        d["source_status"] = []
    if not isinstance(d.get("scores"), dict):
        d["scores"] = {"綜合": None, "基本面": None, "籌碼面": None, "技術面": None, "估值": None}
    return d


async def build_stock_v580(ticker: str, force_refresh: bool = False):
    boot = None
    attempts = []
    try:
        boot, attempts = await asyncio.wait_for(run_v564._bootstrap_price(ticker), timeout=8.0)
    except Exception as e:
        attempts = [{"source": "official_price_bootstrap", "ok": False,
                     "error": f"{type(e).__name__}: {str(e)[:160]}"}]

    # If official bootstrap is available, it becomes the price dependency injected into
    # the core. Crucially, other datasets are still attempted independently.
    try:
        d = await asyncio.wait_for(_build_with_price_injection(ticker, boot, force_refresh), timeout=45.0)
    except asyncio.TimeoutError:
        d = None
    except Exception as e:
        d = None
        core_error = f"{type(e).__name__}: {str(e)[:180]}"

    if not isinstance(d, dict):
        # Never return a whole-page failure. This shell is only the last resort; it also
        # exposes diagnostics so the next repair targets the exact independent module.
        d = {
            "ticker": ticker, "name": ticker, "industry": "—", "version": VERSION,
            "pipeline_state": "partial", "report_partial": True,
            "technical": {"series": []}, "revenue": {"series": []}, "financial": {},
            "official_financial": {}, "financial_integrity": {}, "cashflow": {}, "flow": {},
            "per": {}, "eps_stack": {}, "research": {"reports": [], "count": 0},
            "company_events": {"rows": [], "earnings_calls": [], "material_info": []},
            "valuation": {"scenarios": [], "status": "waiting_for_eps", "eps_basis": "資料取得中"},
            "scores": {"綜合": None, "基本面": None, "籌碼面": None, "技術面": None, "估值": None},
            "source_status": [],
            "status_text": "模組化資料恢復中；單一來源失敗不再中止整份報告",
        }
        if 'core_error' in locals():
            d["core_error"] = core_error

    if boot:
        ds, p, source = boot
        d = run_v564._apply(d, p, ds, source)
        try:
            d = run_v566._recompute_after_price(d)
        except Exception:
            pass

    # Revenue repair has its own budget and cannot erase already obtained modules.
    try:
        d = await asyncio.wait_for(run_v566._repair_revenue_bounded(ticker, d), timeout=8.0)
    except Exception:
        pass

    # Institutional/margin official enrichment remains isolated/background-capable.
    try:
        d = run_v566._merge_official_cache(d, run_v566._official_cache.get(ticker))
        run_v566._schedule_official(ticker, d)
    except Exception:
        pass

    diag = d.get("pipeline_diagnostics") if isinstance(d.get("pipeline_diagnostics"), dict) else {}
    diag.update({
        "mode": "modular_price_injection",
        "official_price_bootstrap": bool(boot),
        "price_bootstrap_attempts": attempts,
        "legacy_price_gate_bypassed": bool(boot),
        "whole_report_abort_on_price_failure": False,
        "technical_rows": len((d.get("technical") or {}).get("series") or []),
        "revenue_rows": len((d.get("revenue") or {}).get("series") or []),
        "financial_available": bool(d.get("financial") or d.get("official_financial") or d.get("eps_stack")),
    })
    d["pipeline_diagnostics"] = diag
    d["data_policy"] = (d.get("data_policy") or "") + (
        " V5.8.0 Modular Recovery：TWSE 官方價格先獨立驗證並注入舊核心，解除價格來源失敗造成的整份研究提前中止；"
        "營收、財報/EPS、籌碼、技術與估值各自保留可用結果，單一來源失敗只影響該模組。"
    )
    return _compat(d)


server.build_stock = build_stock_v580

_oldidx = run_v551._patched_index
def _idx():
    text = _oldidx()
    for v in ("5.7.4","5.7.3","5.7.2","5.7.1","5.7.0","5.6.6","5.6.5","5.6.4","5.6.3","5.6.2","5.6.1","5.6.0","5.5.9","5.5.1"):
        text = text.replace(v, VERSION)
    return text
run_v551._patched_index = _idx

_oldsw = run_v551._patched_sw
def _sw():
    text = _oldsw()
    for v in ("5.7.4","5.7.3","5.7.2","5.7.1","5.7.0","5.6.6","5.6.5","5.6.4","5.6.3","5.6.2","5.6.1","5.6.0","5.5.9","5.5.1"):
        text = text.replace(f"ai-stock-v{v}", f"ai-stock-v{VERSION}")
    return text
run_v551._patched_sw = _sw

@server.app.middleware("http")
async def runtime(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({
            "status": "ok", "version": VERSION, "mode": "modular-recovery",
            "official_price_injection": True,
            "whole_report_abort_on_price_failure": False,
            "modules": ["price", "technical", "revenue", "financial_eps", "positioning", "valuation"],
        }, headers={"Cache-Control": "no-store"})
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp

app = server.app
