"""V5.5.2 Valuation Multi-Model.

Keeps V5.5.1 official EPS integrity rules, but prevents the valuation panel from
becoming blank when four consecutive audited quarterly EPS values are not yet
available. Fallbacks are explicitly labelled estimates and never overwrite the
official EPS evidence ledger.
"""
from __future__ import annotations

from typing import Any
from fastapi import Request
from fastapi.responses import JSONResponse

import run_v551
import server

VERSION = "5.5.2"
server.app.version = VERSION
_base_build_stock = server.build_stock


def _num(v: Any):
    try:
        x=float(v)
        return x if x==x else None
    except Exception:
        return None


def _first_num(obj: dict[str,Any], keys: tuple[str,...]):
    for k in keys:
        v=_num(obj.get(k))
        if v is not None:
            return v
    return None


def _fallback_valuation(d: dict[str,Any]) -> dict[str,Any]:
    old=d.get("valuation") or {}
    if old.get("scenarios"):
        old["model_version"]=VERSION
        old["fallback_used"]=False
        return old

    price=_num(d.get("price"))
    stack=d.get("eps_stack") or {}
    research=d.get("research") or {}
    per=d.get("per") or {}
    off=d.get("official_financial") or {}

    ttm=_num(stack.get("ttm_eps"))
    ytd=_num(stack.get("ytd_eps"))
    q=int(_num(off.get("fiscal_quarter")) or 0)
    forward=_first_num(research,("median_forward_eps","forward_eps_median","forward_eps"))
    analyst_target=_first_num(research,("median_target","median_target_price","target_price_median"))
    market_per=_first_num(per,("per","pe","trailing_pe"))

    eps=None; basis=None; quality="estimate"
    if ttm is not None and ttm>0:
        eps=ttm; basis=f"TTM EPS {ttm:.2f}（官方連續四季）"; quality="official"
    elif ytd is not None and ytd>0 and q in (1,2,3):
        eps=ytd*4/q
        basis=f"YTD EPS {ytd:.2f} 年化推估 {eps:.2f}（{q}Q 已公布；非官方全年 EPS）"
    elif forward is not None and forward>0:
        eps=forward; basis=f"Forward EPS {forward:.2f}（市場預估）"

    # Use observable market PER when available; otherwise derive an implied PER
    # from current price and the selected EPS basis. Clamp only to avoid absurd
    # display ranges; the clamp is disclosed in metadata.
    anchor_per=market_per if market_per is not None and market_per>0 else ((price/eps) if price and eps else None)
    if anchor_per is not None:
        anchor_per=max(5.0,min(80.0,anchor_per))

    scenarios=[]
    method=None
    if eps is not None and eps>0 and anchor_per is not None:
        vals=[("悲觀",eps*anchor_per*0.85),("基準",eps*anchor_per),("樂觀",eps*anchor_per*1.15)]
        # If a valid analyst consensus target exists, blend only the base case
        # modestly, preserving the EPS/PER framework as the primary model.
        if analyst_target is not None and analyst_target>0:
            vals[1]=(vals[1][0],vals[1][1]*0.70+analyst_target*0.30)
            method="EPS × 市場隱含/可得 PER；基準價 30% 參考分析師目標價中位數"
        else:
            method="EPS × 市場隱含/可得 PER 情境法"
        scenarios=[{"name":n,"target":round(v,1)} for n,v in vals]
    elif analyst_target is not None and analyst_target>0:
        scenarios=[{"name":"悲觀","target":round(analyst_target*0.85,1)},{"name":"基準","target":round(analyst_target,1)},{"name":"樂觀","target":round(analyst_target*1.15,1)}]
        basis="分析師目標價中位數（EPS 證據不足時的次級模型）"
        method="分析師目標價中位數 ±15% 情境帶"
        quality="market_estimate"

    if not scenarios:
        old.update({"eps_basis":basis or "資料不足","model_version":VERSION,"fallback_used":True,"fallback_reason":"TTM/YTD/Forward EPS 與分析師目標價皆不足"})
        return old

    return {
        **old,
        "scenarios":scenarios,
        "eps_basis":basis,
        "model_version":VERSION,
        "fallback_used":quality!="official",
        "valuation_quality":quality,
        "method":method,
        "anchor_per":round(anchor_per,2) if anchor_per is not None else None,
        "analyst_target":analyst_target,
        "disclosure":"V5.5.2 多模型估值：官方 TTM 優先；不足時依序使用 YTD 年化、Forward EPS、分析師目標價。推估值不回寫官方 EPS Ledger。",
    }


async def build_stock_v552(ticker: str, force_refresh: bool=False):
    d=await _base_build_stock(ticker,force_refresh=force_refresh)
    if isinstance(d,dict):
        d["valuation"]=_fallback_valuation(d)
        d["version"]=VERSION
        d["data_policy"]=(d.get("data_policy") or "")+" V5.5.2：估值採多模型降級；官方 TTM 優先，缺季時可用 YTD 年化/Forward EPS/分析師目標價，且所有推估均明確標示、不污染官方 Evidence Ledger。"
    return d

server.build_stock=build_stock_v552


@server.app.middleware("http")
async def v552_runtime(request: Request, call_next):
    if request.url.path=="/health":
        return JSONResponse({"status":"ok","version":VERSION,"mode":"valuation-multi-model","official_ttm_first":True,"ytd_annualized_fallback":True,"forward_eps_fallback":True,"analyst_target_fallback":True,"pwa":True},headers={"Cache-Control":"no-store"})
    response=await call_next(request)
    response.headers["X-AI-Stock-Version"]=VERSION
    return response

app=server.app
