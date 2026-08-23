"""V5.5.3 Cash Flow + Margin Change.

Institutional section is converted from share-count net flow to estimated buy/sell/net
cash amounts (daily official T86 share counts × each day's close). Margin/short balance
changes are shown separately for 1/5/20 trading-day windows.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

import run_v552
import run_v551
import server

VERSION="5.5.3"
server.app.version=VERSION
_base_build_stock=server.build_stock


def _num(v: Any):
    try:
        if v is None: return None
        return float(str(v).replace(",","").replace("--","").strip())
    except Exception:
        return None


def _row_dict(payload: dict[str,Any], ticker: str):
    fields=payload.get("fields") or []
    for row in payload.get("data") or []:
        d=dict(zip(fields,row))
        code=str(d.get("證券代號") or d.get("股票代號") or d.get("代號") or "").strip()
        if code==ticker: return d
    for tab in payload.get("tables") or []:
        fields=tab.get("fields") or []
        for row in tab.get("data") or []:
            d=dict(zip(fields,row))
            code=str(d.get("證券代號") or d.get("股票代號") or d.get("代號") or "").strip()
            if code==ticker: return d
    return None


def _pick(d: dict[str,Any] | None, includes: tuple[str,...], excludes: tuple[str,...]=()):
    if not d: return None
    for k,v in d.items():
        ks=str(k).replace(" ","")
        if all(x in ks for x in includes) and not any(x in ks for x in excludes):
            n=_num(v)
            if n is not None: return n
    return None


async def _json(client: httpx.AsyncClient, url: str, params: dict[str,Any]):
    try:
        r=await client.get(url,params=params,timeout=12)
        if r.status_code>=400: return None
        x=r.json()
        return x if isinstance(x,dict) else None
    except Exception:
        return None


async def _t86_day(client: httpx.AsyncClient,ticker:str,date_iso:str,close:float):
    day=date_iso.replace("-","")
    p=await _json(client,"https://www.twse.com.tw/rwd/zh/fund/T86",{"date":day,"selectType":"ALLBUT0999","response":"json"})
    d=_row_dict(p or {},ticker)
    if not d: return None
    fb=_pick(d,("外陸資","買進股數"),()) or _pick(d,("外資","買進股數"),())
    fs=_pick(d,("外陸資","賣出股數"),()) or _pick(d,("外資","賣出股數"),())
    tb=_pick(d,("投信","買進股數"),())
    ts=_pick(d,("投信","賣出股數"),())
    db1=_pick(d,("自營商","買進股數","自行買賣"),()) or 0
    ds1=_pick(d,("自營商","賣出股數","自行買賣"),()) or 0
    db2=_pick(d,("自營商","買進股數","避險"),()) or 0
    ds2=_pick(d,("自營商","賣出股數","避險"),()) or 0
    out={"date":date_iso,"close":close}
    for key,b,s in (("foreign",fb,fs),("trust",tb,ts),("dealer",db1+db2,ds1+ds2)):
        if b is None or s is None: continue
        out[key]={"buy":b*close,"sell":s*close,"net":(b-s)*close}
    return out


async def _margin_day(client:httpx.AsyncClient,ticker:str,date_iso:str):
    day=date_iso.replace("-","")
    p=await _json(client,"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",{"date":day,"selectType":"MS","response":"json"})
    d=_row_dict(p or {},ticker)
    if not d: return None
    mb=_pick(d,("融資","今日餘額"),())
    sb=_pick(d,("融券","今日餘額"),())
    return {"date":date_iso,"margin_balance":mb,"short_balance":sb}


def _sum_window(rows:list[dict[str,Any]],n:int,key:str):
    part=rows[-n:] if len(rows)>=n else rows
    buy=sum((x.get(key) or {}).get("buy",0) for x in part)
    sell=sum((x.get(key) or {}).get("sell",0) for x in part)
    return {"buy":buy,"sell":sell,"net":buy-sell,"days":len(part)} if part else None


def _pct_change(now,old):
    return None if now is None or old in (None,0) else (now/old-1)*100


async def _cashflow(ticker:str,d:dict[str,Any]):
    series=[x for x in (d.get("technical") or {}).get("series",[]) if x.get("date") and _num(x.get("close")) is not None]
    if not series: return {}
    series=series[-21:]
    async with httpx.AsyncClient(follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.5.3"}) as client:
        t86=await asyncio.gather(*(_t86_day(client,ticker,str(x["date"]),float(x["close"])) for x in series[-20:]))
        idxs=sorted(set([0,max(0,len(series)-21),max(0,len(series)-6),max(0,len(series)-2),len(series)-1]))
        mrows=await asyncio.gather(*(_margin_day(client,ticker,str(series[i]["date"])) for i in idxs))
    rows=[x for x in t86 if x]
    institutional={}
    for key in ("foreign","trust","dealer"):
        institutional[key]={str(n):_sum_window(rows,n,key) for n in (1,5,20)}
    mm={x["date"]:x for x in mrows if x}
    latest_date=str(series[-1]["date"]); latest=mm.get(latest_date)
    changes={}
    if latest:
        for n,idx in ((1,-2),(5,-6),(20,-21)):
            if len(series)>=abs(idx):
                base=mm.get(str(series[idx]["date"]))
                if base:
                    changes[str(n)]={
                        "margin_pct":_pct_change(latest.get("margin_balance"),base.get("margin_balance")),
                        "short_pct":_pct_change(latest.get("short_balance"),base.get("short_balance")),
                        "margin_balance":latest.get("margin_balance"),"short_balance":latest.get("short_balance")}
    return {"institutional":institutional,"margin_short":changes,"as_of":latest_date,
            "amount_method":"estimated_cash_amount","amount_note":"官方 T86 買賣股數 × 每日收盤價換算；為估算成交金額，非交易所逐筆成交金額。",
            "currency":"TWD"}


async def build_stock_v553(ticker:str,force_refresh:bool=False):
    d=await _base_build_stock(ticker,force_refresh=force_refresh)
    if isinstance(d,dict):
        try: d["cashflow"]=await _cashflow(ticker,d)
        except Exception: d["cashflow"]={}
        d["version"]=VERSION
        d["data_policy"]=(d.get("data_policy") or "")+" V5.5.3：籌碼改看法人估算買進/賣出/淨額金額；融資與融券餘額變化獨立顯示 1/5/20 日。"
    return d

server.build_stock=build_stock_v553

# Patch the core JS generator from V5.5.1. V5.5.1 middleware calls this function at request time.
_old_js=run_v551._patched_app_js
_JS=r'''
function moneyYi(v){if(v==null||!Number.isFinite(Number(v)))return '—';const n=Number(v)/1e8;return `${n>0?'+':''}${n.toFixed(Math.abs(n)>=100?0:Math.abs(n)>=10?1:2)}億`;}
function pctCell(v,reverse=false){if(v==null||!Number.isFinite(Number(v)))return '<span>—</span>';const n=Number(v),bad=reverse?n>0:n<0;return `<span class="${bad?'neg':'pos'}">${n>0?'+':''}${n.toFixed(1)}%</span>`;}
function flowCashMatrix(cf,fl){
 const inst=cf?.institutional||{};const labels=[['外資','foreign'],['投信','trust'],['自營商','dealer']];
 const block=(kind,label)=>`<div class="cash-subhead">${label}</div><div class="flow-matrix cash-matrix"><div class="flow-head"><b>法人</b><b>1日</b><b>5日</b><b>20日</b></div>${labels.map(([name,key])=>`<div class="flow-matrix-row"><b>${name}</b>${[1,5,20].map(n=>{const v=inst?.[key]?.[String(n)]?.[kind];return `<span class="${Number(v)<0?'neg':'pos'}">${moneyYi(v)}</span>`}).join('')}</div>`).join('')}</div>`;
 const ms=cf?.margin_short||{};
 const margin=`<div class="cash-subhead margin-title">融資融券餘額變化</div><div class="flow-matrix"><div class="flow-head"><b>項目</b><b>1日</b><b>5日</b><b>20日</b></div><div class="flow-matrix-row"><b>融資</b>${[1,5,20].map(n=>pctCell(ms?.[String(n)]?.margin_pct,true)).join('')}</div><div class="flow-matrix-row"><b>融券</b>${[1,5,20].map(n=>pctCell(ms?.[String(n)]?.short_pct,false)).join('')}</div></div>`;
 return `<div class="cashflow-stack"><div class="cash-note">法人估算成交金額（億元）｜買進、賣出與淨額分開顯示</div>${block('buy','買進金額')}${block('sell','賣出金額')}${block('net','淨買賣金額')}${margin}<small class="chart-note">${cf?.amount_note||'金額依官方買賣股數與每日收盤價估算。'}</small></div>`;
}
'''
def _js553():
    text=_old_js()
    if "function flowCashMatrix" not in text: text=_JS+"\n"+text
    text=text.replace("$('flowTable').innerHTML=flowMatrix(fl);","$('flowTable').innerHTML=flowCashMatrix(d.cashflow||{},fl);")
    return text
run_v551._patched_app_js=_js553
run_v551.CORE_CSS += r'''
.cashflow-stack{display:grid;gap:10px;width:100%}.cash-note,.cash-subhead{color:var(--muted);font-size:10px}.cash-subhead{font-weight:800;color:var(--text);margin-top:4px}.margin-title{margin-top:10px}.cash-matrix{margin-bottom:2px}.cashflow-stack .flow-matrix-row span{font-size:9.5px}.cashflow-stack .flow-head,.cashflow-stack .flow-matrix-row{grid-template-columns:46px repeat(3,minmax(0,1fr))!important}@media(max-width:600px){.cashflow-stack .flow-head,.cashflow-stack .flow-matrix-row{grid-template-columns:42px repeat(3,minmax(0,1fr))!important}.cashflow-stack .flow-matrix-row span{font-size:9px}}
'''
_old_index=run_v551._patched_index
def _index553():
    return _old_index().replace("5.5.1","5.5.3")
run_v551._patched_index=_index553

@server.app.middleware("http")
async def v553_runtime(request:Request,call_next):
    if request.url.path=="/health":
        return JSONResponse({"status":"ok","version":VERSION,"mode":"institutional-cashflow+margin-short-change","cash_amounts":True,"margin_short_1_5_20":True,"pwa":True},headers={"Cache-Control":"no-store"})
    response=await call_next(request);response.headers["X-AI-Stock-Version"]=VERSION;return response

app=server.app
