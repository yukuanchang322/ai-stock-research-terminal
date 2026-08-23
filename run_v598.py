"""V5.9.8 Financial Diagnostics + Resilient Recovery.

Goals:
- Diagnose TWSE financial feeds explicitly instead of silently returning "missing".
- Parse official income-statement rows without depending on one fixed schema.
- Use TWSE EPS registry as a conservative partial fallback when the detailed
  statement feed is temporarily unavailable.
- Keep T86 / margin / price fixes from V5.9.7.
- Make /health the single visible runtime version source.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

import run_v597, run_v596, run_v594, server

VERSION = "5.9.8"
server.app.version = VERSION

INCOME_PATHS = [
    "/opendata/t187ap06_L_ci",
    "/opendata/t187ap06_L_mim",
    "/opendata/t187ap06_L_basi",
    "/opendata/t187ap06_L_bd",
    "/opendata/t187ap06_L_fh",
    "/opendata/t187ap06_L_ins",
    "/opendata/t187ap06_X_ci",
    "/opendata/t187ap06_X_mim",
    "/opendata/t187ap06_X_basi",
    "/opendata/t187ap06_X_bd",
    "/opendata/t187ap06_X_fh",
    "/opendata/t187ap06_X_ins",
]
EPS_PATHS = ["/opendata/t187ap14_L"]


def _norm(x: Any) -> str:
    return re.sub(r"[\s　_()（）\-]", "", str(x or "")).lower()


def _num(x: Any):
    return server.parse_num_text(x)


def _find_value(row: dict[str, Any], aliases: tuple[str, ...], *, excludes: tuple[str, ...]=()):
    # exact normalized alias first
    norm_map={_norm(k):v for k,v in (row or {}).items()}
    for a in aliases:
        na=_norm(a)
        if na in norm_map and norm_map[na] not in (None, ""):
            return norm_map[na]
    # semantic contains fallback
    for k,v in (row or {}).items():
        nk=_norm(k)
        if any(_norm(a) in nk for a in aliases) and not any(_norm(e) in nk for e in excludes):
            if v not in (None, ""):
                return v
    return None


def _row_code(row: dict[str, Any]) -> str:
    raw=_find_value(row,("公司代號","公司代碼","證券代號","股票代號","companycode","securitiescompanycode","stockid","code"))
    if raw is None:
        # Safe fallback: inspect values only when a key itself mentions code/代號.
        for k,v in (row or {}).items():
            if any(t in _norm(k) for t in ("代號","代碼","code","stockid")):
                raw=v; break
    s=str(raw or "").strip()
    m=re.search(r"\b(\d{4,6})\b",s)
    return m.group(1) if m else re.sub(r"\D","",s)


def _period(row: dict[str, Any]):
    yr=_find_value(row,("年度","year","fiscalyear"))
    q=_find_value(row,("季別","季度","quarter","fiscalquarter"))
    dt=_find_value(row,("出表日期","資料日期","statementdate","date"))
    y=None; qq=None
    try:
        y=int(float(str(yr).strip())); y=y+1911 if y<1911 else y
    except Exception: pass
    mq=re.search(r"([1-4])",str(q or ""))
    if mq: qq=int(mq.group(1))
    p=f"{y} Q{qq}" if y and qq else None
    return y,qq,p,server.roc_date_to_iso(dt)


def _parse_income_row(row: dict[str, Any], ticker: str, path: str):
    if _row_code(row) != str(ticker):
        return None
    y,q,period,statement_date=_period(row)
    eps=_num(_find_value(row,("基本每股盈餘(元)","基本每股盈餘","每股盈餘","basicearningspershare","eps"),excludes=("稀釋",)))
    revenue=_num(_find_value(row,("營業收入","收入合計","operatingrevenue","revenue"),excludes=("百分比","比率")))
    gross=_num(_find_value(row,("營業毛利","營業毛利毛損","grossprofit"),excludes=("百分比","比率")))
    op=_num(_find_value(row,("營業利益","營業利益損失","operatingincome"),excludes=("百分比","比率")))
    net=_num(_find_value(row,("本期淨利","本期淨利淨損","稅後淨利","netincome","profitloss"),excludes=("百分比","比率","每股")))
    completeness=sum(x is not None for x in (eps,revenue,gross,op,net))
    # A row that matched the ticker but contains no usable financial metric is diagnostic only.
    if completeness == 0:
        return {"_unparsed":True,"company_code":str(ticker),"period":period,"raw_keys":list(row.keys())[:60],"endpoint":path}
    return {
        "source":"TWSE OpenAPI official income statement",
        "endpoint":path,
        "official":True,
        "company_code":str(ticker),
        "fiscal_year":y,"fiscal_quarter":q,"period":period or statement_date or "latest",
        "statement_date":statement_date,
        "ytd_eps":eps,"revenue_ytd":revenue,"gross_profit_ytd":gross,
        "operating_income_ytd":op,"net_income_ytd":net,
        "completeness":completeness,"raw_keys":list(row.keys())[:60],
        "feed_kind":"detail",
    }


def _parse_eps_row(row: dict[str, Any], ticker: str, path: str):
    if _row_code(row) != str(ticker): return None
    y,q,period,statement_date=_period(row)
    eps=_num(_find_value(row,("基本每股盈餘","每股盈餘","eps"),excludes=("稀釋",)))
    if eps is None: return None
    return {"source":"TWSE OpenAPI EPS registry","endpoint":path,"official":True,"company_code":str(ticker),
            "fiscal_year":y,"fiscal_quarter":q,"period":period or statement_date or "latest",
            "statement_date":statement_date,"ytd_eps":eps,"completeness":1,"feed_kind":"summary",
            "raw_keys":list(row.keys())[:60]}


def _sort_key(s: dict[str,Any]):
    return (int(s.get("fiscal_year") or 0),int(s.get("fiscal_quarter") or 0),int(s.get("completeness") or 0))


async def _probe_paths(ticker: str, paths: list[str], parser):
    hits=[]; diag=[]
    for path in paths:
        try:
            rows=await asyncio.wait_for(server.openapi_json(server.TWSE_OPENAPI,path),timeout=14)
            matched=[]
            for row in rows:
                if _row_code(row)==str(ticker):
                    parsed=parser(row,ticker,path)
                    if parsed: matched.append(parsed)
            diag.append({"path":path,"http":"ok","rows":len(rows),"ticker_rows":len(matched),
                         "sample_keys":list(rows[0].keys())[:25] if rows else []})
            hits.extend([x for x in matched if not x.get("_unparsed")])
            for x in matched:
                if x.get("_unparsed"):
                    diag[-1]["matched_but_unparsed_keys"]=x.get("raw_keys")
            # For a listed stock, one successful detailed schema is enough; avoid serial latency.
            if hits and path.startswith("/opendata/t187ap06_L_"):
                break
        except Exception as e:
            diag.append({"path":path,"http":"error","error":f"{type(e).__name__}: {str(e)[:160]}"})
    hits.sort(key=_sort_key,reverse=True)
    return (hits[0] if hits else None),diag


async def _recover_financial_v598(ticker: str,d:dict):
    diag=d.setdefault("pipeline_diagnostics",{})
    snap,probe=await _probe_paths(ticker,INCOME_PATHS,_parse_income_row)
    diag["financial_probe_v598"]=probe
    fallback_kind=None
    if not snap:
        eps_snap,eps_probe=await _probe_paths(ticker,EPS_PATHS,_parse_eps_row)
        diag["eps_probe_v598"]=eps_probe
        snap=eps_snap; fallback_kind="eps_registry" if snap else None
    if not snap:
        diag["financial_v598_status"]="no_official_match"
        return d

    # Preserve exact official snapshot; build a conservative display payload even if only EPS exists.
    try:
        eps_stack=await asyncio.wait_for(server.build_eps_stack(ticker,[],snap,{}),timeout=12)
    except Exception:
        eps_stack={"ytd_eps":snap.get("ytd_eps"),"quarter_period":snap.get("period"),"source":snap.get("source")}
    try:
        integrity=server.assess_financial_integrity(snap,eps_stack,datetime.now().date())
    except Exception:
        integrity={}
    fin={
        "statement_date":snap.get("statement_date") or snap.get("period"),"period":snap.get("period"),
        "source":snap.get("source"),"official":True,"partial":fallback_kind=="eps_registry",
        "ytd_eps":snap.get("ytd_eps"),"quarter_eps":eps_stack.get("quarter_eps"),"ttm_eps":eps_stack.get("ttm_eps"),
        "revenue":snap.get("revenue_ytd"),"gross_profit":snap.get("gross_profit_ytd"),
        "operating_income":snap.get("operating_income_ytd"),"net_income":snap.get("net_income_ytd"),
    }
    rev=fin.get("revenue")
    if rev not in (None,0):
        if fin.get("gross_profit") is not None: fin["gross_margin"]=fin["gross_profit"]/rev*100
        if fin.get("operating_income") is not None: fin["operating_margin"]=fin["operating_income"]/rev*100
        if fin.get("net_income") is not None: fin["net_margin"]=fin["net_income"]/rev*100
    d["official_financial"]=snap; d["eps_stack"]=eps_stack; d["financial_integrity"]=integrity; d["financial"]=fin
    diag["financial_v598_status"]="partial_eps_only" if fallback_kind else "ok"
    diag["financial_v598_period"]=snap.get("period")
    diag["financial_v598_endpoint"]=snap.get("endpoint")
    try: d["valuation"]=server.model_valuation(d.get("price"),d.get("per") or {},eps_stack,d.get("research") or {},integrity)
    except Exception: pass
    return d


async def build_stock_v598(ticker:str,force_refresh:bool=False):
    d=await run_v597.build_stock_v597(ticker,force_refresh=force_refresh)
    if not (d.get("financial") or d.get("official_financial")):
        try:
            d=await asyncio.wait_for(_recover_financial_v598(ticker,d),timeout=70)
        except Exception as e:
            d.setdefault("pipeline_diagnostics",{})["financial_v598_fatal"]=f"{type(e).__name__}: {str(e)[:180]}"
    d=run_v596._sync_financial_status(d)
    # Do not present EPS-only fallback as a full financial statement.
    fin=d.get("financial") if isinstance(d.get("financial"),dict) else {}
    if fin.get("partial"):
        for row in d.get("source_status") or []:
            if row.get("name")=="財務報表":
                row.update({"status":"partial","as_of":fin.get("period"),"dataset":"TWSE EPS registry (財報明細暫缺)"})
    try:
        d["scores"]=server.scores(d.get("technical") or {},d.get("revenue") or {},d.get("flow") or {},d.get("per") or {},d.get("financial") or {},d.get("research") or {})
    except Exception: pass
    try: d=run_v594._reconfidence(d)
    except Exception: pass
    d["version"]=VERSION
    return d


app=server.app
for r in list(app.routes):
    if getattr(r,"path",None) in ("/api/stock/{ticker}","/","/health"):
        try: app.routes.remove(r)
        except ValueError: pass

@app.get("/api/stock/{ticker}")
async def api_stock_v598(ticker:str,refresh:int=0):
    return await build_stock_v598(ticker.strip(),force_refresh=bool(refresh))

@app.get("/api/v598/stock/{ticker}")
async def api_v598(ticker:str,refresh:int=0):
    return await build_stock_v598(ticker.strip(),force_refresh=bool(refresh))

@app.get("/api/v598/financial-diagnostics/{ticker}")
async def financial_diagnostics_v598(ticker:str):
    snap,probe=await _probe_paths(ticker.strip(),INCOME_PATHS,_parse_income_row)
    eps,eps_probe=await _probe_paths(ticker.strip(),EPS_PATHS,_parse_eps_row)
    return {"version":VERSION,"ticker":ticker.strip(),"income_match":snap,"income_probe":probe,
            "eps_match":eps,"eps_probe":eps_probe}

@app.get("/health")
async def health_v598():
    return JSONResponse({"status":"ok","version":VERSION,"mode":"financial-diagnostics-resilient-parser",
                         "financial":"TWSE income schema probe + EPS registry fallback","institutional":"TWSE T86 recovered"},
                        headers={"Cache-Control":"no-store"})

@app.get("/",response_class=HTMLResponse)
async def root_v598():
    text=(server.ROOT/"index.html").read_text(encoding="utf-8")
    # Remove every static app-version marker; /health is the sole visible source.
    text=re.sub(r'<span[^>]*data-app-version[^>]*>.*?</span>','',text,flags=re.S|re.I)
    text=re.sub(r'AI Stock Research Terminal V5\.\d+(?:\.\d+)?',f'AI Stock Research Terminal V{VERSION}',text)
    for asset in ("styles.css","app.js","recovery.js","v547_hotfix.js"):
        text=re.sub(rf'{re.escape(asset)}(?:\?v=[^\"\']+)?',f'{asset}?v={VERSION}',text)
    text=re.sub(r'/sw\.js(?:\?v=[^\"\']+)?',f'/sw.js?v={VERSION}',text)
    # Hide any legacy version token accidentally left as a standalone status sibling.
    cleanup="""<script id='v598-version-cleanup'>
    (()=>{const clean=()=>{document.querySelectorAll('[data-app-version]').forEach(n=>n.remove());
      const s=document.querySelector('.cloud-status'); if(!s)return;
      [...s.children].forEach(n=>{if(n.id!=='cloudStatus'&&/^V5\\.\\d+\\.\\d+$/.test((n.textContent||'').trim()))n.remove();});};
      clean(); new MutationObserver(clean).observe(document.body,{subtree:true,childList:true});})();
    </script>"""
    text=text.replace("</body>",cleanup+"</body>")
    return HTMLResponse(text,headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0","Pragma":"no-cache","Expires":"0","X-App-Version":VERSION})

@app.middleware("http")
async def runtime_v598(request:Request,call_next):
    resp=await call_next(request); resp.headers["X-AI-Stock-Version"]=VERSION; return resp
