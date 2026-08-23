"""V5.4.9 Revenue Bars + Market-Anchored Valuation.

Keeps V5.4.8 official valuation data recovery, but prevents a sparse historical PER
sample from producing misleadingly remote fair values. When historical PE percentiles
are unavailable, the model anchors the multiple to the price / selected EPS basis and
uses a narrower scenario band. Analyst target consensus is used only as a transparent
secondary blend when coverage is sufficient.
"""
from __future__ import annotations

from typing import Any
from fastapi import Request
from fastapi.responses import JSONResponse

import run_v548
import server

VERSION = "5.4.9"
server.app.version = VERSION
_v548_model_valuation = server.model_valuation
_v548_build_stock = server.build_stock


def _num(v: Any):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def model_valuation_v549(price, perdata, eps_stack, research, integrity=None):
    out = _v548_model_valuation(price, perdata, eps_stack, research, integrity)
    if not price or not isinstance(out, dict) or not out.get("scenarios"):
        return out

    # Historical PE percentiles are the preferred anchor. V5.4.8's TWSE fallback may
    # provide only the latest observation, so never pretend that one point is history.
    has_history = bool(perdata.get("pe_median") and int(perdata.get("sample_count") or 0) >= 20)
    anchor_eps = _num(out.get("anchor_eps"))
    if has_history or not anchor_eps or anchor_eps <= 0:
        out["valuation_anchor"] = "historical_pe_percentiles" if has_history else "legacy_fallback"
        out["version"] = VERSION
        return out

    implied_pe = float(price) / anchor_eps
    # Sparse-history mode: anchor the base case to today's market multiple for the same
    # EPS definition. This removes the apples-vs-oranges error of combining market PER
    # (often trailing) with a different EPS basis (TTM/forward) and calling it fair value.
    bear_pe = max(5.0, implied_pe * 0.88)
    base_pe = implied_pe
    bull_pe = min(150.0, implied_pe * 1.12)
    scenarios = [
        {"name": "悲觀", "eps": anchor_eps * 0.90, "pe": bear_pe},
        {"name": "基準", "eps": anchor_eps, "pe": base_pe},
        {"name": "樂觀", "eps": anchor_eps * 1.10, "pe": bull_pe},
    ]
    for x in scenarios:
        x["target"] = x["eps"] * x["pe"]
        x["upside_pct"] = (x["target"] / float(price) - 1) * 100

    # If at least two comparable analyst targets exist, blend only the base target 25%
    # toward consensus. The model remains market anchored and the external estimate is
    # visible rather than silently dominating the result.
    analyst_target = _num(research.get("median_target"))
    target_count = len([r for r in (research.get("reports") or []) if _num(r.get("target_price")) is not None])
    blend_note = ""
    if analyst_target and target_count >= 2 and 0.5 * float(price) <= analyst_target <= 1.8 * float(price):
        raw_base = scenarios[1]["target"]
        blended = raw_base * 0.75 + analyst_target * 0.25
        scale = blended / raw_base if raw_base else 1.0
        for x in scenarios:
            x["target"] *= scale
            x["upside_pct"] = (x["target"] / float(price) - 1) * 100
        blend_note = f"；基準價含25%法人目標價中位數權重（{target_count}筆）"

    out.update({
        "scenarios": scenarios,
        "pe_basis": f"歷史PER樣本不足：以現價/同一EPS基礎反推市場隱含PER {implied_pe:.1f}x，情境採 ±12% multiple；EPS 採 -10%/基準/+10%{blend_note}",
        "confidence": min(int(out.get("confidence") or 50), 68),
        "valuation_anchor": "market_implied_pe_same_eps_basis",
        "market_implied_pe": round(implied_pe, 2),
        "historical_pe_sample_count": int(perdata.get("sample_count") or 0),
        "version": VERSION,
        "guardrail": "歷史估值樣本不足時不輸出假精準的長期合理PER；基準價以目前市場對同一EPS基礎的定價為錨。",
    })
    return out


server.model_valuation = model_valuation_v549


async def build_stock_v549(ticker: str, force_refresh: bool = False):
    d = await _v548_build_stock(ticker, force_refresh=force_refresh)
    if isinstance(d, dict):
        d["version"] = VERSION
        d["data_policy"] = (d.get("data_policy") or "") + " V5.4.9：營收成長改以月度YoY柱狀圖呈現；估值在歷史PER樣本不足時改用同EPS基礎的市場隱含PER錨定，避免估值與現價因口徑錯配產生巨大偏差。"
    return d


server.build_stock = build_stock_v549


@server.app.middleware("http")
async def v549_runtime_metadata(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({
            "status": "ok", "version": VERSION,
            "mode": "revenue-bars+market-anchored-valuation+valuation-recovery+data-recovery",
            "valuation_guardrail": True, "revenue_yoy_bars": True,
            "finmind_token": bool(server.FINMIND_TOKEN), "cache_ttl_seconds": server.CACHE_TTL,
            "pwa": True, "official_fallback": True, "data_recovery": True,
        }, headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    response.headers["X-AI-Stock-Version"] = VERSION
    return response


app = server.app
