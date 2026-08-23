"""V5.5.4 Data Series Repair.

Repairs three data-series issues together:
1) guarantees enough monthly revenue history for a true 12-month YoY bar chart,
2) restores 1/5/20-day margin and short balance changes from historical balance rows,
3) keeps institutional cash-flow amounts explicitly labelled as estimates.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

import run_v553
import run_v551
import server

VERSION="5.5.4"
server.app.version=VERSION
_base_build_stock=server.build_stock


def _num(v: Any):
    try:
        if v is None: return None
        return float(str(v).replace(",","").strip())
    except Exception:
        return None


async def _finmind(dataset:str,ticker:str,start_date:str):
    params={"dataset":dataset,"data_id":ticker,"start_date":start_date}
    token=os.getenv("FINMIND_TOKEN")
    if token: params["token"]=token
    try:
        async with httpx.AsyncClient(timeout=15,follow_redirects=True) as client:
            r=await client.get("https://api.finmindtrade.com/api/v4/data",params=params)
            if r.status_code>=400: return []
            j=r.json()
            return j.get("data") or [] if isinstance(j,dict) else []
    except Exception:
        return []


async def _repair_revenue(ticker:str,d:dict[str,Any]):
    # Need at least 24+ months so the latest 12 months each have a prior-year base.
    start=(date.today()-timedelta(days=900)).isoformat()
    rows=await _finmind("TaiwanStockMonthRevenue",ticker,start)
    series=[]
    for x in rows:
        rev=_num(x.get("revenue"))
        y=x.get("revenue_year"); m=x.get("revenue_month")
        if rev is None: continue
        try: period=f"{int(y):04d}-{int(m):02d}"
        except Exception:
            ds=str(x.get("date") or "")
            period=ds[:7] if len(ds)>=7 else None
        if period: series.append({"period":period,"revenue":rev,"date":x.get("date")})
    # Deduplicate by month and sort.
    by={x["period"]:x for x in series}
    series=[by[k] for k in sorted(by)][-30:]
    if len(series)>=13:
        rev=d.get("revenue") or {}
        rev["series"]=series
        rev["history_months"]=len(series)
        rev["series_source"]="FinMind TaiwanStockMonthRevenue"
        rev["yoy_bar_ready"]=len(series)>=24
        d["revenue"]=rev
    return d


def _pick_balance(row:dict[str,Any],keys:tuple[str,...]):
    for k in keys:
        v=_num(row.get(k))
        if v is not None: return v
    return None


async def _repair_margin_short(ticker:str,d:dict[str,Any]):
    start=(date.today()-timedelta(days=180)).isoformat()
    rows=await _finmind("TaiwanStockMarginPurchaseShortSale",ticker,start)
    hist=[]
    for x in rows:
        mb=_pick_balance(x,("MarginPurchaseTodayBalance","MarginPurchaseTodayBalanceValue","margin_purchase_today_balance"))
        sb=_pick_balance(x,("ShortSaleTodayBalance","ShortSaleTodayBalanceValue","short_sale_today_balance"))
        ds=str(x.get("date") or "")[:10]
        if ds and (mb is not None or sb is not None): hist.append({"date":ds,"margin_balance":mb,"short_balance":sb})
    by={x["date"]:x for x in hist}; hist=[by[k] for k in sorted(by)]
    if len(hist)<2: return d
    latest=hist[-1]
    def chg(n,key):
        if len(hist)<=n: return None
        old=hist[-1-n].get(key); now=latest.get(key)
        if old in (None,0) or now is None: return None
        return (now/old-1)*100
    ms={}
    for n in (1,5,20):
        ms[str(n)]={"margin_pct":chg(n,"margin_balance"),"short_pct":chg(n,"short_balance"),"margin_balance":latest.get("margin_balance"),"short_balance":latest.get("short_balance")}
    cf=d.get("cashflow") or {}
    cf["margin_short"]=ms
    cf["margin_short_as_of"]=latest.get("date")
    cf["margin_short_source"]="FinMind TaiwanStockMarginPurchaseShortSale"
    d["cashflow"]=cf
    return d


async def build_stock_v554(ticker:str,force_refresh:bool=False):
    d=await _base_build_stock(ticker,force_refresh=force_refresh)
    if isinstance(d,dict):
        try: d=await _repair_revenue(ticker,d)
        except Exception: pass
        try: d=await _repair_margin_short(ticker,d)
        except Exception: pass
        cf=d.get("cashflow") or {}
        cf["amount_note"]="法人買進/賣出金額＝官方 T86 買賣股數 × 各交易日收盤價；為估算成交金額，單位新台幣。"
        d["cashflow"]=cf
        d["version"]=VERSION
        d["data_policy"]=(d.get("data_policy") or "")+" V5.5.4：補足月營收歷史以產生逐月 12 根 YoY 柱；融資融券改由歷史餘額計算 1/5/20 日變化。"
    return d

server.build_stock=build_stock_v554

# Update V5.5.1 core UI version/cache strings while preserving its monthly-bar renderer.
_old_index=run_v551._patched_index
def _index554(): return _old_index().replace("5.5.3","5.5.4").replace("5.5.2","5.5.4").replace("5.5.1","5.5.4")
run_v551._patched_index=_index554
_old_sw=run_v551._patched_sw
def _sw554(): return _old_sw().replace("ai-stock-v5.5.1","ai-stock-v5.5.4")
run_v551._patched_sw=_sw554

@server.app.middleware("http")
async def v554_runtime(request:Request,call_next):
    if request.url.path=="/health":
        return JSONResponse({"status":"ok","version":VERSION,"mode":"monthly-revenue-series+margin-short-history","revenue_12_month_bars":True,"margin_short_1_5_20":True,"pwa":True},headers={"Cache-Control":"no-store"})
    response=await call_next(request); response.headers["X-AI-Stock-Version"]=VERSION; return response

app=server.app
