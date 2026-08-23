"""V5.9.5 Institutional + frontend integrity hardening.
- Robust TWSE T86 OpenAPI/daily parsing by semantic field matching.
- Removes duplicate frontend version marker from served HTML.
- Keeps financial/margin recovery from V5.9.4 and recomputes scores/confidence.
"""
from __future__ import annotations

from datetime import date, timedelta
import asyncio
import re
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

import run_v594, run_v590, server

VERSION = "5.9.5"
server.app.version = VERSION
num = run_v590.num
get_json = run_v590.get_json
rows_from_rwd = run_v590.rows_from_rwd
merge_cf = run_v590.merge_cf


def _norm(s):
    return str(s or "").replace(" ", "").replace("　", "").strip()


def _ticker_match(row, ticker):
    target = str(ticker).strip()
    for k, v in (row or {}).items():
        ks = _norm(k).lower()
        if any(x in ks for x in ("證券代號", "股票代號", "code", "stock_id", "stockno")):
            if _norm(v).replace(".0", "") == target:
                return True
    # Some OpenAPI payloads change the code column name; exact value fallback is safe for 4-digit tickers.
    return any(_norm(v).replace(".0", "") == target for v in (row or {}).values())


def _find_num(row, include, exclude=()):
    for k, v in (row or {}).items():
        ks = _norm(k)
        if all(x in ks for x in include) and not any(x in ks for x in exclude):
            z = num(v)
            if z is not None:
                return z
    return None


def _participant(row, who):
    if who == "foreign":
        tokens = [("外陸資",), ("外資及陸資",), ("外資",)]
        excludes = ("自營商",)
    elif who == "trust":
        tokens = [("投信",)]
        excludes = ()
    else:
        tokens = [("自營商",)]
        excludes = ()

    def find(kind):
        for tk in tokens:
            v = _find_num(row, tk + (kind,), excludes)
            if v is not None:
                return v
        return None

    net = find("買賣超")
    buy = find("買進")
    sell = find("賣出")

    if who == "dealer" and (buy is None or sell is None):
        b1 = _find_num(row, ("自營商", "買進", "自行買賣"))
        b2 = _find_num(row, ("自營商", "買進", "避險"))
        s1 = _find_num(row, ("自營商", "賣出", "自行買賣"))
        s2 = _find_num(row, ("自營商", "賣出", "避險"))
        if buy is None and (b1 is not None or b2 is not None): buy = (b1 or 0) + (b2 or 0)
        if sell is None and (s1 is not None or s2 is not None): sell = (s1 or 0) + (s2 or 0)
    if net is None and buy is not None and sell is not None:
        net = buy - sell
    return buy, sell, net


def _build_inst(row, px):
    inst = {}
    for who in ("foreign", "trust", "dealer"):
        buy, sell, net = _participant(row, who)
        if net is None:
            continue
        inst[who] = {"1": {
            "buy": buy * px if buy is not None and px else None,
            "sell": sell * px if sell is not None and px else None,
            "net": net * px if px else net,
            "shares_net": net,
            "days": 1,
        }}
    return inst


async def _openapi_t86(ticker, d):
    j = await get_json("https://openapi.twse.com.tw/v1/fund/T86", timeout=10)
    rows = j if isinstance(j, list) else []
    row = next((r for r in rows if _ticker_match(r, ticker)), None)
    if not row:
        return None, {"source":"openapi","status":"ticker_not_found","rows":len(rows)}
    inst = _build_inst(row, num(d.get("price")))
    if not inst:
        return None, {"source":"openapi","status":"fields_unmatched","rows":len(rows),"keys":list(row.keys())[:30]}
    asof = str(d.get("price_as_of") or date.today().isoformat())[:10]
    return {"institutional":inst,"institutional_rows":1,"institutional_source":"TWSE OpenAPI T86 official","last_date":asof}, {"source":"openapi","status":"ok","rows":len(rows),"date":asof}


async def _daily_t86(ticker, d):
    try:
        end = date.fromisoformat(str(d.get("price_as_of") or date.today().isoformat())[:10])
    except Exception:
        end = date.today()
    px = num(d.get("price")); attempts=[]
    for back in range(0, 12):
        dt=end-timedelta(days=back)
        if dt.weekday()>=5: continue
        try:
            j=await get_json("https://www.twse.com.tw/rwd/zh/fund/T86",{"date":dt.strftime("%Y%m%d"),"selectType":"ALL","response":"json"},timeout=8)
            rows=rows_from_rwd(j)
            row=next((r for r in rows if _ticker_match(r,ticker)),None)
            attempts.append({"date":dt.isoformat(),"rows":len(rows),"found":bool(row)})
            if not row: continue
            inst=_build_inst(row,px)
            if not inst:
                attempts[-1]["keys"]=list(row.keys())[:30]
                continue
            return {"institutional":inst,"institutional_rows":1,"institutional_source":"TWSE T86 official daily","last_date":dt.isoformat()}, attempts
        except Exception as e:
            attempts.append({"date":dt.isoformat(),"error":type(e).__name__})
    return None, attempts


