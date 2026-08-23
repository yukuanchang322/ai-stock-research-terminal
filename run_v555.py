"""V5.5.5 Verified Series Repair.

Fixes the two root causes left in V5.5.4:
- revenue history is accepted only when enough months exist for a true 12-month YoY chart;
  FinMind is queried in bounded chunks and merged to avoid truncated history.
- margin/short history uses FinMind first, then TWSE MI_MARGN by actual trading dates as
  an official fallback, so 1/5/20-day changes do not silently remain blank.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

import run_v554
import run_v553
import run_v551
import server

VERSION="5.5.5"
server.app.version=VERSION
_base_build_stock=server.build_stock


def _num(v: Any):
    try:
        if v is None: return None
        s=str(v).replace(",","").replace("--","").strip()
        return float(s) if s else None
    except Exception:
        return None


async def _finmind_range(dataset:str,ticker:str,start_date:str,end_date:str):
    params={"dataset":dataset,"data_id":ticker,"start_date":start_date,"end_date":end_date}
    token=os.getenv("FINMIND_TOKEN")
    if token: params["token"]=token
    try:
        async with httpx.AsyncClient(timeout=18,follow_redirects=True) as client:
            r=await client.get("https://api.finmindtrade.com/api/v4/data",params=params)
            if r.status_code>=400: return []
            j=r.json()
            return (j.get("data") or []) if isinstance(j,dict) else []
    except Exception:
        return []


def _month_shift(y:int,m:int,delta:int):
    z=y*12+(m-1)+delta
    return z//12,z%12+1


async def _revenue_history(ticker:str):
    # Query in 12-month chunks because provider responses may be truncated even with a long start_date.
    today=date.today()
    chunks=[]
    for back in (0,12,24):
        ey,em=_month_shift(today.year,today.month,-back)
        sy,sm=_month_shift(ey,em,-11)
        start=f"{sy:04d}-{sm:02d}-01"
        # end at first day after chunk's ending month
        ny,nm=_month_shift(ey,em,1)
        end=f"{ny:04d}-{nm:02d}-01"
        chunks.append(_finmind_range("TaiwanStockMonthRevenue",ticker,start,end))
    parts=await asyncio.gather(*chunks)
    by={}
    for rows in parts:
        for x in rows:
            rev=_num(x.get("revenue"))
            if rev is None: continue
            y=x.get("revenue_year");m=x.get("revenue_month")
            try: period=f"{int(y):04d}-{int(m):02d}"
            except Exception:
                ds=str(x.get("date") or "")
                period=ds[:7] if len(ds)>=7 else ""
            if period: by[period]={"period":period,"revenue":rev,"date":x.get("date")}
    return [by[k] for k in sorted(by)][-36:]


def _compute_yoy(series:list[dict[str,Any]]):
    m={str(x.get("period")): _num(x.get("revenue")) for x in series}
    out=[]
    for x in series:
        p=str(x.get("period") or "")
        try:
            y=int(p[:4]); mo=int(p[5:7]); base=m.get(f"{y-1:04d}-{mo:02d}"); cur=_num(x.get("revenue"))
        except Exception: continue
        if cur is not None and base not in (None,0):
            out.append({"period":p,"yoy":(cur/base-1)*100,"revenue":cur})
    return out[-12:]


async def _repair_revenue(ticker:str,d:dict[str,Any]):
    fetched=await _revenue_history(ticker)
    existing=(d.get("revenue") or {}).get("series") or []
    by={}
    for x in list(existing)+list(fetched):
        p=str(x.get("period") or "")
        rv=_num(x.get("revenue"))
        if p and rv is not None: by[p]={**x,"period":p,"revenue":rv}
    series=[by[k] for k in sorted(by)][-36:]
    yoy=_compute_yoy(series)
    rev=d.get("revenue") or {}
    if len(yoy)>=12:
        rev["series"]=series
        rev["yoy_series"]=yoy
        rev["history_months"]=len(series)
        rev["yoy_months"]=len(yoy)
        rev["yoy_bar_ready"]=True
        rev["series_source"]="FinMind chunked monthly revenue + existing recovery merge"
    else:
        # Never claim a 12-month chart when only one/few points are computable.
        rev["yoy_series"]=yoy
        rev["history_months"]=len(series)
        rev["yoy_months"]=len(yoy)
        rev["yoy_bar_ready"]=False
        rev["series_warning"]=f"need 12 YoY points, got {len(yoy)}"
    d["revenue"]=rev
    return d


def _row_dict(payload:dict[str,Any],ticker:str):
    for fields,data in [((payload or {}).get("fields") or [],(payload or {}).get("data") or [])]:
        for row in data:
            q=dict(zip(fields,row)); code=str(q.get("股票代號") or q.get("證券代號") or q.get("代號") or "").strip()
            if code==ticker:return q
    for tab in (payload or {}).get("tables") or []:
        fields=tab.get("fields") or []
        for row in tab.get("data") or []:
            q=dict(zip(fields,row)); code=str(q.get("股票代號") or q.get("證券代號") or q.get("代號") or "").strip()
            if code==ticker:return q
    return None


def _pick(row:dict[str,Any]|None, words:tuple[str,...]):
    if not row:return None
    for k,v in row.items():
        ks=str(k).replace(" ","")
        if all(w in ks for w in words):
            n=_num(v)
            if n is not None:return n
    return None


async def _twse_margin_day(client:httpx.AsyncClient,ticker:str,ds:str):
    day=ds.replace("-","")
    for select in ("ALL","MS"):
        try:
            r=await client.get("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",params={"date":day,"selectType":select,"response":"json"},timeout=15)
            if r.status_code>=400: continue
            j=r.json(); row=_row_dict(j if isinstance(j,dict) else {},ticker)
            if not row: continue
            mb=_pick(row,("融資","今日餘額")); sb=_pick(row,("融券","今日餘額"))
            if mb is not None or sb is not None:
                return {"date":ds,"margin_balance":mb,"short_balance":sb,"source":"TWSE MI_MARGN"}
        except Exception:
            continue
    return None


async def _margin_history_finmind(ticker:str,start:str,end:str):
    rows=await _finmind_range("TaiwanStockMarginPurchaseShortSale",ticker,start,end)
    out=[]
    for x in rows:
        mb=None;sb=None
        for k in ("MarginPurchaseTodayBalance","MarginPurchaseTodayBalanceValue","margin_purchase_today_balance"):
            mb=_num(x.get(k))
            if mb is not None:break
        for k in ("ShortSaleTodayBalance","ShortSaleTodayBalanceValue","short_sale_today_balance"):
            sb=_num(x.get(k))
            if sb is not None:break
        ds=str(x.get("date") or "")[:10]
        if ds and (mb is not None or sb is not None):out.append({"date":ds,"margin_balance":mb,"short_balance":sb,"source":"FinMind"})
    return out


def _changes(hist:list[dict[str,Any]]):
    by={x["date"]:x for x in hist if x.get("date")}; hist=[by[k] for k in sorted(by)]
    if len(hist)<2:return {},None
    latest=hist[-1]
    def c(n,key):
        if len(hist)<=n:return None
        old=hist[-1-n].get(key);now=latest.get(key)
        return None if old in (None,0) or now is None else (now/old-1)*100
    out={}
    for n in (1,5,20):out[str(n)]={"margin_pct":c(n,"margin_balance"),"short_pct":c(n,"short_balance"),"margin_balance":latest.get("margin_balance"),"short_balance":latest.get("short_balance")}
    return out,latest.get("date")


async def _repair_margin_short(ticker:str,d:dict[str,Any]):
    tech=[x for x in (d.get("technical") or {}).get("series",[]) if x.get("date")][-35:]
    if not tech:return d
    start=str(tech[0]["date"]); end=(date.fromisoformat(str(tech[-1]["date"]))+timedelta(days=1)).isoformat()
    hist=await _margin_history_finmind(ticker,start,end)
    # If provider history is insufficient, fetch official balances for all needed trading dates.
    if len(hist)<21:
        dates=[str(x["date"]) for x in tech[-25:]]
        async with httpx.AsyncClient(follow_redirects=True,headers={"User-Agent":"Mozilla/5.0"}) as client:
            rows=await asyncio.gather(*(_twse_margin_day(client,ticker,ds) for ds in dates))
        hist=[x for x in rows if x]
    ms,asof=_changes(hist)
    cf=d.get("cashflow") or {}
    cf["margin_short"]=ms
    cf["margin_short_as_of"]=asof
    cf["margin_short_source"]=(hist[-1].get("source") if hist else "missing")
    cf["margin_short_history_rows"]=len(hist)
    d["cashflow"]=cf
    return d


async def build_stock_v555(ticker:str,force_refresh:bool=False):
    d=await _base_build_stock(ticker,force_refresh=force_refresh)
    if isinstance(d,dict):
        try:d=await _repair_revenue(ticker,d)
        except Exception as e:(d.get("revenue") or {}).update({"series_error":str(e)[:120]})
        try:d=await _repair_margin_short(ticker,d)
        except Exception as e:(d.get("cashflow") or {}).update({"margin_short_error":str(e)[:120]})
        d["version"]=VERSION
        d["data_policy"]=(d.get("data_policy") or "")+" V5.5.5：月營收只在取得完整 12 個 YoY 點後才標示完整柱狀圖；融資融券不足時逐交易日回補 TWSE MI_MARGN。"
    return d

server.build_stock=build_stock_v555

# Frontend: consume backend-precomputed YoY series first, and explicitly show coverage.
_old_js=run_v551._patched_app_js
_PATCH_JS=r'''
function revenueBarSvgVerified(rev){
 const data=(rev?.yoy_series||[]).filter(x=>x&&x.period&&Number.isFinite(Number(x.yoy))).slice(-12);
 if(!data.length)return '<div class="empty">月營收 YoY 歷史不足</div>';
 const w=720,h=230,pl=42,pr=10,pt=22,pb=38,vals=data.map(x=>Number(x.yoy)),mn=Math.min(0,...vals),mx=Math.max(0,...vals),span=(mx-mn)||1;
 const y=v=>pt+(mx-v)*(h-pt-pb)/span,zero=y(0),step=(w-pl-pr)/data.length,bw=Math.max(10,step*.62);
 const bars=data.map((x,i)=>{const v=Number(x.yoy),cx=pl+(i+.5)*step,yy=y(v),top=Math.min(yy,zero),bh=Math.max(1,Math.abs(yy-zero));return `<g><rect class="revenue-bar ${v>=0?'positive':'negative'}" x="${cx-bw/2}" y="${top}" width="${bw}" height="${bh}" rx="3"><title>${x.period} ${v>=0?'+':''}${v.toFixed(1)}%</title></rect><text class="revenue-xlabel" x="${cx}" y="${h-12}" text-anchor="middle">${x.period.slice(5)}</text></g>`}).join('');
 const last=data[data.length-1];return `<div class="revenue-yoy-head"><b>逐月營收 YoY</b><span>${data.length}/12 月｜最新 ${Number(last.yoy)>=0?'+':''}${Number(last.yoy).toFixed(1)}%</span></div><svg class="revenue-bar-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line class="revenue-zero" x1="${pl}" y1="${zero}" x2="${w-pr}" y2="${zero}"/>${bars}</svg><small class="chart-note">月份依序顯示；綠色為年增、紅色為年減。資料覆蓋 ${data.length}/12 個月。</small>`;
}
'''
def _js555():
    text=_old_js()
    if "function revenueBarSvgVerified" not in text:text=_PATCH_JS+"\n"+text
    text=text.replace("$('fundChart').innerHTML=revenueBarSvgCore(r.series||[]);","$('fundChart').innerHTML=revenueBarSvgVerified(r);")
    return text
run_v551._patched_app_js=_js555

_old_index=run_v551._patched_index
def _index555():return _old_index().replace("5.5.4","5.5.5").replace("5.5.3","5.5.5").replace("5.5.1","5.5.5")
run_v551._patched_index=_index555
_old_sw=run_v551._patched_sw
def _sw555():return _old_sw().replace("ai-stock-v5.5.4","ai-stock-v5.5.5").replace("ai-stock-v5.5.1","ai-stock-v5.5.5")
run_v551._patched_sw=_sw555

@server.app.middleware("http")
async def v555_runtime(request:Request,call_next):
    if request.url.path=="/health":return JSONResponse({"status":"ok","version":VERSION,"mode":"verified-series-repair","revenue_yoy_coverage_required":12,"twse_margin_history_fallback":True},headers={"Cache-Control":"no-store"})
    response=await call_next(request);response.headers["X-AI-Stock-Version"]=VERSION;return response

app=server.app