async def build_stock_v595(ticker: str, force_refresh: bool=False):
    d=await run_v594.build_stock_v594(ticker, force_refresh=force_refresh)
    d["version"]=VERSION
    cf=d.get("cashflow") if isinstance(d.get("cashflow"),dict) else {}
    if not cf.get("institutional"):
        diag=d.setdefault("pipeline_diagnostics",{})
        extra=None
        try:
            extra,meta=await asyncio.wait_for(_openapi_t86(ticker,d),timeout=12)
            diag["t86_openapi_v595"]=meta
        except Exception as e:
            diag["t86_openapi_v595"]={"status":"error","error":f"{type(e).__name__}: {str(e)[:140]}"}
        if not extra:
            try:
                extra,meta=await asyncio.wait_for(_daily_t86(ticker,d),timeout=24)
                diag["t86_daily_v595"]=meta
            except Exception as e:
                diag["t86_daily_v595"]={"status":"error","error":f"{type(e).__name__}: {str(e)[:140]}"}
        if extra:
            d=merge_cf(d,extra)

    cf=d.get("cashflow") if isinstance(d.get("cashflow"),dict) else {}
    for row in d.get("source_status") or []:
        if row.get("name")=="三大法人":
            row.update({"status":"ok" if cf.get("institutional") else "missing","as_of":cf.get("last_date"),"dataset":cf.get("institutional_source") or "TWSE T86"})
    try:
        d["scores"]=server.scores(d.get("technical") or {},d.get("revenue") or {},d.get("flow") or {},d.get("per") or {},d.get("financial") or {},d.get("research") or {})
    except Exception:
        pass
    try:
        d=run_v594._reconfidence(d)
    except Exception:
        pass
    d["version"]=VERSION
    return d


app=server.app
for r in list(app.routes):
    if getattr(r,"path",None) in ("/api/stock/{ticker}","/"):
        try: app.routes.remove(r)
        except ValueError: pass

@app.get("/api/stock/{ticker}")
async def api_stock_v595(ticker:str,refresh:int=0):
    return await build_stock_v595(ticker.strip(),force_refresh=bool(refresh))

@app.get("/api/v595/stock/{ticker}")
async def api_v595(ticker:str,refresh:int=0):
    return await build_stock_v595(ticker.strip(),force_refresh=bool(refresh))

@app.get("/",response_class=HTMLResponse)
async def root_v595():
    text=(server.ROOT/"index.html").read_text(encoding="utf-8")
    text=re.sub(r"AI Stock Research Terminal V5\.\d+(?:\.\d+)?",f"AI Stock Research Terminal V{VERSION}",text)
    for asset in ("styles.css","app.js","recovery.js","v547_hotfix.js"):
        text=re.sub(rf"{re.escape(asset)}(?:\?v=[^\"']+)?",f"{asset}?v={VERSION}",text)
    text=re.sub(r'<span class="status-sep">•</span>\s*<span data-app-version>.*?</span>', '', text, flags=re.S)
    text=re.sub(r'<span data-app-version>.*?</span>\s*<span class="status-sep">•</span>', '', text, flags=re.S)
    patch='''<style id="v595-mobile-fix">@media(max-width:900px){html,body{width:100%;max-width:100%;overflow-x:hidden!important}main{width:100%;max-width:100%;overflow:hidden;padding-left:10px!important;padding-right:10px!important}.report,.hero,.search-card,.section,.panel,.summary-grid,.report-head{width:100%;max-width:100%;min-width:0!important}.freshness-strip,.kpi-grid{display:grid!important;grid-template-columns:repeat(2,minmax(0,1fr))!important;width:100%!important;max-width:100%!important;min-width:0!important;overflow:hidden!important}.fresh,.kpi{min-width:0!important;max-width:100%!important;width:auto!important;overflow:hidden!important;padding:12px 9px!important}.fresh b,.fresh span,.kpi b,.kpi span{white-space:normal!important;overflow-wrap:anywhere!important;word-break:break-word}.cloud-status{flex-wrap:wrap!important}}</style>'''
    text=text.replace("</head>",patch+"</head>")
    return HTMLResponse(text,headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0","Pragma":"no-cache","Expires":"0","X-App-Version":VERSION})

@app.middleware("http")
async def v595_runtime(request:Request,call_next):
    if request.url.path=="/health":
        return JSONResponse({"status":"ok","version":VERSION,"mode":"institutional-integrity","t86":"OpenAPI + nearest daily semantic parser","mobile_overflow_fix":True},headers={"Cache-Control":"no-store"})
    resp=await call_next(request);resp.headers["X-AI-Stock-Version"]=VERSION;return resp
