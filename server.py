from __future__ import annotations

import asyncio
import html
import json
import math
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import quote_plus
import re
import xml.etree.ElementTree as ET
from lxml import html as lxml_html
import csv
import io
from urllib.parse import urljoin
from email.utils import parsedate_to_datetime

import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "generated_reports"
DATA_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)

FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "").strip()
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "600"))
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

app = FastAPI(title="AI Stock Research Terminal", version="5.3.5")
app.add_middleware(GZipMiddleware, minimum_size=800)
app.mount("/static", StaticFiles(directory=ROOT), name="static")


@app.middleware("http")
async def prevent_stale_pwa_shell(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path in {"/", "/index.html", "/app.js", "/styles.css", "/sw.js", "/static/manifest.webmanifest"}:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    if path == "/sw.js":
        response.headers["Service-Worker-Allowed"] = "/"
    return response


def safe_num(v: Any, default: float | None = None) -> float | None:
    try:
        if v in (None, "", "--", "—"):
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


FULLWIDTH_TICKER_TRANSLATION=str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ", "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def normalize_ticker(value: Any) -> str:
    """Normalize iPhone/Chinese-IME full-width ticker input to the API's canonical form."""
    return str(value or "").translate(FULLWIDTH_TICKER_TRANSLATION).strip().upper()


def require_numeric_taiwan_ticker(value: Any) -> str:
    ticker=normalize_ticker(value)
    if not re.fullmatch(r"\d{4,6}",ticker):
        raise HTTPException(400,"請輸入有效台股代號")
    return ticker


def pct(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v:+.{digits}f}%"


def nfmt(v: float | int | None, digits: int = 1) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.{digits}f}"


async def finmind(dataset: str, ticker: str | None = None, start: date | None = None, end: date | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"dataset": dataset}
    if ticker:
        params["data_id"] = ticker
    if start:
        params["start_date"] = start.isoformat()
    if end:
        params["end_date"] = end.isoformat()
    headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"} if FINMIND_TOKEN else {}
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        r = await client.get(FINMIND_BASE, params=params, headers=headers)
        r.raise_for_status()
        payload = r.json()
    if payload.get("status") not in (200, None):
        raise RuntimeError(payload.get("msg") or f"FinMind {dataset} failed")
    return payload.get("data") or []


TWSE_OPENAPI = "https://openapi.twse.com.tw/v1"
TPEX_OPENAPI = "https://www.tpex.org.tw/openapi/v1"
TWSTOCK_MCP_URL = os.getenv("TWSTOCK_MCP_URL", "https://TW-Stock-MCP-Server.fastmcp.app/mcp").strip()
TWSTOCK_MCP_ENABLED = os.getenv("TWSTOCK_MCP_ENABLED", "0").strip().lower() in {"1","true","yes","on"}

PROVIDER_REGISTRY = [
    {"id":"twse","name":"TWSE OpenAPI/Web API","tier":1,"authority":"official","role":"primary","enabled":True},
    {"id":"tpex","name":"TPEx OpenAPI","tier":1,"authority":"official","role":"primary","enabled":True},
    {"id":"company_ir","name":"Company IR / Earnings Release","tier":1,"authority":"company_official","role":"primary","enabled":True},
    {"id":"eps_registry","name":"Official EPS Registry","tier":1,"authority":"verified_registry","role":"historical_backfill","enabled":True},
    {"id":"twstock_mcp","name":"TWStock MCP compatible adapter","tier":2,"authority":"aggregator","role":"shadow_crosscheck","enabled":TWSTOCK_MCP_ENABLED,"url":TWSTOCK_MCP_URL},
    {"id":"finmind","name":"FinMind","tier":3,"authority":"third_party","role":"fallback","enabled":True},
    {"id":"public_web","name":"Public Web Research","tier":4,"authority":"public_web","role":"research_context","enabled":True},
]

def parse_num_text(v: Any) -> float | None:
    if v is None: return None
    s=str(v).strip().replace(",", "").replace("％", "").replace("%", "")
    if not s or re.fullmatch(r"[-—－]+", s): return None
    try: return float(s)
    except Exception: return None

def roc_date_to_iso(v: Any) -> str:
    s=re.sub(r"[^0-9]", "", str(v or ""))
    try:
        if len(s)==7: return f"{int(s[:3])+1911:04d}-{s[3:5]}-{s[5:7]}"
        if len(s)==8: return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    except Exception: pass
    return str(v or "")

def _row_value(row: dict[str, Any], includes: list[str], excludes: list[str] | None=None) -> Any:
    excludes=excludes or []
    for k,v in row.items():
        ks=str(k).replace(" ", "")
        if all(x in ks for x in includes) and not any(x in ks for x in excludes): return v
    return None

async def openapi_json(base: str, path: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.3.2"}) as client:
        r=await client.get(base + path); r.raise_for_status(); data=r.json()
    return data if isinstance(data,list) else []


def _month_starts(end_date: date, count: int = 13) -> list[date]:
    months=[]
    year,month=end_date.year,end_date.month
    for _ in range(count):
        months.append(date(year,month,1))
        month-=1
        if month==0:
            year-=1; month=12
    return list(reversed(months))


async def fetch_twse_stock_day(ticker: str, months: int = 13) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch official TWSE monthly OHLC without failing the full series on one bad month."""
    url="https://www.twse.com.tw/exchangeReport/STOCK_DAY"
    errors=[]
    async with httpx.AsyncClient(timeout=20,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.3.5"}) as client:
        async def fetch_month(month_start: date):
            month_key=month_start.strftime("%Y%m")
            try:
                response=await client.get(url,params={"response":"json","date":month_start.strftime("%Y%m%d"),"stockNo":ticker})
                response.raise_for_status()
                payload=response.json()
                if payload.get("stat") != "OK":
                    raise RuntimeError(str(payload.get("stat") or "TWSE response not OK"))
                rows=[]
                for raw in payload.get("data") or []:
                    if len(raw)<7:
                        continue
                    open_,high,low,close,volume=(parse_num_text(raw[3]),parse_num_text(raw[4]),parse_num_text(raw[5]),parse_num_text(raw[6]),parse_num_text(raw[1]))
                    if None in (open_,high,low,close):
                        continue
                    rows.append({"date":roc_date_to_iso(raw[0]),"open":open_,"max":high,"min":low,"close":close,"Trading_Volume":volume})
                return rows
            except Exception as exc:
                errors.append(f"TWSE_STOCK_DAY {month_key}: {type(exc).__name__}: {exc}")
                return []
        monthly=await asyncio.gather(*(fetch_month(month) for month in _month_starts(date.today(),months)))
    rows=[row for group in monthly for row in group]
    unique={row["date"]:row for row in rows if row.get("date")}
    return [unique[key] for key in sorted(unique)],errors


async def fetch_twse_stock_info(ticker: str) -> tuple[dict[str, Any], list[str]]:
    """Resolve listed-company identity from the official TWSE company OpenAPI."""
    try:
        rows=await openapi_json(TWSE_OPENAPI,"/opendata/t187ap03_L")
        row=next((item for item in rows if str(item.get("公司代號") or "").strip()==ticker),None)
        if not row:
            return {},[]
        return {
            "stock_id":ticker,
            "stock_name":str(row.get("公司簡稱") or row.get("公司名稱") or ticker).strip(),
            "industry_category":_industry_label(row.get("產業別")),
            "type":"上市",
            "source":"TWSE OpenAPI t187ap03_L",
        },[]
    except Exception as exc:
        return {},[f"TWSE_INFO: {type(exc).__name__}: {exc}"]


def _industry_label(value: Any) -> str:
    text=str(value or "").strip()
    if not text:
        return "—"
    return f"產業代碼 {text}" if re.fullmatch(r"\d{2}",text) else text


def _normalize_market_type(value: Any) -> str:
    text=str(value or "").strip()
    lowered=text.lower()
    if lowered in {"twse","sii","上市"}:
        return "上市"
    if lowered in {"tpex","otc","上櫃"}:
        return "上櫃"
    return text or "—"


def normalize_stock_info(info: dict[str, Any], ticker: str) -> dict[str, Any]:
    if not info:
        return {}
    normalized=dict(info)
    normalized["stock_id"]=str(normalized.get("stock_id") or ticker).strip()
    normalized["stock_name"]=str(normalized.get("stock_name") or ticker).strip()
    normalized["industry_category"]=_industry_label(normalized.get("industry_category"))
    normalized["type"]=_normalize_market_type(normalized.get("type"))
    return normalized


async def fetch_tpex_stock_info(ticker: str) -> tuple[dict[str, Any], list[str]]:
    """Resolve OTC-company identity from the official TPEx company OpenAPI."""
    try:
        rows=await openapi_json(TPEX_OPENAPI,"/mopsfin_t187ap03_O")
        row=next((item for item in rows if str(item.get("SecuritiesCompanyCode") or "").strip()==ticker),None)
        if not row:
            return {},[]
        return {
            "stock_id":ticker,
            "stock_name":str(row.get("CompanyAbbreviation") or row.get("CompanyName") or ticker).strip(),
            "industry_category":_industry_label(row.get("SecuritiesIndustryCode")),
            "type":"上櫃",
            "source":"TPEx OpenAPI mopsfin_t187ap03_O",
        },[]
    except Exception as exc:
        return {},[f"TPEX_INFO: {type(exc).__name__}: {exc}"]


async def fetch_official_stock_info(ticker: str) -> tuple[dict[str, Any], list[str]]:
    """Resolve listed or OTC identity without treating the other market as an error."""
    twse_result,tpex_result=await asyncio.gather(fetch_twse_stock_info(ticker),fetch_tpex_stock_info(ticker))
    twse_info,twse_errors=twse_result
    tpex_info,tpex_errors=tpex_result
    if twse_info:
        return twse_info,[*twse_errors,*tpex_errors]
    if tpex_info:
        return tpex_info,[*twse_errors,*tpex_errors]
    return {},[*twse_errors,*tpex_errors,"OFFICIAL_INFO: ticker not found in listed or OTC company data"]


def parse_tpex_stock_day_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize one official TPEx monthly response to calc_technical() columns."""
    if str(payload.get("stat") or "").lower() != "ok":
        raise RuntimeError(str(payload.get("stat") or "TPEx response not OK"))
    tables=payload.get("tables") or []
    if not tables:
        return []
    table=tables[0] or {}
    fields=[re.sub(r"\s+", "", str(field or "")) for field in (table.get("fields") or [])]

    def column(*names: str) -> int | None:
        for name in names:
            if name in fields:
                return fields.index(name)
        return None

    indices={
        "date":column("日期"),
        "volume":column("成交張數","成交股數"),
        "open":column("開盤","開盤價"),
        "high":column("最高","最高價"),
        "low":column("最低","最低價"),
        "close":column("收盤","收盤價"),
    }
    if any(index is None for index in indices.values()):
        raise RuntimeError(f"TPEx fields missing: {fields}")
    volume_is_lots=fields[indices["volume"]] == "成交張數"
    rows=[]
    for raw in table.get("data") or []:
        if not isinstance(raw,list) or max(indices.values()) >= len(raw):
            continue
        open_=parse_num_text(raw[indices["open"]])
        high=parse_num_text(raw[indices["high"]])
        low=parse_num_text(raw[indices["low"]])
        close=parse_num_text(raw[indices["close"]])
        volume=parse_num_text(raw[indices["volume"]])
        if None in (open_,high,low,close):
            continue
        if volume is not None and volume_is_lots:
            volume*=1000
        rows.append({"date":roc_date_to_iso(raw[indices["date"]]),"open":open_,"max":high,"min":low,"close":close,"Trading_Volume":volume})
    return rows


async def fetch_tpex_stock_day(ticker: str, months: int = 13) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch official TPEx monthly OHLC; one failed month never aborts the series."""
    url="https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
    errors=[]
    async with httpx.AsyncClient(timeout=20,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.3.5"}) as client:
        async def fetch_month(month_start: date):
            month_key=month_start.strftime("%Y%m")
            try:
                response=await client.post(url,data={"code":ticker,"date":month_start.strftime("%Y/%m/01"),"id":"","response":"json"})
                response.raise_for_status()
                return parse_tpex_stock_day_payload(response.json())
            except Exception as exc:
                errors.append(f"TPEX_TRADING_STOCK {month_key}: {type(exc).__name__}: {exc}")
                return []
        monthly=await asyncio.gather(*(fetch_month(month) for month in _month_starts(date.today(),months)))
    rows=[row for group in monthly for row in group]
    unique={row["date"]:row for row in rows if row.get("date")}
    return [unique[key] for key in sorted(unique)],errors


async def fetch_official_stock_day(ticker: str, market_type: str = "") -> tuple[list[dict[str, Any]], str | None, list[str]]:
    """Choose the official listed/OTC history provider, with a cross-market fallback."""
    market=_normalize_market_type(market_type)
    providers=[("TPEx afterTrading/tradingStock",fetch_tpex_stock_day),("TWSE STOCK_DAY",fetch_twse_stock_day)] if market=="上櫃" else [("TWSE STOCK_DAY",fetch_twse_stock_day),("TPEx afterTrading/tradingStock",fetch_tpex_stock_day)]
    errors=[]
    for provider,fetcher in providers:
        rows,provider_errors=await fetcher(ticker,13)
        errors.extend(provider_errors)
        if rows:
            return rows,provider,errors
    return [],None,errors


def parse_twse_institutional_payload(payload: dict[str, Any], ticker: str, trading_date: str) -> list[dict[str, Any]]:
    fields=payload.get("fields") or []
    for raw in payload.get("data") or []:
        if not raw or str(raw[0]).strip()!=ticker: continue
        row=dict(zip(fields,raw))
        return [{
            "date":trading_date,"stock_id":ticker,
            "Foreign_Investor_buy":parse_num_text(row.get("外陸資買進股數(不含外資自營商)")),
            "Foreign_Investor_sell":parse_num_text(row.get("外陸資賣出股數(不含外資自營商)")),
            "Foreign_Dealer_Self_buy":parse_num_text(row.get("外資自營商買進股數")),
            "Foreign_Dealer_Self_sell":parse_num_text(row.get("外資自營商賣出股數")),
            "Investment_Trust_buy":parse_num_text(row.get("投信買進股數")),
            "Investment_Trust_sell":parse_num_text(row.get("投信賣出股數")),
            "Dealer_self_buy":parse_num_text(row.get("自營商買進股數(自行買賣)")),
            "Dealer_self_sell":parse_num_text(row.get("自營商賣出股數(自行買賣)")),
            "Dealer_Hedging_buy":parse_num_text(row.get("自營商買進股數(避險)")),
            "Dealer_Hedging_sell":parse_num_text(row.get("自營商賣出股數(避險)")),
        }]
    return []


def parse_twse_margin_payload(payload: dict[str, Any], ticker: str, trading_date: str) -> list[dict[str, Any]]:
    for table in payload.get("tables") or []:
        fields=table.get("fields") or []
        if not fields or fields[0] != "代號": continue
        for raw in table.get("data") or []:
            if not raw or str(raw[0]).strip()!=ticker or len(raw)<16: continue
            return [{
                "date":trading_date,"stock_id":ticker,
                "MarginPurchaseBuy":parse_num_text(raw[2]),"MarginPurchaseSell":parse_num_text(raw[3]),
                "MarginPurchaseCashRepayment":parse_num_text(raw[4]),
                "MarginPurchaseYesterdayBalance":parse_num_text(raw[5]),"MarginPurchaseTodayBalance":parse_num_text(raw[6]),
                "MarginPurchaseLimit":parse_num_text(raw[7]),"ShortSaleBuy":parse_num_text(raw[8]),
                "ShortSaleSell":parse_num_text(raw[9]),"ShortSaleCashRepayment":parse_num_text(raw[10]),
                "ShortSaleYesterdayBalance":parse_num_text(raw[11]),"ShortSaleTodayBalance":parse_num_text(raw[12]),
                "ShortSaleLimit":parse_num_text(raw[13]),"OffsetLoanAndShort":parse_num_text(raw[14]),"Note":raw[15],
            }]
    return []


def parse_twse_revenue_rows(payload: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    item=next((row for row in payload if str(row.get("公司代號") or "").strip()==ticker),None)
    if not item: return []
    ym=re.sub(r"\D","",str(item.get("資料年月") or ""))
    if len(ym)!=5: return []
    year=int(ym[:3])+1911; month=int(ym[3:])
    current=parse_num_text(item.get("營業收入-當月營收"))
    prior=parse_num_text(item.get("營業收入-去年當月營收"))
    rows=[]
    if prior is not None:
        rows.append({"date":f"{year-1:04d}-{month:02d}-01","stock_id":ticker,"revenue_year":year-1,"revenue_month":month,"revenue":prior*1000})
    if current is not None:
        rows.append({"date":f"{year:04d}-{month:02d}-01","stock_id":ticker,"revenue_year":year,"revenue_month":month,"revenue":current*1000})
    return rows


async def fetch_twse_flow_fallback(ticker: str, trading_dates: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Fetch exact-code TWSE flow data for recent known trading dates."""
    errors=[]; institutional=[]; margin=[]
    dates=sorted({str(d)[:10] for d in trading_dates if d},reverse=True)[:24]
    async with httpx.AsyncClient(timeout=20,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.3.5"}) as client:
        async def one(day: str):
            compact=day.replace("-","")
            try:
                inst_r,margin_r=await asyncio.gather(
                    client.get("https://www.twse.com.tw/rwd/zh/fund/T86",params={"date":compact,"selectType":"ALLBUT0999","response":"json"}),
                    client.get("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",params={"date":compact,"selectType":"ALL","response":"json"}),
                )
                inst_r.raise_for_status(); margin_r.raise_for_status()
                return parse_twse_institutional_payload(inst_r.json(),ticker,day),parse_twse_margin_payload(margin_r.json(),ticker,day)
            except Exception as exc:
                errors.append(f"TWSE_FLOW {day}: {type(exc).__name__}"); return [],[]
        groups=await asyncio.gather(*(one(day) for day in dates))
    for inst_rows,margin_rows in groups:
        institutional.extend(inst_rows); margin.extend(margin_rows)
    return sorted(institutional,key=lambda x:x["date"]),sorted(margin,key=lambda x:x["date"]),errors


async def fetch_twse_revenue_fallback(ticker: str) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        payload=await openapi_json(TWSE_OPENAPI,"/opendata/t187ap05_L")
        return parse_twse_revenue_rows(payload,ticker),[]
    except Exception as exc:
        return [],[f"TWSE_REVENUE: {type(exc).__name__}: {exc}"]


def parse_twse_lending_snapshots(short_payload: dict[str, Any], borrow_payload: dict[str, Any], ticker: str, trading_date: str) -> dict[str, Any]:
    """Normalize official share counts to lots for the dashboard."""
    out={"lending_last_date":trading_date}
    short_row=next((raw for raw in short_payload.get("data") or [] if raw and str(raw[0]).strip()==ticker),None)
    if short_row and len(short_row)>=14:
        values=[parse_num_text(v) for v in short_row]
        out.update({
            "sbl_short_previous":values[8]/1000 if values[8] is not None else None,
            "sbl_short_sell":values[9]/1000 if values[9] is not None else None,
            "sbl_short_return":values[10]/1000 if values[10] is not None else None,
            "sbl_short_adjustment":values[11]/1000 if values[11] is not None else None,
            "sbl_short_balance":values[12]/1000 if values[12] is not None else None,
            "sbl_short_limit":values[13]/1000 if values[13] is not None else None,
        })
    borrow_row=next((raw for raw in borrow_payload.get("data") or [] if raw and str(raw[0]).strip()==ticker),None)
    if borrow_row and len(borrow_row)>=8:
        values=[parse_num_text(v) for v in borrow_row]
        out.update({
            "sbl_borrow_previous":values[2]/1000 if values[2] is not None else None,
            "sbl_borrow":values[3]/1000 if values[3] is not None else None,
            "sbl_return":values[4]/1000 if values[4] is not None else None,
            "sbl_balance":values[5]/1000 if values[5] is not None else None,
            "sbl_market_value":values[7],
        })
    for current,previous,key in [
        (out.get("sbl_short_balance"),out.get("sbl_short_previous"),"sbl_short_change"),
        (out.get("sbl_balance"),out.get("sbl_borrow_previous"),"sbl_balance_change"),
    ]:
        out[key]=(current-previous) if current is not None and previous is not None else None
    return out if len(out)>1 else {}


async def fetch_twse_lending_snapshot(ticker: str, trading_date: str) -> tuple[dict[str, Any], list[str]]:
    compact=str(trading_date or "").replace("-","")[:8]
    if len(compact)!=8: return {},["TWSE_LENDING: missing trading date"]
    try:
        async with httpx.AsyncClient(timeout=20,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.3.5"}) as client:
            short_r,borrow_r=await asyncio.gather(
                client.get("https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U",params={"date":compact,"selectType":"ALL","response":"json"}),
                client.get("https://www.twse.com.tw/rwd/zh/lending/TWT72U",params={"date":compact,"response":"json"}),
            )
            short_r.raise_for_status(); borrow_r.raise_for_status()
        return parse_twse_lending_snapshots(short_r.json(),borrow_r.json(),ticker,trading_date),[]
    except Exception as exc:
        return {},[f"TWSE_LENDING: {type(exc).__name__}: {exc}"]



MOPS_CSV_BASE = "https://mopsfin.twse.com.tw/opendata"
MOPS_HISTORY_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t163sb04"
MOPS_HISTORY_CACHE_TTL = max(CACHE_TTL, 21600)
_MOPS_HISTORY_CACHE: dict[tuple[str, int, int], tuple[float, dict[str, dict[str, Any]]]] = {}
IR_FINANCIAL_PAGES = {
    "3661": "https://www.alchip.com/en/Investors/financials/report",
}

async def mops_csv_rows(filename: str) -> list[dict[str, Any]]:
    """Official MOPS CSV fallback. Some foreign/KY issuers can appear here even when JSON feeds lag."""
    url=f"{MOPS_CSV_BASE}/{filename}"
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.3.2"}) as client:
        r=await client.get(url); r.raise_for_status()
    raw=r.content
    text=None
    for enc in ("utf-8-sig","utf-8","cp950","big5"):
        try:
            text=raw.decode(enc); break
        except Exception:
            continue
    if text is None: return []
    return [dict(x) for x in csv.DictReader(io.StringIO(text))]

def _official_row_to_snapshot(row: dict[str, Any], source: str, endpoint: str, market: str="上市", kind: str="summary") -> dict[str, Any] | None:
    def val(*names):
        for name in names:
            if name in row and row.get(name) not in (None,""): return row.get(name)
        return None
    code=val("公司代號","SecuritiesCompanyCode","公司代碼","代號")
    if code is None:
        code=_row_value(row,["公司","代號"]) or _row_value(row,["代號"])
    eps=parse_num_text(val("基本每股盈餘(元)","基本每股盈餘","每股盈餘") or _row_value(row,["基本每股盈餘"]) or _row_value(row,["每股盈餘"]))
    revenue=parse_num_text(val("營業收入") or _row_value(row,["營業收入"],["百分比"]))
    gross=parse_num_text(val("營業毛利（毛損）","營業毛利(毛損)") or _row_value(row,["營業毛利"],["百分比"]))
    op=parse_num_text(val("營業利益（損失）","營業利益(損失)","營業利益") or _row_value(row,["營業利益"],["百分比"]))
    net=parse_num_text(val("本期淨利（淨損）","本期淨利(淨損)","本期淨利","稅後淨利") or _row_value(row,["本期淨利"],["百分比"]) or _row_value(row,["稅後淨利"]))
    year=val("年度") or _row_value(row,["年度"]) or _row_value(row,["年"])
    quarter=val("季別") or _row_value(row,["季別"]) or _row_value(row,["季"])
    out_date=val("出表日期","資料日期") or _row_value(row,["出表日期"]) or _row_value(row,["資料日期"])
    try:
        y=int(str(year).strip()); y=y+1911 if y<1911 else y
    except Exception: y=None
    q=None; qm=re.search(r"([1-4])",str(quarter or "")); q=int(qm.group(1)) if qm else None
    period=f"{y} Q{q}" if y and q else (roc_date_to_iso(out_date) or "latest")
    return {"source":source,"market":market,"endpoint":endpoint,"official":True,"period":period,
            "fiscal_year":y,"fiscal_quarter":q,"statement_date":roc_date_to_iso(out_date),
            "ytd_eps":eps,"revenue_ytd":revenue,"gross_profit_ytd":gross,
            "operating_income_ytd":op,"net_income_ytd":net,"raw_keys":list(row.keys())[:30],
            "feed_kind":kind,"company_code":re.sub(r"\D","",str(code or "")) or str(code or "").strip(),
            "completeness":sum(v is not None for v in (eps,revenue,gross,op,net))}

async def fetch_mops_csv_official(ticker: str) -> list[dict[str, Any]]:
    files=[
        ("t187ap14_L.csv","TWSE/MOPS EPS CSV","summary"),
        ("t187ap06_L_ci.csv","TWSE/MOPS Income CSV","detail"),
        ("t187ap06_L_mim.csv","TWSE/MOPS Income CSV","detail"),
        ("t187ap06_X_ci.csv","TWSE/MOPS Public/Foreign Income CSV","detail"),
        ("t187ap06_X_mim.csv","TWSE/MOPS Public/Foreign Income CSV","detail"),
    ]
    out=[]
    for filename,source,kind in files:
        try:
            rows=await mops_csv_rows(filename)
            for row in rows:
                snap=_official_row_to_snapshot(row,source,filename,"上市/公發",kind)
                if snap and snap.get("company_code")==ticker:
                    out.append(snap); break
        except Exception:
            continue
    return out

def _extract_pdf_financials(text: str, year: int, quarter: int) -> dict[str, Any]:
    """Parse reviewed/audited IR PDFs conservatively.

    For Q2/Q3 Alchip-style statements the columns are:
    current quarter, prior-year quarter, current YTD, prior-year YTD.
    Therefore the third amount/EPS is the current YTD value. We only accept
    explicit table patterns; ambiguity returns None instead of guessing.
    """
    compact=" ".join(text.split())
    def num(x): return parse_num_text(str(x).replace("$", "").replace("(", "-").replace(")", ""))
    def eps_current_ytd():
        if quarter in (2,3):
            m=re.search(r"(?:EARNINGS PER SHARE.*?)(?:Basic)\s+\$?\s*([0-9.]+)\s+\$?\s*([0-9.]+)\s+\$?\s*([0-9.]+)\s+\$?\s*([0-9.]+)", compact, re.I)
            return num(m.group(3)) if m else None
        if quarter==1:
            m=re.search(r"(?:EARNINGS PER SHARE.*?)(?:Basic)\s+\$?\s*[0-9.]+\s+\$?\s*([0-9.]+)",compact,re.I)
            return num(m.group(1)) if m else None
        m=re.search(r"(?:EARNINGS PER SHARE.*?)(?:Basic)\s+\$?\s*[0-9.]+\s+\$?\s*([0-9.]+)",compact,re.I)
        return num(m.group(1)) if m else None
    def table_ytd(label: str, pct_pattern: str="(?:100|[0-9]{1,2})"):
        # Amount/% repeated four times; choose current YTD (third amount) for Q2/Q3.
        pat=rf"{label}.*?\$?\s*([0-9,]+)\s+{pct_pattern}\s+\$?\s*([0-9,]+)\s+{pct_pattern}\s+\$?\s*([0-9,]+)\s+{pct_pattern}\s+\$?\s*([0-9,]+)\s+{pct_pattern}"
        m=re.search(pat,compact,re.I)
        if m: return num(m.group(3))
        # Q1 / annual two-column form: USD and NTD (or current/prior year); prefer NTD second amount.
        m=re.search(rf"{label}.*?\$?\s*([0-9,]+)\s+\$?\s*([0-9,]+)",compact,re.I)
        return num(m.group(2)) if m else None
    eps=eps_current_ytd()
    revenue=table_ytd(r"OPERATING REVENUE(?:\s*\(Note\s*20\))?", "100")
    gross=table_ytd(r"GROSS PROFIT")
    op=table_ytd(r"PROFIT FROM OPERATIONS")
    net=table_ytd(r"NET PROFIT FOR THE PERIOD")
    return {"ytd_eps":eps,"revenue_ytd":revenue,"gross_profit_ytd":gross,"operating_income_ytd":op,"net_income_ytd":net}

async def fetch_company_ir_financial(ticker: str, expected_year: int, expected_quarter: int) -> dict[str, Any] | None:
    page=IR_FINANCIAL_PAGES.get(ticker)
    if not page: return None
    try:
        async with httpx.AsyncClient(timeout=30,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.3.2"}) as client:
            r=await client.get(page); r.raise_for_status(); page_html=r.text
            hrefs=re.findall(r'href=["\']([^"\']+)["\']',page_html,re.I)
            embedded=re.findall(r'["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',page_html,re.I)
            # Prefer PDFs whose URL/text suggests the expected year/quarter. Hidden/data-attribute PDF URLs are included too.
            pdfs=[]
            for href in list(dict.fromkeys(hrefs+embedded)):
                if ".pdf" not in href.lower(): continue
                url=urljoin(str(r.url),href)
                score=0
                lo=url.lower()
                if str(expected_year) in lo: score+=4
                if f"q{expected_quarter}" in lo or f"_{expected_quarter}_" in lo: score+=5
                pdfs.append((score,url))
            pdfs=sorted(set(pdfs),reverse=True)[:12]
            for _,url in pdfs:
                pr=await client.get(url); pr.raise_for_status()
                reader=PdfReader(io.BytesIO(pr.content))
                text="\n".join((page.extract_text() or "") for page in reader.pages[:12])
                # Period must be explicit; never infer a current quarter from download date.
                low=text.lower()
                if expected_quarter==2:
                    period_ok=(str(expected_year) in text and ("six months ended june 30" in low or "六月三十日" in text or "6月30日" in text))
                elif expected_quarter==1:
                    period_ok=(str(expected_year) in text and ("three months ended march 31" in low or "三月三十一日" in text or "3月31日" in text))
                elif expected_quarter==3:
                    period_ok=(str(expected_year) in text and ("nine months ended september 30" in low or "九月三十日" in text or "9月30日" in text))
                else:
                    period_ok=(str(expected_year) in text and ("year ended december 31" in low or "十二月三十一日" in text or "12月31日" in text))
                if not period_ok: continue
                vals=_extract_pdf_financials(text,expected_year,expected_quarter)
                return {"source":"Company IR audited/reviewed PDF","market":"IR","endpoint":url,"official":True,
                        "period":f"{expected_year} Q{expected_quarter}","fiscal_year":expected_year,"fiscal_quarter":expected_quarter,
                        "statement_date":None,"feed_kind":"ir_pdf","completeness":sum(v is not None for v in vals.values()),
                        **vals,"ir_page":page}
    except Exception:
        return None
    return None


def _text_field(row: dict[str, Any], *names: str) -> str:
    """Read a MOPS field while tolerating invisible/trailing spaces in official keys."""
    wanted={re.sub(r"\s+","",x) for x in names}
    for k,v in row.items():
        if re.sub(r"\s+","",str(k)) in wanted:
            return str(v or "")
    return ""


def _roc_year_to_ad(v: str | int | None) -> int | None:
    if v is None: return None
    m=re.search(r"(\d{2,4})",str(v))
    if not m: return None
    y=int(m.group(1))
    return y+1911 if y < 1911 else y


def _announcement_period(text: str) -> tuple[int|None,int|None]:
    """Resolve fiscal year/quarter from MOPS board-approved financial-report disclosures."""
    compact=re.sub(r"\s+","",text)
    # Highest-confidence: explicit report start/end date, e.g. 115/01/01~115/06/30.
    m=re.search(r"(?:報導期間|報告期間|起訖日期).*?(\d{2,4})[/-]0?1[/-]0?1.*?(\d{2,4})[/-](0?3|0?6|0?9|12)[/-](?:30|31)",compact,re.I)
    if m:
        y=_roc_year_to_ad(m.group(2) or m.group(1)); month=int(m.group(3)); q={3:1,6:2,9:3,12:4}.get(month)
        if y and q: return y,q
    # Fallback: 115年度第二季 / 115年第2季.
    m=re.search(r"(\d{2,4})年(?:度)?第?([一二三四1234])季",compact)
    if m:
        qmap={"一":1,"二":2,"三":3,"四":4,"1":1,"2":2,"3":3,"4":4}
        return _roc_year_to_ad(m.group(1)),qmap.get(m.group(2))
    return None,None


def _extract_mops_financial_announcement(row: dict[str, Any], ticker: str) -> dict[str, Any] | None:
    """Parse the official MOPS material-information disclosure for board-approved quarterly financials.

    This is intentionally conservative: it only accepts announcements whose subject/description explicitly
    say the board approved a quarterly/annual financial report and whose report period can be resolved.
    """
    code=_text_field(row,"公司代號","公司代碼")
    if re.sub(r"\D","",code) != ticker: return None
    subject=_text_field(row,"主旨","主旨 ")
    desc=_text_field(row,"說明")
    blob=f"{subject}\n{desc}"
    key=re.sub(r"\s+","",blob)
    if not ("財務報告" in key and ("董事會通過" in key or "董事會決議" in key or "提報董事會" in key)):
        return None
    fy,fq=_announcement_period(blob)
    if not fy or not fq: return None
    def pick(patterns):
        for pat in patterns:
            m=re.search(pat,blob,re.I|re.S)
            if m:
                return parse_num_text(m.group(1))
        return None
    # MOPS amounts here are explicitly stated in NT$ thousand; keep the same unit as official income statements.
    revenue=pick([r"累計至本期止營業收入[^:：]*[:：]\s*([\-0-9,\.]+)",r"營業收入\(仟元\)[^:：]*[:：]\s*([\-0-9,\.]+)"])
    gross=pick([r"累計至本期止營業毛利[^:：]*[:：]\s*([\-0-9,\.]+)",r"營業毛利[^:：]*\(仟元\)[^:：]*[:：]\s*([\-0-9,\.]+)"])
    op=pick([r"累計至本期止營業利益[^:：]*[:：]\s*([\-0-9,\.]+)",r"營業利益[^:：]*\(仟元\)[^:：]*[:：]\s*([\-0-9,\.]+)"])
    net=pick([r"歸屬於母公司業主淨利[^:：]*[:：]\s*([\-0-9,\.]+)",r"累計至本期止本期淨利[^:：]*[:：]\s*([\-0-9,\.]+)"])
    eps=pick([r"基本每股盈餘(?:\(損失\))?[^:：]*[:：]\s*([\-0-9,\.]+)",r"每股盈餘[^:：]*[:：]\s*([\-0-9,\.]+)"])
    speech=roc_date_to_iso(_text_field(row,"發言日期") or _text_field(row,"出表日期"))
    return {
        "source":"TWSE/MOPS board-approved financial disclosure",
        "market":"上市重大訊息","endpoint":"/opendata/t187ap04_L","official":True,
        "period":f"{fy} Q{fq}","fiscal_year":fy,"fiscal_quarter":fq,"statement_date":speech,
        "feed_kind":"material_announcement","company_code":ticker,"ytd_eps":eps,
        "revenue_ytd":revenue,"gross_profit_ytd":gross,"operating_income_ytd":op,"net_income_ytd":net,
        "completeness":sum(v is not None for v in (eps,revenue,gross,op,net)),
        "subject":subject,"announcement_date":speech,
    }


async def fetch_mops_material_financial(ticker: str, expected_year: int | None=None, expected_quarter: int | None=None) -> list[dict[str, Any]]:
    """Official fallback for foreign/KY issuers whose XBRL summary feed lags.

    Layer A uses TWSE's daily MOPS material-information OpenAPI. Layer B tries the official MOPS
    historical-search endpoint so a disclosure from one or two days ago is still discoverable.
    """
    out=[]
    ua={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.3.2"}
    async with httpx.AsyncClient(timeout=25,follow_redirects=True,headers=ua) as client:
        # A. Daily official material-information feed.
        try:
            r=await client.get(TWSE_OPENAPI+"/opendata/t187ap04_L"); r.raise_for_status(); data=r.json()
            for row in (data if isinstance(data,list) else []):
                snap=_extract_mops_financial_announcement(row,ticker)
                if snap: out.append(snap)
        except Exception:
            pass
        # B. Official MOPS historical material-information search. The HTML response contains the same
        # disclosure text; this layer is best-effort because MOPS occasionally changes presentation HTML.
        try:
            roc_year=(expected_year-1911) if expected_year else (date.today().year-1911)
            form={"encodeURIComponent":"1","step":"1","firstin":"1","off":"1","TYPEK":"all",
                  "co_id":ticker,"year":str(roc_year),"month":f"{date.today().month:02d}"}
            r=await client.post("https://mops.twse.com.tw/mops/web/ajax_t05st01",data=form)
            if r.status_code==200 and ticker in r.text:
                text=html.unescape(re.sub(r"<[^>]+>","\n",r.text))
                text=re.sub(r"[\t\r ]+"," ",text)
                # Locate report-approval blocks and convert to pseudo rows for the shared parser.
                for m in re.finditer(r"(.{0,240}(?:董事會通過|董事會決議).{0,1200}財務報告.{0,2400})",text,re.S):
                    block=m.group(1)
                    snap=_extract_mops_financial_announcement({"公司代號":ticker,"主旨":"董事會通過財務報告","說明":block,"發言日期":""},ticker)
                    if snap: out.append(snap)
        except Exception:
            pass
    # Deduplicate by period, keep most complete.
    best={}
    for x in out:
        k=(x.get("fiscal_year"),x.get("fiscal_quarter"))
        if k not in best or x.get("completeness",0)>best[k].get("completeness",0): best[k]=x
    return list(best.values())




def _decode_mops_html(raw: bytes, apparent: str | None = None) -> str:
    """Decode MOPS legacy/server-java pages. The endpoint may still emit Big5/CP950."""
    encs=[]
    if apparent: encs.append(apparent)
    encs += ["utf-8-sig","utf-8","cp950","big5"]
    for enc in encs:
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore")


def _flatten_cols(cols) -> list[str]:
    out=[]
    for c in cols:
        if isinstance(c, tuple):
            parts=[str(x).strip() for x in c if str(x).strip() and not str(x).startswith("Unnamed")]
            out.append(" ".join(parts))
        else:
            out.append(str(c).strip())
    return out


def _norm_label(v: Any) -> str:
    return re.sub(r"[\s　:：()（）/／\\\-]", "", str(v or "")).lower()


def _pick_income_row(df: pd.DataFrame, aliases: list[str]) -> int | None:
    if df.empty: return None
    # Account name is normally one of the first 2 columns; scan all cells for resilience.
    aliases_n=[_norm_label(x) for x in aliases]
    for i,row in df.iterrows():
        for v in row.iloc[:3].tolist():
            t=_norm_label(v)
            if any(a and a in t for a in aliases_n):
                return i
    return None


def _target_ytd_column(df: pd.DataFrame, quarter: int) -> Any | None:
    """Resolve current-period cumulative/YTD amount column in MOPS IFRS tables."""
    cols=_flatten_cols(df.columns)
    # Strongest signals first. MultiIndex from read_html usually preserves these labels.
    keys=["本期累計","本年度截至本季止","本年度截至本期止","本期累計數","本期累計金額","累計"]
    for key in keys:
        hits=[i for i,c in enumerate(cols) if key in c and not any(x in c for x in ("去年","前期","上期"))]
        if hits:
            # Prefer amount rather than percentage.
            for i in hits:
                if not any(x in cols[i] for x in ("%","％","百分比")): return df.columns[i]
            return df.columns[hits[0]]
    # Q1 is both quarter and YTD. Prefer first non-percentage numeric column after account label.
    if quarter==1:
        for i,c in enumerate(cols[1:], start=1):
            if not any(x in c for x in ("%","％","百分比")): return df.columns[i]
    # Legacy tables can expose four amount columns without useful flattened headers:
    # current quarter, prior-year quarter, current YTD, prior-year YTD.
    candidates=[df.columns[i] for i,c in enumerate(cols[1:], start=1) if not any(x in c for x in ("%","％","百分比"))]
    if quarter in (2,3) and len(candidates)>=3: return candidates[2]
    if candidates: return candidates[0]
    return None


def _mops_lxml_rows(html_text: str) -> list[dict[str, Any]]:
    """Parse HTML tables with lxml only. Avoid pandas.read_html/html5lib runtime dependency."""
    out=[]
    try:
        root=lxml_html.fromstring(html_text)
    except Exception:
        return out
    for ti,table in enumerate(root.xpath("//table")):
        rows=[]
        for tr in table.xpath(".//tr"):
            cells=[]
            for cell in tr.xpath("./th|./td"):
                txt=" ".join(" ".join(cell.itertext()).split())
                cells.append(txt)
            if cells: rows.append(cells)
        if rows: out.append({"table_index":ti,"rows":rows})
    return out


def _parse_mops_historical_eps(html_text: str, year: int, quarter: int, market_type: str) -> dict[str, dict[str, Any]]:
    """Parse the official MOPS historical income-statement summary into ticker snapshots.

    MOPS renders several industry-specific tables in one response. Each table has a slightly
    different shape, so columns are resolved from the header instead of a fixed index. Reported
    EPS is cumulative/YTD for Q2-Q4 and equals standalone EPS for Q1.
    """
    snapshots: dict[str, dict[str, Any]] = {}
    for table in _mops_lxml_rows(html_text):
        rows=table.get("rows") or []
        header_index=None; code_index=None; name_index=None; eps_index=None
        for ri,row in enumerate(rows):
            normalized=[_norm_label(cell) for cell in row]
            for ci,label in enumerate(normalized):
                if code_index is None and ("公司代號" in label or label in {"代號","公司代碼"}): code_index=ci
                if name_index is None and ("公司名稱" in label or label in {"名稱","公司簡稱"}): name_index=ci
                if eps_index is None and ("基本每股盈餘" in label or "basicearningspershare" in label): eps_index=ci
            if code_index is not None and eps_index is not None:
                header_index=ri; break
        if header_index is None or code_index is None or eps_index is None: continue
        for row in rows[header_index+1:]:
            if max(code_index,eps_index)>=len(row): continue
            ticker=re.sub(r"\D","",str(row[code_index] or ""))
            if not re.fullmatch(r"\d{4,6}",ticker): continue
            eps=parse_num_text(row[eps_index])
            if eps is None: continue
            company_name=str(row[name_index]).strip() if name_index is not None and name_index<len(row) else ticker
            endpoint=f"{MOPS_HISTORY_URL}?TYPEK={market_type}&year={year-1911}&season={quarter:02d}"
            snapshots[ticker]={
                "source":"MOPS historical income statement summary","market":"上市" if market_type=="sii" else "上櫃",
                "endpoint":endpoint,"request_method":"POST",
                "request_params":{"TYPEK":market_type,"year":year-1911,"season":f"{quarter:02d}"},
                "official":True,"period":f"{year} Q{quarter}",
                "fiscal_year":year,"fiscal_quarter":quarter,"statement_date":None,
                "feed_kind":"mops_historical_summary","company_code":ticker,"company_name":company_name,
                "ytd_eps":float(eps),"quarter_eps_direct":float(eps) if quarter==1 else None,
                "eps_provenance":"official_mops_historical","eps_confidence":100,
                "report_id":"t163sb04","table_index":table.get("table_index"),"completeness":1,
            }
    return snapshots


async def _fetch_mops_historical_market(year: int, quarter: int, market_type: str) -> dict[str, dict[str, Any]]:
    key=(market_type,year,quarter)
    cached=_MOPS_HISTORY_CACHE.get(key)
    if cached and time.time()-cached[0] < MOPS_HISTORY_CACHE_TTL:
        return cached[1]
    form={
        # MOPS currently requires this literal historical form spelling; changing it to the
        # grammatically correct "true" can return an empty/security response.
        "encodeURIComponent":"1","step":"1","firstin":"ture","off":"1",
        "TYPEK":market_type,"year":str(year-1911 if year>=1912 else year),"season":f"{quarter:02d}",
    }
    headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.3.5","Referer":"https://mopsov.twse.com.tw/mops/"}
    async with httpx.AsyncClient(timeout=35,follow_redirects=True,headers=headers) as client:
        response=await client.post(MOPS_HISTORY_URL,data=form)
        response.raise_for_status()
    text=_decode_mops_html(response.content,response.encoding)
    if "因為安全性考量" in text or len(text)<500:
        raise RuntimeError("MOPS historical response was blocked or empty")
    rows=_parse_mops_historical_eps(text,year,quarter,market_type)
    _MOPS_HISTORY_CACHE[key]=(time.time(),rows)
    return rows


async def fetch_mops_historical_eps(ticker: str, year: int, quarter: int) -> dict[str, Any] | None:
    """Resolve exact-period cumulative EPS from the official MOPS historical summary."""
    errors=[]; successful=False
    for market_type in ("sii","otc"):
        try:
            rows=await _fetch_mops_historical_market(year,quarter,market_type)
            successful=True
            if ticker in rows: return dict(rows[ticker])
        except Exception as exc:
            errors.append(f"{market_type}:{type(exc).__name__}:{exc}")
    if not successful and errors:
        raise RuntimeError("; ".join(errors))
    return None

def _eps_from_lxml_rows(tables: list[dict[str, Any]], quarter: int) -> tuple[float|None, dict[str, Any]|None]:
    aliases=("基本每股盈餘","每股盈餘","basic earnings per share","earnings per share")
    for table in tables:
        rows=table.get("rows") or []
        for ri,row in enumerate(rows):
            label=" | ".join(row[:3]).lower()
            if not any(a in label for a in aliases):
                continue
            nums=[]
            for ci,v in enumerate(row[1:],start=1):
                n=parse_num_text(v)
                if n is not None: nums.append((ci,v,float(n)))
            if not nums: continue
            # IFRS Q2/Q3 income statement rows commonly expose current quarter, prior-year quarter,
            # current YTD, prior-year YTD. Q1 is both quarter and YTD.
            pick_idx=0 if quarter==1 else (2 if quarter in (2,3) and len(nums)>=3 else 0)
            ci,raw,val=nums[pick_idx]
            return val,{"table_index":table.get("table_index"),"row_index":ri,"row":row[:10],"numeric_candidates":[{"column_index":x[0],"raw":x[1],"number":x[2]} for x in nums[:8]],"selected_numeric_index":pick_idx,"selected_column_index":ci}
    return None,None

def _extract_mops_ifrs_tables(html_text: str, ticker: str, year: int, quarter: int, report_id: str, url: str) -> dict[str, Any] | None:
    """Parse a company-specific MOPS IFRS page using lxml, without html5lib."""
    if "查無資料" in html_text or "無符合條件" in html_text or "HTTP Status 404" in html_text: return None
    tables=_mops_lxml_rows(html_text)
    if not tables: return None
    eps,meta=_eps_from_lxml_rows(tables,quarter)
    # Historical resolver only needs EPS. Current-period financial metrics come from official CSV/OpenAPI.
    if eps is None: return None
    return {
        "source":"MOPS company IFRS report","market":"MOPS","endpoint":url,"official":True,
        "period":f"{year} Q{quarter}","fiscal_year":year,"fiscal_quarter":quarter,
        "statement_date":None,"feed_kind":f"mops_company_{report_id}","company_code":ticker,
        "ytd_eps":eps,"revenue_ytd":None,"gross_profit_ytd":None,
        "operating_income_ytd":None,"net_income_ytd":None,"completeness":1,
        "report_id":report_id,"table_count":len(tables),"selected_column":str((meta or {}).get("selected_column_index")),
        "eps_parser":"lxml","eps_parser_meta":meta,
    }



def _safe_preview_text(text: str, limit: int = 5000) -> str:
    """Small diagnostic preview; enough to debug MOPS parsing without returning huge pages."""
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]

def _df_preview(df: pd.DataFrame, max_rows: int = 14, max_cols: int = 8) -> dict[str, Any]:
    try:
        slim=df.iloc[:max_rows,:max_cols].copy()
        return {
            "shape":[int(df.shape[0]),int(df.shape[1])],
            "columns":[str(x) for x in _flatten_cols(df.columns)[:max_cols]],
            "rows":[[str(v)[:180] for v in row] for row in slim.astype(str).values.tolist()]
        }
    except Exception as e:
        return {"error":f"{type(e).__name__}: {e}"}

def _eps_candidates_from_tables(tables: list[pd.DataFrame]) -> list[dict[str, Any]]:
    out=[]
    aliases=["基本每股盈餘合計","基本每股盈餘","每股盈餘","Basic earnings per share","Earnings per share"]
    for ti,df in enumerate(tables):
        try:
            cols=_flatten_cols(df.columns)
            for i,row in df.iterrows():
                first=[str(x) for x in row.iloc[:3].tolist()]
                label=" | ".join(first)
                if any(_norm_label(a) in _norm_label(label) for a in aliases):
                    vals=[]
                    for ci,v in enumerate(row.tolist()):
                        n=parse_num_text(v)
                        if n is not None:
                            vals.append({"column_index":ci,"column":cols[ci] if ci<len(cols) else str(ci),"raw":str(v),"number":n})
                    out.append({"table_index":ti,"row_index":str(i),"label":label[:500],"values":vals[:12]})
        except Exception:
            continue
    return out

async def _request_mops_ifrs_candidates(client: httpx.AsyncClient, ticker: str, year: int, quarter: int, report_id: str):
    """Try current MOPS web routes first; keep legacy server-java only as a diagnostic fallback."""
    mops_year=year-1911 if year>=1912 else year
    params={"step":"1","CO_ID":ticker,"SYEAR":str(mops_year),"SSEASON":str(quarter),"REPORT_ID":report_id}
    candidates=[
        ("GET","https://mops.twse.com.tw/mops/web/t164sb01",params),
        ("POST","https://mops.twse.com.tw/mops/web/t164sb01",params),
        ("POST","https://mops.twse.com.tw/mops/web/ajax_t164sb01",params),
        ("GET","https://mops.twse.com.tw/server-java/t164sb01",params),
    ]
    results=[]
    for method,url,payload in candidates:
        try:
            if method=="POST": r=await client.post(url,data=payload)
            else: r=await client.get(url,params=payload)
            text=_decode_mops_html(r.content,r.encoding)
            results.append({"method":method,"url":url,"params":payload,"response":r,"text":text})
            if r.status_code==200 and len(r.content)>500 and "HTTP Status 404" not in text and "Not Found" not in text:
                snap=_extract_mops_ifrs_tables(text,ticker,year,quarter,report_id,str(r.url))
                if snap:
                    return snap,results
        except Exception as e:
            results.append({"method":method,"url":url,"params":payload,"error":f"{type(e).__name__}: {e}"})
    return None,results

async def trace_mops_company_ifrs(ticker: str, year: int, quarter: int) -> dict[str, Any]:
    """Raw trace of current/legacy MOPS historical IFRS routes plus lxml EPS parser output."""
    headers={"User-Agent":"Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1",
             "Referer":"https://mops.twse.com.tw/mops/"}
    mops_year=year-1911 if year>=1912 else year
    trace={"ticker":ticker,"gregorian_year":year,"mops_syear":mops_year,"quarter":quarter,"parser":"lxml","requests":[]}
    async with httpx.AsyncClient(timeout=30,follow_redirects=True,headers=headers) as client:
        for report_id in ("C","B","A"):
            snap,requests=await _request_mops_ifrs_candidates(client,ticker,year,quarter,report_id)
            for x in requests:
                item={"report_id":report_id,"method":x.get("method"),"request_url":x.get("url"),"request_params":x.get("params")}
                if x.get("error"):
                    item["request_error"]=x["error"]
                else:
                    r=x["response"]; text=x["text"]
                    item.update({"http_status":r.status_code,"final_url":str(r.url),"content_type":r.headers.get("content-type"),"content_length":len(r.content),"encoding_hint":r.encoding,"decoded_preview":_safe_preview_text(text,2500)})
                    tables=_mops_lxml_rows(text)
                    eps,meta=_eps_from_lxml_rows(tables,quarter)
                    item["lxml_table_count"]=len(tables); item["lxml_eps"]=eps; item["lxml_eps_meta"]=meta
                trace["requests"].append(item)
            if snap:
                trace["selected"]=snap
                break
    return trace

async def fetch_mops_company_ifrs(ticker: str, year: int, quarter: int) -> list[dict[str, Any]]:
    """Historical company EPS resolver using current MOPS routes and lxml parser."""
    out=[]
    headers={"User-Agent":"Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1",
             "Referer":"https://mops.twse.com.tw/mops/"}
    async with httpx.AsyncClient(timeout=30,follow_redirects=True,headers=headers) as client:
        for report_id in ("C","B","A"):
            snap,_=await _request_mops_ifrs_candidates(client,ticker,year,quarter,report_id)
            if snap: out.append(snap)
    return out



async def fetch_tsmc_quarterly_release(year: int, quarter: int) -> dict[str, Any] | None:
    """TSMC official quarter bridge.

    Resolve a *single-quarter* EPS from TSMC official sources. The quarterly-results page is tried
    first. If the page/PDF markup does not expose EPS to the server-side client, fall back to the
    official TSMC press/news archive and locate the matching quarter's EPS announcement. This keeps
    the bridge fully official while avoiding blocked historical MOPS HTML.
    """
    if quarter not in (1,2,3,4): return None
    landing=f"https://investor.tsmc.com/english/quarterly-results/{year}/q{quarter}"
    headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.3.2"}
    # Evidence-ledger seed URLs: official TSMC press releases are stable evidence pages.
    # Values are never hard-coded; the page itself is fetched and parsed.
    # Stable company-official evidence pages. Values are NEVER embedded here; each URL is
    # fetched live and EPS is parsed from the official publication. Multiple language URLs are
    # supplied because one locale can occasionally be blocked or render differently.
    seed_urls={
        (2026,1): ["https://pr.tsmc.com/english/news/3297", "https://pr.tsmc.com/schinese/news/3297"],
        (2026,2): ["https://pr.tsmc.com/english/news/3326", "https://pr.tsmc.com/schinese/news/3326"],
        (2025,1): ["https://pr.tsmc.com/english/news/3222", "https://pr.tsmc.com/schinese/news/3222"],
        (2025,2): ["https://pr.tsmc.com/english/news/3249", "https://pr.tsmc.com/schinese/news/3249"],
        (2025,3): ["https://pr.tsmc.com/english/news/3264", "https://pr.tsmc.com/schinese/news/3264"],
        (2025,4): ["https://pr.tsmc.com/english/news/3281", "https://pr.tsmc.com/schinese/news/3281"],
        (2024,4): ["https://pr.tsmc.com/english/news/3201", "https://pr.tsmc.com/schinese/news/3201"],
    }

    def extract(blob: str):
        blob=" ".join(html.unescape(blob or "").split())
        eps_patterns=[
            r"(?:diluted\s+)?earnings per share(?:\s+of|\s+was|\s*[:：])?\s*NT\$\s*([0-9.]+)",
            r"EPS(?:\s+of|\s+was|\s*[:：])?\s*NT\$\s*([0-9.]+)",
            r"每股盈餘(?:為|新台幣|\s*[:：])?\s*(?:新台幣)?\s*([0-9.]+)\s*元",
            r"Earnings per Share\s*-\s*Diluted\s+\$?\s*[0-9.]+\s+\$?\s*([0-9.]+)",
        ]
        qeps=None
        for pat in eps_patterns:
            m=re.search(pat,blob,re.I)
            if m:
                qeps=parse_num_text(m.group(1)); break
        gm=om=None
        for pat in (r"Gross margin(?: for the quarter)? was\s*([0-9.]+)%", r"Gross Margin\s+([0-9.]+)%", r"毛利率(?:為)?\s*([0-9.]+)%"):
            m=re.search(pat,blob,re.I)
            if m: gm=parse_num_text(m.group(1)); break
        for pat in (r"operating margin(?: for the quarter)? was\s*([0-9.]+)%", r"Operating Margin\s+([0-9.]+)%", r"營業利益率(?:為)?\s*([0-9.]+)%"):
            m=re.search(pat,blob,re.I)
            if m: om=parse_num_text(m.group(1)); break
        return qeps,gm,om

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
            source_url=landing; page_html=""; pdf_text=""
            try:
                r=await client.get(landing); r.raise_for_status(); page_html=r.text; source_url=str(r.url)
                hrefs=re.findall(r'href=["\']([^"\']+)["\']',page_html,re.I)
                candidates=[]
                for href in hrefs:
                    lo=href.lower(); score=0
                    if 'earningsrelease' in lo or 'earnings-release' in lo or 'earnings_release' in lo: score+=10
                    if '.pdf' in lo: score+=3
                    if f'q{quarter}' in lo or f'{quarter}q' in lo: score+=2
                    if score: candidates.append((score,urljoin(str(r.url),href)))
                for _,url in sorted(set(candidates), reverse=True):
                    try:
                        pr=await client.get(url); pr.raise_for_status()
                        if 'pdf' not in (pr.headers.get('content-type') or '').lower() and not url.lower().endswith('.pdf'): continue
                        reader=PdfReader(io.BytesIO(pr.content))
                        text="\n".join((pg.extract_text() or "") for pg in reader.pages[:6])
                        if text: pdf_text=text; source_url=url; break
                    except Exception: continue
            except Exception:
                pass
            clean=" ".join(html.unescape(re.sub(r"<[^>]+>"," ",page_html)).split())
            qeps,gm,om=extract(clean+" "+pdf_text)

            # First try a stable official publication URL when one is known. The EPS value is
            # still parsed from the live official page, so the ledger records source evidence rather
            # than embedding a financial number in code.
            if qeps is None and (year,quarter) in seed_urls:
                for seed in seed_urls[(year,quarter)]:
                    try:
                        nr=await client.get(seed); nr.raise_for_status()
                        article=" ".join(html.unescape(re.sub(r'<[^>]+>',' ',nr.text)).split())
                        qe,gg,oo=extract(article)
                        if qe is not None:
                            qeps=qe; gm=gm if gm is not None else gg; om=om if om is not None else oo
                            source_url=str(nr.url)
                            break
                    except Exception:
                        continue

            # Quarter Bridge fallback: TSMC official press archive. This is especially useful for
            # historical Q1 where the investor landing page can be JS-heavy while the press release
            # is static and contains the direct EPS value in plain HTML.
            if qeps is None:
                ord_en={1:'First',2:'Second',3:'Third',4:'Fourth'}[quarter]
                archive_urls=["https://pr.tsmc.com/english/latest-news","https://pr.tsmc.com/chinese/latest-news"]
                article_candidates=[]
                for archive in archive_urls:
                    try:
                        ar=await client.get(archive); ar.raise_for_status()
                        for href,label_html in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',ar.text,re.I|re.S):
                            label=" ".join(html.unescape(re.sub(r'<[^>]+>',' ',label_html)).split())
                            lo=label.lower()
                            score=0
                            if str(year) in label: score+=5
                            if 'eps' in lo or '每股盈餘' in label: score+=5
                            if ord_en.lower() in lo and 'quarter' in lo: score+=6
                            if f'第{quarter}季' in label or {1:'第一季',2:'第二季',3:'第三季',4:'第四季'}[quarter] in label: score+=6
                            if score>=10: article_candidates.append((score,urljoin(str(ar.url),href),label))
                    except Exception: continue
                for _,url,_label in sorted(article_candidates,reverse=True):
                    try:
                        nr=await client.get(url); nr.raise_for_status()
                        article=" ".join(html.unescape(re.sub(r'<[^>]+>',' ',nr.text)).split())
                        qe,gg,oo=extract(article)
                        if qe is not None:
                            qeps=qe; gm=gm if gm is not None else gg; om=om if om is not None else oo
                            source_url=str(nr.url); break
                    except Exception: continue

            if gm is None and om is None and qeps is None: return None
            return {"source":"TSMC official quarter bridge","market":"Company IR","endpoint":source_url,"official":True,
                    "period":f"{year} Q{quarter}","fiscal_year":year,"fiscal_quarter":quarter,"statement_date":None,
                    "feed_kind":"company_ir_quarter_bridge","company_code":"2330","quarter_eps_direct":qeps,
                    "gross_margin_direct":gm,"operating_margin_direct":om,
                    "eps_provenance":"company_official_direct","eps_confidence":100,
                    "bridge_version":"5.2.15",
                    "completeness":sum(v is not None for v in (qeps,gm,om))}
    except Exception:
        return None

async def fetch_official_income_statement(ticker: str) -> dict[str, Any]:
    """Fetch the newest official MOPS income-statement row across listed/OTC industry schemas.

    Do not stop at the first endpoint: a company can live in a schema other than general industry,
    and different endpoints may refresh at slightly different times. The newest fiscal period wins.
    """
    industry_suffixes=["ci","mim","basi","bd","fh","ins"]
    candidates=[]
    # Daily EPS summary is the fastest official period anchor and is not tied to an industry schema.
    # It is especially useful for KY/foreign issuers whose detailed statement may live outside the usual L_* feed.
    candidates.append((TWSE_OPENAPI, "/opendata/t187ap14_L", "TWSE/MOPS EPS Daily Summary", "上市", "summary"))
    candidates.append((TPEX_OPENAPI, "/mopsfin_t187ap14_O", "TPEx/MOPS EPS Daily Summary", "上櫃", "summary"))
    for suffix in industry_suffixes:
        candidates.append((TWSE_OPENAPI, f"/opendata/t187ap06_L_{suffix}", "TWSE/MOPS Income Statement", "上市", "detail"))
        # X_* is the public-company/foreign-issuer feed and covers cases that do not appear in L_* as expected.
        candidates.append((TWSE_OPENAPI, f"/opendata/t187ap06_X_{suffix}", "TWSE/MOPS Income Statement (X/foreign)", "公發/外國", "detail"))
        candidates.append((TPEX_OPENAPI, f"/mopsfin_t187ap06_O_{suffix}", "TPEx/MOPS Income Statement", "上櫃", "detail"))
    errors=[]; found=[]

    # Layer 0: official MOPS historical summary for the expected quarter. Unlike the legacy
    # company HTML route, this aggregate endpoint remains accessible and also covers KY issuers.
    try:
        ey,eq,_=expected_latest_financial_period(date.today())
        historical_current=await fetch_mops_historical_eps(ticker,ey,eq)
        if historical_current: found.append(historical_current)
    except Exception as e:
        errors.append(f"mops_history_current:{type(e).__name__}")

    # TSMC publishes quarterly results earlier than some MOPS aggregate refreshes.
    if ticker=="2330":
        try:
            ey,eq,_=expected_latest_financial_period(date.today())
            ir=await fetch_tsmc_quarterly_release(ey,eq)
            if ir: found.append(ir)
        except Exception as e:
            errors.append(f"tsmc_ir:{type(e).__name__}")

    def company_code(row: dict[str, Any]) -> str:
        direct=row.get("公司代號") or row.get("SecuritiesCompanyCode") or row.get("公司代碼") or row.get("代號")
        if direct is not None: return re.sub(r"\D", "", str(direct)) or str(direct).strip()
        v=_row_value(row,["公司","代號"]) or _row_value(row,["代號"])
        return str(v or "").strip()

    async def probe(base: str, path: str, source: str, market: str, kind: str):
        try:
            rows=await openapi_json(base,path)
            row=next((x for x in rows if company_code(x)==ticker),None)
            if not row: return None
            eps=parse_num_text(_row_value(row,["基本每股盈餘"]) or _row_value(row,["每股盈餘"]))
            revenue=parse_num_text(_row_value(row,["營業收入"],["百分比"]))
            gross=parse_num_text(_row_value(row,["營業毛利"],["百分比"]))
            op=parse_num_text(_row_value(row,["營業利益"],["百分比"]))
            net=parse_num_text(_row_value(row,["本期淨利"],["百分比"]) or _row_value(row,["本期稅後淨利"]))
            year=_row_value(row,["年度"]) or _row_value(row,["年"])
            quarter=_row_value(row,["季別"]) or _row_value(row,["季"])
            out_date=_row_value(row,["出表日期"]) or _row_value(row,["資料日期"])
            try:
                y=int(str(year).strip()); y=y+1911 if y<1911 else y
            except Exception: y=None
            q=None; qm=re.search(r"([1-4])",str(quarter or "")); q=int(qm.group(1)) if qm else None
            period=f"{y} Q{q}" if y and q else (roc_date_to_iso(out_date) or "latest")
            return {"source":source,"market":market,"endpoint":path,"official":True,"period":period,
                    "fiscal_year":y,"fiscal_quarter":q,"statement_date":roc_date_to_iso(out_date),
                    "ytd_eps":eps,"revenue_ytd":revenue,"gross_profit_ytd":gross,
                    "operating_income_ytd":op,"net_income_ytd":net,"raw_keys":list(row.keys())[:20],
                    "feed_kind":kind,
                    "completeness":sum(v is not None for v in (eps,revenue,gross,op,net))}
        except Exception as e:
            errors.append(f"{path}:{type(e).__name__}")
            return None

    results=await asyncio.gather(*(probe(*c) for c in candidates))
    found.extend(x for x in results if x)
    # Layer 2: direct official MOPS CSV. This bypasses JSON endpoint/schema lag and is critical for KY/foreign issuers.
    try:
        found.extend(await fetch_mops_csv_official(ticker))
    except Exception as e:
        errors.append(f"mops_csv:{type(e).__name__}")
    # Layer 3: official board-approved financial-report disclosure. This is especially important for
    # KY/foreign issuers whose structured XBRL/EPS feed can lag even after the board has approved Q2/Q3.
    try:
        ey,eq,_=expected_latest_financial_period(date.today())
        found.extend(await fetch_mops_material_financial(ticker,ey,eq))
    except Exception as e:
        errors.append(f"mops_material:{type(e).__name__}")

    # Layer 4: company IR reviewed/audited PDF for mapped issuers. Used only when the PDF explicitly states the expected period.
    try:
        ey,eq,_=expected_latest_financial_period(date.today())
        ir=await fetch_company_ir_financial(ticker,ey,eq)
        if ir: found.append(ir)
    except Exception as e:
        errors.append(f"company_ir:{type(e).__name__}")

    # If the expected quarter has not been filed yet, retain the newest official prior quarter
    # instead of falling through to a third-party snapshot. The integrity gate still marks it stale.
    if not found:
        ey,eq,_=expected_latest_financial_period(date.today())
        y,q=ey,eq
        for _ in range(3):
            q-=1
            if q==0: y-=1; q=4
            try:
                prior=await fetch_mops_historical_eps(ticker,y,q)
                if prior:
                    prior["latest_expected_period"]=f"{ey} Q{eq}"
                    prior["current_period_status"]="not_published_or_unavailable"
                    found.append(prior)
                    break
            except Exception as e:
                errors.append(f"mops_history_{y}q{q}:{type(e).__name__}")
    if found:
        # Latest fiscal period always wins. Within the same period prefer the detailed row with more parsed fields.
        found.sort(key=lambda x: (x.get("fiscal_year") or 0, x.get("fiscal_quarter") or 0,
                                  x.get("completeness") or 0, 1 if x.get("feed_kind")=="detail" else 0,
                                  x.get("statement_date") or ""), reverse=True)
        best=found[0]
        latest_key=(best.get("fiscal_year"),best.get("fiscal_quarter"))
        same=[x for x in found if (x.get("fiscal_year"),x.get("fiscal_quarter"))==latest_key]
        # Merge fields across official feeds for the same period; e.g. daily EPS summary can anchor Q2 while
        # a detailed feed supplies gross profit/margins. Never merge across different fiscal periods.
        merged=dict(best)
        for row in same:
            for k in ("ytd_eps","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd","statement_date",
                      "quarter_eps_direct","gross_margin_direct","operating_margin_direct"):
                if merged.get(k) is None and row.get(k) is not None:
                    merged[k]=row[k]
        merged["source_candidates"]=[{"source":x.get("source"),"endpoint":x.get("endpoint"),"kind":x.get("feed_kind"),"completeness":x.get("completeness")} for x in same]
        merged["errors"]=errors; merged["candidate_hits"]=len(found)
        return merged
    return {"official":False,"errors":errors,"candidate_hits":0}



async def diagnose_official_financial_sources(ticker: str) -> dict[str, Any]:
    """Live diagnostic of every official-financial source used by V5.2.4.

    This endpoint intentionally exposes metadata/status only (no secrets). It is designed to answer:
    * did the upstream endpoint respond?
    * did it contain the requested ticker?
    * what fiscal period did it report?
    * for company IR, did the page advertise the expected quarter and did it expose a PDF link?
    * if a PDF was found, did its text explicitly validate the expected period and parse the core fields?
    """
    now=datetime.now().astimezone()
    ey,eq,expected=expected_latest_financial_period(now.date())
    ua={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.3.2 diagnostics"}
    out={
        "ticker":ticker, "generated_at":now.isoformat(timespec="seconds"),
        "expected_period":expected, "expected_year":ey, "expected_quarter":eq,
        "company_mops":[], "openapi":[], "mops_csv":[], "mops_material":[], "company_ir":None, "selection":None,
        "diagnostic_version":"5.2.13",
    }

    def company_code(row: dict[str, Any]) -> str:
        direct=row.get("公司代號") or row.get("SecuritiesCompanyCode") or row.get("公司代碼") or row.get("代號")
        if direct is not None:
            return re.sub(r"\D","",str(direct)) or str(direct).strip()
        v=_row_value(row,["公司","代號"]) or _row_value(row,["代號"])
        return str(v or "").strip()

    industry_suffixes=["ci","mim","basi","bd","fh","ins"]
    candidates=[
        (TWSE_OPENAPI, "/opendata/t187ap14_L", "TWSE/MOPS EPS Daily Summary", "summary"),
        (TPEX_OPENAPI, "/mopsfin_t187ap14_O", "TPEx/MOPS EPS Daily Summary", "summary"),
    ]
    for suffix in industry_suffixes:
        candidates += [
            (TWSE_OPENAPI, f"/opendata/t187ap06_L_{suffix}", "TWSE/MOPS Income Statement", "detail"),
            (TWSE_OPENAPI, f"/opendata/t187ap06_X_{suffix}", "TWSE/MOPS X/Foreign Income Statement", "detail"),
            (TPEX_OPENAPI, f"/mopsfin_t187ap06_O_{suffix}", "TPEx/MOPS Income Statement", "detail"),
        ]

    # Company-specific MOPS IFRS diagnostics (the primary V5.2.5 source).
    try:
        direct=await fetch_mops_company_ifrs(ticker,ey,eq)
        out["company_mops"]=[{k:x.get(k) for k in ("source","endpoint","period","fiscal_year","fiscal_quarter","report_id","selected_column","ytd_eps","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd","completeness","table_count")} for x in direct]
    except Exception as e:
        out["company_mops_error"]=f"{type(e).__name__}: {str(e)[:240]}"

    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=ua) as client:
        for base,path,source,kind in candidates:
            item={"source":source,"url":base+path,"kind":kind}
            try:
                r=await client.get(base+path)
                item.update({"http_status":r.status_code,"content_type":r.headers.get("content-type"),"bytes":len(r.content)})
                r.raise_for_status()
                data=r.json()
                rows=data if isinstance(data,list) else []
                item["rows_count"]=len(rows)
                row=next((x for x in rows if company_code(x)==ticker),None)
                item["matched"]=bool(row)
                if row:
                    snap=_official_row_to_snapshot(row,source,path,"official",kind)
                    item["snapshot"]={k:snap.get(k) for k in ("period","fiscal_year","fiscal_quarter","statement_date","ytd_eps","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd","completeness")} if snap else None
                    item["raw_keys"]=list(row.keys())[:40]
            except Exception as e:
                item["error"]=f"{type(e).__name__}: {str(e)[:240]}"
            out["openapi"].append(item)

        csv_files=[
            ("t187ap14_L.csv","TWSE/MOPS EPS CSV","summary"),
            ("t187ap06_L_ci.csv","TWSE/MOPS Income CSV","detail"),
            ("t187ap06_L_mim.csv","TWSE/MOPS Income CSV","detail"),
            ("t187ap06_X_ci.csv","TWSE/MOPS Public/Foreign Income CSV","detail"),
            ("t187ap06_X_mim.csv","TWSE/MOPS Public/Foreign Income CSV","detail"),
        ]
        for filename,source,kind in csv_files:
            url=f"{MOPS_CSV_BASE}/{filename}"
            item={"source":source,"url":url,"kind":kind}
            try:
                r=await client.get(url)
                item.update({"http_status":r.status_code,"content_type":r.headers.get("content-type"),"bytes":len(r.content)})
                r.raise_for_status()
                text=None; encoding=None
                for enc in ("utf-8-sig","utf-8","cp950","big5"):
                    try: text=r.content.decode(enc); encoding=enc; break
                    except Exception: pass
                if text is None: raise ValueError("CSV decode failed")
                rows=[dict(x) for x in csv.DictReader(io.StringIO(text))]
                item.update({"encoding":encoding,"rows_count":len(rows),"header":list(rows[0].keys())[:40] if rows else []})
                matched=None; snap=None
                for row in rows:
                    candidate=_official_row_to_snapshot(row,source,filename,"official",kind)
                    if candidate and candidate.get("company_code")==ticker:
                        matched=row; snap=candidate; break
                item["matched"]=bool(matched)
                if snap:
                    item["snapshot"]={k:snap.get(k) for k in ("period","fiscal_year","fiscal_quarter","statement_date","ytd_eps","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd","completeness")}
            except Exception as e:
                item["error"]=f"{type(e).__name__}: {str(e)[:240]}"
            out["mops_csv"].append(item)

        # Official material-information fallback diagnostics.
        try:
            mats=await fetch_mops_material_financial(ticker,ey,eq)
            out["mops_material"]=[{k:x.get(k) for k in ("period","fiscal_year","fiscal_quarter","statement_date","ytd_eps","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd","completeness","subject","source")} for x in mats]
        except Exception as e:
            out["mops_material_error"]=f"{type(e).__name__}: {str(e)[:240]}"

        page=IR_FINANCIAL_PAGES.get(ticker)
        if page:
            ir={"page_url":page,"expected_period":expected,"pdf_candidates":[]}
            try:
                r=await client.get(page)
                html_text=r.text
                ir.update({"http_status":r.status_code,"final_url":str(r.url),"content_type":r.headers.get("content-type"),"bytes":len(r.content)})
                r.raise_for_status()
                compact_html=re.sub(r"\s+"," ",html_text)
                q_tokens={1:["Q1","First Quarter","第一季度","第一季"],2:["Q2","Second Quarter","第二季度","第二季"],3:["Q3","Third Quarter","第三季度","第三季"],4:["Q4","Fourth Quarter","第四季度","第四季"]}[eq]
                ir["expected_year_visible"]=str(ey) in html_text
                ir["expected_quarter_label_visible"]=any(t.lower() in html_text.lower() for t in q_tokens)
                year_pos=compact_html.find(str(ey))
                ir["html_context"]=compact_html[max(0,year_pos-160):year_pos+700] if year_pos>=0 else compact_html[:700]
                hrefs=re.findall(r'href=["\']([^"\']+)["\']',html_text,re.I)
                embedded=re.findall(r'["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',html_text,re.I)
                pdfs=[]
                for href in list(dict.fromkeys(hrefs+embedded)):
                    if ".pdf" not in href.lower(): continue
                    url=urljoin(str(r.url),href); lo=url.lower(); score=0
                    if str(ey) in lo: score+=4
                    if f"q{eq}" in lo or f"_{eq}_" in lo: score+=5
                    pdfs.append((score,url))
                pdfs=sorted(set(pdfs),reverse=True)[:16]
                ir["pdf_link_count"]=len(pdfs)
                ir["expected_q_pdf_link_detected"]=any(score>=5 for score,_ in pdfs)
                for score,url in pdfs[:8]:
                    pi={"url":url,"score":score}
                    try:
                        pr=await client.get(url)
                        pi.update({"http_status":pr.status_code,"content_type":pr.headers.get("content-type"),"bytes":len(pr.content)})
                        pr.raise_for_status()
                        reader=PdfReader(io.BytesIO(pr.content))
                        text="\n".join((pg.extract_text() or "") for pg in reader.pages[:12])
                        low=text.lower()
                        if eq==2: period_ok=(str(ey) in text and ("six months ended june 30" in low or "六月三十日" in text or "6月30日" in text))
                        elif eq==1: period_ok=(str(ey) in text and ("three months ended march 31" in low or "三月三十一日" in text or "3月31日" in text))
                        elif eq==3: period_ok=(str(ey) in text and ("nine months ended september 30" in low or "九月三十日" in text or "9月30日" in text))
                        else: period_ok=(str(ey) in text and ("year ended december 31" in low or "十二月三十一日" in text or "12月31日" in text))
                        pi.update({"pages":len(reader.pages),"text_chars_first12":len(text),"period_ok":period_ok})
                        if period_ok: pi["parsed"]=_extract_pdf_financials(text,ey,eq)
                    except Exception as e:
                        pi["error"]=f"{type(e).__name__}: {str(e)[:240]}"
                    ir["pdf_candidates"].append(pi)
            except Exception as e:
                ir["error"]=f"{type(e).__name__}: {str(e)[:240]}"
            out["company_ir"]=ir

    try:
        selected=await fetch_official_income_statement(ticker)
        out["selection"]={k:selected.get(k) for k in ("official","source","endpoint","period","fiscal_year","fiscal_quarter","statement_date","ytd_eps","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd","candidate_hits","errors")}
    except Exception as e:
        out["selection"]={"error":f"{type(e).__name__}: {str(e)[:240]}"}

    # concise machine-readable conclusion for screenshots/support
    matched_open=[x for x in out["openapi"] if x.get("matched")]
    matched_csv=[x for x in out["mops_csv"] if x.get("matched")]
    ir=out.get("company_ir") or {}
    valid_pdf=[x for x in ir.get("pdf_candidates",[]) if x.get("period_ok")]
    out["summary"]={
        "company_mops_hits":len(out.get("company_mops") or []), "openapi_hits":len(matched_open), "csv_hits":len(matched_csv), "material_financial_hits":len(out.get("mops_material") or []), "valid_ir_pdfs":len(valid_pdf),
        "ir_expected_label_visible":ir.get("expected_quarter_label_visible"),
        "ir_expected_pdf_link_detected":ir.get("expected_q_pdf_link_detected"),
        "selected_period":(out.get("selection") or {}).get("period"),
        "selected_source":(out.get("selection") or {}).get("source"),
    }
    return out


async def reconcile_official_financial_snapshot(ticker: str, selected: dict[str, Any]) -> dict[str, Any]:
    """Final safety reconciliation before data reaches /api/stock.

    Diagnostics proved that official MOPS CSV can already contain a newer quarter even when
    another upstream path or stale structured source wins earlier in the request. This helper
    independently re-checks the official CSV and forces the latest official fiscal period into
    the main stock payload. Fields are merged only within the same fiscal period.
    """
    candidates: list[dict[str, Any]] = []
    if selected and selected.get("official"):
        candidates.append(dict(selected))
    try:
        candidates.extend(await fetch_mops_csv_official(ticker))
    except Exception:
        pass
    # Re-probe the exact expected quarter through the generic official resolver. This gives a
    # transiently failed MOPS/OpenAPI request a second independent chance and prevents a valid
    # Q2 filing from being hidden merely because an older structured snapshot was also available.
    try:
        expected_year,expected_quarter,_=expected_latest_financial_period(date.today())
        expected=await fetch_official_eps_for_period(ticker,expected_year,expected_quarter)
        if expected:
            candidates.append(expected)
    except Exception:
        pass
    if not candidates:
        return selected or {"official": False, "candidate_hits": 0, "errors": ["reconcile:no_official_candidate"]}

    def key(x: dict[str, Any]):
        return (int(x.get("fiscal_year") or 0), int(x.get("fiscal_quarter") or 0))
    latest_key=max(key(x) for x in candidates)
    latest=[x for x in candidates if key(x)==latest_key]
    latest.sort(key=lambda x:(x.get("completeness") or 0, 1 if x.get("feed_kind")=="detail" else 0, x.get("statement_date") or ""), reverse=True)
    merged=dict(latest[0])
    for row in latest[1:]:
        for field in ("ytd_eps","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd",
                      "quarter_eps_direct","gross_margin_direct","operating_margin_direct","statement_date"):
            if merged.get(field) is None and row.get(field) is not None:
                merged[field]=row[field]
    merged["official"]=True
    merged["period"]=f"{latest_key[0]} Q{latest_key[1]}" if all(latest_key) else merged.get("period")
    merged["mapping_reconciled"]=True
    merged["mapping_sources"]=[{"source":x.get("source"),"endpoint":x.get("endpoint"),"period":x.get("period"),"completeness":x.get("completeness")} for x in latest]
    return merged

def expected_latest_financial_period(as_of: date | None=None) -> tuple[int,int,str]:
    """Conservative Taiwan quarterly filing calendar used only as a freshness gate.

    It does not invent financial values. It decides whether the latest fetched period is old enough
    that the UI/valuation must warn or block stale accounting data.
    """
    d=as_of or date.today(); y=d.year
    md=(d.month,d.day)
    if md >= (11,14): return y,3,f"{y} Q3"
    if md >= (8,14): return y,2,f"{y} Q2"
    if md >= (5,15): return y,1,f"{y} Q1"
    if md >= (3,31): return y-1,4,f"{y-1} Q4"
    return y-1,3,f"{y-1} Q3"


def assess_financial_integrity(official: dict[str, Any], eps_stack: dict[str, Any], as_of: date | None=None) -> dict[str, Any]:
    ey,eq,expected=expected_latest_financial_period(as_of)
    expected_key=(ey,eq)
    oy,oq=official.get("fiscal_year"),official.get("fiscal_quarter")
    official_key=(oy,oq) if oy and oq else None
    api_period=eps_stack.get("structured_api_period")
    am=re.match(r"(\d{4}) Q([1-4])",str(api_period or ""))
    api_key=(int(am.group(1)),int(am.group(2))) if am else None
    verified=bool(official.get("official") and official_key and official_key>=expected_key)
    official_stale=bool(official.get("official") and official_key and official_key<expected_key)
    api_stale=bool(api_key and api_key<expected_key)
    eps_ready=verified and (eps_stack.get("ytd_eps") is not None or eps_stack.get("quarter_eps") is not None)
    if verified and eps_ready:
        status="verified"; severity="ok"; message=f"官方最新財報期已驗證：{official.get('period')}"
    elif verified:
        status="verified_period_missing_eps"; severity="warning"; message=f"官方期間已達 {official.get('period')}，但 EPS 欄位尚未可靠解析；核心財報估值暫停。"
    elif official_stale:
        status="official_stale"; severity="stale"; message=f"官方介面目前僅取得 {official.get('period')}，低於應有 {expected}；不得標示為最新。"
    else:
        status="unverified"; severity="stale"; message=f"尚未從官方介面驗證 {expected}；結構化 API 財報不得視為最新。"
    return {"status":status,"severity":severity,"expected_period":expected,
            "official_period":official.get("period"),"structured_api_period":api_period,
            "official_verified":verified,"official_stale":official_stale,"structured_api_stale":api_stale,
            "core_financials_allowed":eps_ready,"market_per_is_independent":True,"message":message,
            "rule":"只有官方期間達到應有季度且至少能解析 YTD EPS 或官方單季 EPS，財報 EPS 才能進核心估值；法人明確年度 Forward EPS 可獨立使用。"}

def _quarter_from_date(v: Any) -> tuple[int|None,int|None]:
    try:
        d=pd.to_datetime(v); return int(d.year), int((d.month-1)//3+1)
    except Exception: return None,None

def finmind_eps_history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    df=pd.DataFrame(rows)
    if df.empty or not {"date","type","value"}.issubset(df.columns): return []
    df["value"]=pd.to_numeric(df["value"],errors="coerce"); df=df.dropna(subset=["value"])
    mask=df["type"].astype(str).str.contains("BasicEarningsPerShare|EarningsPerShare|基本每股盈餘",case=False,regex=True)
    e=df[mask].copy()
    out=[]
    for dt,g in e.groupby("date"):
        y,q=_quarter_from_date(dt); val=safe_num(g.iloc[0]["value"]);
        if y and q and val is not None: out.append({"date":str(dt),"year":y,"quarter":q,"ytd_eps":val,"source":"FinMind"})
    return sorted(out,key=lambda x:x["date"])

def _best_period_snapshot(rows: list[dict[str, Any]], year: int, quarter: int) -> dict[str, Any] | None:
    candidates=[r for r in rows if r.get("official") and r.get("fiscal_year")==year and r.get("fiscal_quarter")==quarter]
    if not candidates: return None
    candidates.sort(key=lambda r:(r.get("completeness") or 0, r.get("statement_date") or ""), reverse=True)
    best=dict(candidates[0])
    for r in candidates[1:]:
        for k in ("ytd_eps","quarter_eps_direct","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd"):
            if best.get(k) is None and r.get(k) is not None: best[k]=r[k]
    return best


BUILTIN_OFFICIAL_EPS_REGISTRY = {
    "schema_version": "1.1",
    "description": "Built-in verified official EPS seed for resilient cloud deployment. File-based registry may extend/override these records.",
    "updated_at": "2026-08-15",
    "records": [
        {"ticker":"2330","year":2025,"quarter":2,"quarter_eps":15.36,"source":"TSMC Press Center","source_url":"https://pr.tsmc.com/chinese/news/3249","evidence_kind":"company_official_quarter_eps","verified":True},
        {"ticker":"2330","year":2025,"quarter":3,"quarter_eps":17.44,"source":"TSMC Press Center","source_url":"https://pr.tsmc.com/chinese/news/3264","evidence_kind":"company_official_quarter_eps","verified":True},
        {"ticker":"2330","year":2025,"quarter":4,"quarter_eps":19.50,"source":"TSMC Press Center","source_url":"https://pr.tsmc.com/chinese/news/3281","evidence_kind":"company_official_quarter_eps","verified":True},
        {"ticker":"2330","year":2026,"quarter":1,"quarter_eps":22.08,"source":"TSMC Press Center","source_url":"https://pr.tsmc.com/chinese/news/3297","evidence_kind":"company_official_quarter_eps","verified":True},
        {"ticker":"2330","year":2026,"quarter":2,"quarter_eps":27.25,"source":"TSMC Press Center","source_url":"https://pr.tsmc.com/chinese/news/3326","evidence_kind":"company_official_quarter_eps","verified":True},
        {"ticker":"3661","year":2025,"quarter":2,"quarter_eps":16.37,"ytd_eps":34.49,"source":"Alchip reviewed financial report","source_url":"https://www.alchip.com/upload/2025_08_18/3_20250818101142i3cka8jyk0.pdf","evidence_kind":"company_official_financial_pdf","verified":True},
        {"ticker":"3661","year":2025,"quarter":3,"quarter_eps":16.40,"ytd_eps":50.89,"source":"Alchip reviewed financial report","source_url":"https://www.alchip.com/upload/2025_11_12/3_20251112141146lr7eqkVGQ0.pdf","evidence_kind":"company_official_financial_pdf","verified":True},
        {"ticker":"3661","year":2025,"quarter":4,"quarter_eps":18.30,"fy_eps":69.18,"source":"Alchip official FY2025 results","source_url":"https://www.alchip.com/en/Newsroom/Alchip_2025_Q4_financial_results","evidence_kind":"company_official_quarter_eps","verified":True},
        {"ticker":"3661","year":2026,"quarter":1,"quarter_eps":17.55,"ytd_eps":17.55,"source":"Alchip reviewed financial report","source_url":"https://www.alchip.com/upload/2026_05_11/3_20260511103038sgoopzYTC0.pdf","evidence_kind":"company_official_financial_pdf","verified":True}
    ]
}

OFFICIAL_EPS_REGISTRY_PATHS = [
    Path(__file__).resolve().parent / "data" / "official_eps_registry.json",
    Path(__file__).resolve().parent / "official_eps_registry.json",
]

def _load_official_eps_registry() -> dict[str, Any]:
    """Load the verified EPS registry with a built-in cloud-safe seed.

    GitHub/mobile uploads can accidentally omit nested data files. V5.3.1 therefore ships a
    minimal verified registry inside server.py and merges any file-based registry on top.
    File rows override built-ins for the same (ticker, year, quarter).
    """
    merged={"schema_version":"1.1","description":"Merged built-in + file official EPS registry","updated_at":"2026-08-15","records":[]}
    by_key={}
    for row in BUILTIN_OFFICIAL_EPS_REGISTRY.get("records",[]):
        if row.get("verified") is True:
            by_key[(str(row.get("ticker")),int(row.get("year")),int(row.get("quarter")))]=dict(row)
    loaded_paths=[]; load_errors=[]
    for registry_path in OFFICIAL_EPS_REGISTRY_PATHS:
        if not registry_path.exists(): continue
        try:
            raw=json.loads(registry_path.read_text(encoding="utf-8"))
            if isinstance(raw,dict):
                for row in raw.get("records",[]):
                    try:
                        if row.get("verified") is True:
                            key=(str(row.get("ticker")),int(row.get("year")),int(row.get("quarter")))
                            by_key[key]=dict(row)
                    except Exception:
                        continue
                merged["file_schema_version"]=raw.get("schema_version")
                merged["file_updated_at"]=raw.get("updated_at")
                loaded_paths.append(registry_path.name)
        except Exception as exc:
            load_errors.append(f"{registry_path.name}:{type(exc).__name__}")
    if loaded_paths: merged["file_paths"]=loaded_paths
    if load_errors: merged["file_load_errors"]=load_errors
    merged["records"]=list(by_key.values())
    merged["records"].sort(key=lambda r:(str(r.get("ticker")),int(r.get("year",0)),int(r.get("quarter",0))))
    merged["record_count"]=len(merged["records"])
    merged["builtin_count"]=len(BUILTIN_OFFICIAL_EPS_REGISTRY.get("records",[]))
    return merged

def registry_eps_for_period(ticker: str, year: int, quarter: int) -> dict[str, Any] | None:
    """Return a verified company-official EPS registry record for one fiscal quarter.

    Registry entries are intentionally small, auditable evidence records. They are used before
    network historical backfill because Render->legacy MOPS history can be blocked. Missing periods
    still fall through to live official resolvers; third-party values are never promoted into this
    registry automatically.
    """
    reg=_load_official_eps_registry()
    for row in reg.get("records",[]):
        if str(row.get("ticker"))==str(ticker) and row.get("year")==year and row.get("quarter")==quarter and row.get("verified") is True:
            qeps=safe_num(row.get("quarter_eps")); ytd=safe_num(row.get("ytd_eps"))
            if quarter==1 and ytd is None and qeps is not None: ytd=qeps
            return {
                "source":row.get("source") or "Official EPS Registry",
                "market":"Company Official Registry","endpoint":row.get("source_url"),"official":True,
                "period":f"{year} Q{quarter}","fiscal_year":year,"fiscal_quarter":quarter,
                "statement_date":row.get("verified_date"),"feed_kind":"official_eps_registry",
                "company_code":str(ticker),"quarter_eps_direct":qeps,"ytd_eps":ytd,
                "eps_provenance":"official_registry_verified","eps_confidence":100,
                "registry_verified":True,"registry_schema_version":reg.get("schema_version"),
                "evidence_kind":row.get("evidence_kind"),"completeness":sum(v is not None for v in (qeps,ytd))
            }
    return None

async def fetch_official_eps_for_period(ticker: str, year: int, quarter: int) -> dict[str, Any] | None:
    """V5.2.13 multi-source official EPS resolver for an exact fiscal period.

    Priority:
      1) official structured MOPS/TWSE data when it exactly matches the requested period;
      2) official MOPS historical aggregate statement;
      3) company official IR / reviewed financial report;
      4) official MOPS board-approved disclosure;
      5) return None. Third-party/FinMind values never fill an official predecessor quarter.
    """
    # Layer 0: verified company-official registry. This avoids re-scraping historical pages on every request
    # and makes evidence auditable/stable. Registry rows always carry their original official URL.
    reg=registry_eps_for_period(ticker,year,quarter)
    if reg: return reg

    # Layer 1: official structured CSV/OpenData. These feeds often expose only the latest quarter,
    # but if it exactly matches the requested period it is the preferred cumulative source.
    try:
        rows=await fetch_mops_csv_official(ticker)
        best=_best_period_snapshot(rows,year,quarter)
        if best and (best.get("ytd_eps") is not None or best.get("quarter_eps_direct") is not None):
            best=dict(best); best["eps_provenance"]="official_structured"; best["eps_confidence"]=100
            return best
    except Exception:
        pass

    # Layer 2: official historical aggregate. This supplies the prior cumulative quarter required
    # to derive Q2/Q3/Q4 standalone EPS for any listed/OTC company, without per-ticker hard-coding.
    try:
        historical=await fetch_mops_historical_eps(ticker,year,quarter)
        if historical and (historical.get("ytd_eps") is not None or historical.get("quarter_eps_direct") is not None):
            return historical
    except Exception:
        pass

    # Layer 3: company official IR. TSMC publishes a direct quarter EPS; mapped issuers such as
    # Alchip are parsed from their reviewed/audited IR PDF when an explicit period match exists.
    if ticker=="2330":
        try:
            ir=await fetch_tsmc_quarterly_release(year,quarter)
            if ir and ir.get("quarter_eps_direct") is not None: return ir
        except Exception:
            pass
    try:
        ir=await fetch_company_ir_financial(ticker,year,quarter)
        if ir and (ir.get("ytd_eps") is not None or ir.get("quarter_eps_direct") is not None):
            ir=dict(ir); ir["eps_provenance"]="company_official_report"; ir["eps_confidence"]=98
            return ir
    except Exception:
        pass

    # Layer 4: official board-approved disclosure, if it contains an exact period and EPS.
    try:
        mats=await fetch_mops_material_financial(ticker,year,quarter)
        mb=_best_period_snapshot(mats,year,quarter)
        if mb and (mb.get("ytd_eps") is not None or mb.get("quarter_eps_direct") is not None):
            mb=dict(mb); mb["eps_provenance"]="official_material_disclosure"; mb["eps_confidence"]=96
            return mb
    except Exception:
        pass
    return None

# Backward-compatible name for diagnostics/older call sites. Production behavior is the new resolver.
async def fetch_official_eps_ytd_for_period(ticker: str, year: int, quarter: int) -> dict[str, Any] | None:
    return await fetch_official_eps_for_period(ticker,year,quarter)

async def build_eps_stack(ticker: str, fin_rows: list[dict[str, Any]], official: dict[str, Any], fallback_financial: dict[str, Any]) -> dict[str, Any]:
    """V5.2.13 multi-source EPS engine with explicit provenance.

    Official current-period data is never combined with a third-party predecessor. Quarter EPS is
    either (a) a direct company-official value, or (b) derived from two official cumulative values.
    TTM is reported only when four real quarters are available.
    """
    fin_hist=finmind_eps_history(fin_rows)
    fy=official.get("fiscal_year") if official.get("official") else None
    fq=official.get("fiscal_quarter") if official.get("official") else None
    ytd=official.get("ytd_eps") if official.get("official") else None
    source=official.get("source") if official.get("official") else "FinMind"

    # If no official period exists at all, preserve the old structured-data fallback, clearly labeled.
    if (not fy or not fq) and fin_hist:
        last=fin_hist[-1]; fy,fq,ytd=last["year"],last["quarter"],last["ytd_eps"]; source=last["source"]

    official_ytd: dict[tuple[int,int],float]={}
    direct_quarter: dict[tuple[int,int],float]={}
    provenance: dict[tuple[int,int],dict[str,Any]]={}
    lookup_diagnostics=[]

    if fy and fq and official.get("official"):
        cur_key=(fy,fq)
        if ytd is not None:
            official_ytd[cur_key]=float(ytd)
            provenance[cur_key]={"method":"official_ytd","source":official.get("source"),"endpoint":official.get("endpoint"),"confidence":100}
        if official.get("quarter_eps_direct") is not None:
            direct_quarter[cur_key]=float(official.get("quarter_eps_direct"))
            provenance[cur_key]={"method":"official_direct","source":official.get("source"),"endpoint":official.get("endpoint"),"confidence":100}

        # Registry/current-company evidence wins for direct quarter EPS when the selected accounting feed has only YTD.
        if cur_key not in direct_quarter:
            reg_cur=registry_eps_for_period(ticker,fy,fq)
            if reg_cur and reg_cur.get("quarter_eps_direct") is not None:
                direct_quarter[cur_key]=float(reg_cur.get("quarter_eps_direct"))
                provenance[cur_key]={"method":"official_registry_verified","source":reg_cur.get("source"),"endpoint":reg_cur.get("endpoint"),"confidence":100}
                if fq==1 and cur_key not in official_ytd:
                    official_ytd[cur_key]=float(reg_cur.get("quarter_eps_direct"))

        # Re-check company IR for the current quarter when the selected accounting feed has only YTD.
        if cur_key not in direct_quarter:
            try:
                cur_ir=await fetch_official_eps_for_period(ticker,fy,fq)
                if cur_ir:
                    if cur_ir.get("quarter_eps_direct") is not None:
                        direct_quarter[cur_key]=float(cur_ir.get("quarter_eps_direct"))
                        provenance[cur_key]={"method":"official_direct","source":cur_ir.get("source"),"endpoint":cur_ir.get("endpoint"),"confidence":cur_ir.get("eps_confidence",98)}
                        if fq==1 and cur_key not in official_ytd:
                            official_ytd[cur_key]=float(cur_ir.get("quarter_eps_direct"))
                    if cur_key not in official_ytd and cur_ir.get("ytd_eps") is not None:
                        official_ytd[cur_key]=float(cur_ir.get("ytd_eps"))
            except Exception:
                pass

        periods=[]; y,q=fy,fq
        for _ in range(5):
            periods.append((y,q)); q-=1
            if q==0: y-=1; q=4
        tasks=[fetch_official_eps_for_period(ticker,y,q) for (y,q) in periods[1:]]
        rows=await asyncio.gather(*tasks, return_exceptions=True)
        for (yq,row) in zip(periods[1:],rows):
            if isinstance(row,Exception):
                lookup_diagnostics.append({"period":f"{yq[0]} Q{yq[1]}","status":"error","error":type(row).__name__}); continue
            if not row:
                lookup_diagnostics.append({"period":f"{yq[0]} Q{yq[1]}","status":"missing_official"}); continue
            if row.get("ytd_eps") is not None:
                official_ytd[yq]=float(row.get("ytd_eps"))
            # For Q1, an official direct-quarter EPS is also the official cumulative YTD EPS.
            if yq[1]==1 and row.get("ytd_eps") is None and row.get("quarter_eps_direct") is not None:
                official_ytd[yq]=float(row.get("quarter_eps_direct"))
            if row.get("quarter_eps_direct") is not None: direct_quarter[yq]=float(row.get("quarter_eps_direct"))
            row_method="official_registry_verified" if row.get("eps_provenance")=="official_registry_verified" else ("official_direct" if row.get("quarter_eps_direct") is not None else "official_ytd")
            provenance[yq]={"method":row_method,
                            "source":row.get("source"),"endpoint":row.get("endpoint"),"confidence":row.get("eps_confidence",98)}
            lookup_diagnostics.append({"period":f"{yq[0]} Q{yq[1]}","status":"ok",
                "source":row.get("source"),"endpoint":row.get("endpoint"),"ytd_eps":row.get("ytd_eps"),
                "quarter_eps_direct":row.get("quarter_eps_direct"),"eps_provenance":row.get("eps_provenance"),
                "confidence":row.get("eps_confidence")})

    # Third-party history may be shown diagnostically, but never used to derive an official quarter.
    fin_map={(x["year"],x["quarter"]):float(x["ytd_eps"]) for x in fin_hist}

    def standalone(year:int, quarter:int):
        key=(year,quarter)
        if key in direct_quarter:
            p=provenance.get(key,{})
            method="official_registry_verified" if p.get("method")=="official_registry_verified" else "official_direct"
            return direct_quarter[key], method, p
        cur=official_ytd.get(key)
        if cur is None: return None,None,None
        if quarter==1:
            p=provenance.get(key,{})
            return cur,"official_ytd_q1",p
        prev=official_ytd.get((year,quarter-1))
        if prev is None: return None,None,None
        current_p=provenance.get(key,{})
        prior_p=provenance.get((year,quarter-1),{})
        p={**current_p,"derivation_inputs":{
            "current":{"period":f"{year} Q{quarter}","ytd_eps":cur,"source":current_p.get("source"),"endpoint":current_p.get("endpoint")},
            "prior":{"period":f"{year} Q{quarter-1}","ytd_eps":prev,"source":prior_p.get("source"),"endpoint":prior_p.get("endpoint")},
        }}
        return round(cur-prev,4),"official_ytd_difference",p

    quarter_eps=None; quarter_method=None; quarter_meta=None
    if fy and fq:
        if official.get("official"):
            quarter_eps,quarter_method,quarter_meta=standalone(fy,fq)
        elif ytd is not None:
            # Structured fallback only when no official period exists.
            if fq==1: quarter_eps=ytd; quarter_method="structured_api_q1"
            elif (fy,fq-1) in fin_map: quarter_eps=ytd-fin_map[(fy,fq-1)]; quarter_method="structured_api_difference"

    quarters=[]
    if fy and fq and official.get("official"):
        y,q=fy,fq; last4=[]
        for _ in range(4):
            last4.append((y,q)); q-=1
            if q==0: y-=1; q=4
        for y,q in reversed(last4):
            val,method,meta=standalone(y,q)
            if val is not None:
                quarters.append({"year":y,"quarter":q,"eps":val,"period":f"{y} Q{q}","method":method,"source":(meta or {}).get("source")})
    ttm=sum(x["eps"] for x in quarters) if len(quarters)==4 else None

    latest_period=f"{fy} Q{fq}" if fy and fq else fallback_financial.get("statement_date")
    api_latest=(fin_hist[-1]["year"],fin_hist[-1]["quarter"]) if fin_hist else (None,None)
    official_key=(fy,fq) if fy and fq and official.get("official") else (None,None)
    stale_api=bool(official_key[0] and api_latest[0] and official_key>api_latest)
    prev_key=(fy,fq-1) if fy and fq and fq>1 else ((fy-1,4) if fy and fq else None)
    # V5.2.15 Historical EPS Backfill: every period carries a source/evidence state.
    # The calculation engine consumes only evidence marked official; missing evidence is explicit.
    evidence_ledger=[]
    if fy and fq:
        y,q=fy,fq
        ledger_periods=[]
        for _ in range(5):
            ledger_periods.append((y,q)); q-=1
            if q==0: y-=1; q=4
        diag_by_period={x.get("period"):x for x in lookup_diagnostics}
        for ly,lq in ledger_periods:
            key=(ly,lq); p=provenance.get(key,{})
            direct=direct_quarter.get(key); cumulative=official_ytd.get(key)
            single,method,_meta=standalone(ly,lq)
            diag=diag_by_period.get(f"{ly} Q{lq}",{})
            status="usable" if (direct is not None or cumulative is not None) else "missing"
            if direct is not None:
                evidence_type="quarter_eps_direct"
            elif cumulative is not None:
                evidence_type="ytd_eps"
            else:
                evidence_type="missing"
            evidence_ledger.append({
                "period":f"{ly} Q{lq}","year":ly,"quarter":lq,"status":status,
                "evidence_type":evidence_type,"quarter_eps_direct":direct,"ytd_eps":cumulative,
                "derived_quarter_eps":single,"derivation_method":method,
                "source":p.get("source") or diag.get("source"),
                "source_url":p.get("endpoint") or diag.get("endpoint"),
                "confidence":p.get("confidence") or diag.get("confidence"),
                "lookup_status":diag.get("status") or ("current" if key==(fy,fq) else "unknown"),
                "missing_reason":None if status=="usable" else (diag.get("error") or diag.get("status") or "no_official_evidence"),
                "registry_verified": bool(p.get("method")=="official_registry_verified"),
                "evidence_url": p.get("endpoint") or diag.get("endpoint")
            })

    q_label={"official_direct":"✅ 公司/官方單季值","official_registry_verified":"✅ 官方 EPS Registry","official_ytd_q1":"✅ 官方 Q1 累計=單季",
             "official_ytd_difference":("🧮 H1－Q1 回推，非公司單季公告" if fq==2 else "🧮 官方累計值差額回推，非公司單季公告"),
             "structured_api_q1":"△ 結構化 API","structured_api_difference":"△ 結構化 API 差額"}.get(quarter_method,"資料不足")
    return {
        "quarter_eps":quarter_eps,"quarter_period":latest_period,"quarter_method":quarter_method,"quarter_method_label":q_label,
        "quarter_source":(quarter_meta or {}).get("source") if quarter_meta else source,
        "quarter_source_url":(quarter_meta or {}).get("endpoint") if quarter_meta else None,
        "quarter_derivation_inputs":(quarter_meta or {}).get("derivation_inputs") if quarter_meta else None,
        "ytd_eps":ytd,"ytd_period":f"{fy} Q{fq} YTD" if fy and fq else latest_period,
        "ytd_method_label":"✅ 官方累計值" if official.get("official") and ytd is not None else ("△ 結構化 API" if ytd is not None else "資料不足"),
        "ttm_eps":ttm,"ttm_period":latest_period,"ttm_method_label":"✅ 四個實際單季加總" if ttm is not None else "四季官方單季資料不足",
        "source":source,"official_period":official.get("period"),"history":quarters,
        "quality":"multi_source_official_eps" if official.get("official") else "structured_api",
        "structured_api_stale":stale_api,"structured_api_period":f"{api_latest[0]} Q{api_latest[1]}" if api_latest[0] else None,
        "prior_ytd_period":f"{prev_key[0]} Q{prev_key[1]} YTD" if prev_key else None,
        "prior_ytd_eps":official_ytd.get(prev_key) if prev_key else None,
        "official_ytd_map":{f"{y} Q{q}":v for (y,q),v in sorted(official_ytd.items())},
        "eps_lookup_diagnostics":lookup_diagnostics,
        "evidence_ledger":evidence_ledger,
        "historical_backfill":{
            "attempted_periods":[x.get("period") for x in evidence_ledger[1:]],
            "resolved_periods":[x.get("period") for x in evidence_ledger[1:] if x.get("status")=="usable"],
            "missing_periods":[x.get("period") for x in evidence_ledger[1:] if x.get("status")!="usable"],
            "policy":"official_registry_then_structured_feed_then_mops_historical_then_company_ir_then_official_disclosure; no third-party EPS for official derivation",
        },
        "evidence_ledger_version":"5.3.5",
        "blocked_mops_html_removed":True,
        "blocked_mops_company_html_removed":True,
        "note":"歷史季度優先讀取可稽核的官方 Registry、TWSE/MOPS 結構化資料與 MOPS 歷史彙總表，再以公司 IR/官方揭露補抓。Q2-Q4 單季 EPS 僅由同年度官方累計值差額推導；缺資料留白，第三方 EPS 不參與官方推導。"
    }


ANALYST_ALIASES = {
    "摩根士丹利": ["摩根士丹利", "大摩", "Morgan Stanley"],
    "摩根大通": ["摩根大通", "小摩", "JPMorgan", "JP Morgan"],
    "高盛": ["高盛", "Goldman Sachs"],
    "花旗": ["花旗", "Citi", "Citigroup"],
    "瑞銀": ["瑞銀", "UBS"],
    "美銀": ["美銀", "Bank of America", "BofA"],
    "野村": ["野村", "Nomura"],
    "麥格理": ["麥格理", "Macquarie"],
    "匯豐": ["匯豐", "HSBC"],
    "里昂": ["里昂", "CLSA"],
    "元大": ["元大"], "凱基": ["凱基"], "富邦": ["富邦"], "永豐": ["永豐"],
    "國泰": ["國泰"], "群益": ["群益"], "統一": ["統一"], "元富": ["元富"],
}
RATING_MAP = [
    ("買進", ["買進", "Buy", "加碼", "優於大盤", "Overweight", "Outperform"]),
    ("中立", ["中立", "Neutral", "持有", "Hold", "Equal-weight", "Market Perform"]),
    ("賣出", ["賣出", "Sell", "減碼", "Underweight", "Underperform"]),
]

def _extract_institution(text: str) -> str | None:
    low=text.lower()
    for canonical, aliases in ANALYST_ALIASES.items():
        if any(a.lower() in low for a in aliases): return canonical
    return None

def _extract_rating(text: str) -> str | None:
    low=text.lower()
    for canonical, words in RATING_MAP:
        if any(w.lower() in low for w in words): return canonical
    return None

def _extract_target(text: str) -> float | None:
    patterns=[
        r"目標價(?:調升|上調|上看|調降|下調|降至|升至|至|為|看|[:：])?\s*(?:新台幣|NT\$?|TWD)?\s*([0-9]{2,5}(?:\.[0-9]+)?)",
        r"target price(?: raised| lowered| to| of|[:：])?\s*(?:NT\$?|TWD)?\s*([0-9]{2,5}(?:\.[0-9]+)?)",
    ]
    for pat in patterns:
        m=re.search(pat,text,re.I)
        if m:
            v=safe_num(m.group(1))
            if v and 5 <= v <= 100000: return v
    return None

def _extract_eps(text: str) -> float | None:
    pats=[r"(?:EPS|每股盈餘)[^0-9]{0,16}([0-9]{1,4}(?:\.[0-9]+)?)", r"([0-9]{1,4}(?:\.[0-9]+)?)\s*元[^。；,，]{0,8}(?:EPS|每股盈餘)"]
    for pat in pats:
        m=re.search(pat,text,re.I)
        if m:
            v=safe_num(m.group(1))
            if v and 0 < v < 5000: return v
    return None

def _extract_eps_year(text: str) -> int | None:
    # Only annual forward EPS with an explicit forecast year is eligible for valuation consensus.
    pats=[r"(20\d{2})(?:E|e|年|年度)?[^。；\n]{0,24}(?:EPS|每股盈餘)", r"(?:EPS|每股盈餘)[^。；\n]{0,24}(20\d{2})(?:E|e|年|年度)?"]
    for pat in pats:
        m=re.search(pat,text,re.I)
        if m:
            y=int(m.group(1))
            if date.today().year-1 <= y <= date.today().year+4: return y
    return None

def _normalize_title(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", s).lower()

def _company_event_identity(ticker: str, company_name: str, title: str) -> tuple[bool, str]:
    """Conservatively bind a public event result to one stock."""
    ticker=normalize_ticker(ticker)
    title_text=" ".join(str(title or "").split())
    title_key=_normalize_title(title_text)
    title_tickers=set(re.findall(r"(?<!\d)(\d{4,6})(?!\d)",title_text))
    if title_tickers and ticker not in title_tickers:
        return False,"different_ticker_in_title"
    if ticker in title_tickers:
        return True,"ticker_in_title"
    raw_name=str(company_name or "").strip()
    aliases={raw_name}
    for suffix in ("股份有限公司","有限公司","-KY","－KY","KY"):
        aliases.add(raw_name.replace(suffix,""))
    aliases={_normalize_title(x) for x in aliases if x}
    aliases={x for x in aliases if len(x)>=3}
    if any(alias in title_key for alias in aliases):
        return True,"company_name_in_title"
    return False,"target_identity_absent"

async def fetch_official_material_info(ticker: str, company_name: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch today's TWSE material disclosures and keep exact company-code matches only."""
    endpoint=TWSE_OPENAPI+"/opendata/t187ap04_L"
    errors=[]; rows=[]
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.3.5"}) as client:
            response=await client.get(endpoint); response.raise_for_status(); payload=response.json()
        for item in payload if isinstance(payload,list) else []:
            code=re.sub(r"\D","",_text_field(item,"公司代號","公司代碼"))
            if code != ticker: continue
            subject=_text_field(item,"主旨","主旨 ")
            description=_text_field(item,"說明")
            rows.append({
                "ticker":ticker,"company_name":_text_field(item,"公司名稱") or company_name,
                "date":roc_date_to_iso(_text_field(item,"發言日期") or _text_field(item,"出表日期")),
                "title":subject,"summary":description[:500],"summary_bullets":[],"management_outlook":[],
                "publisher":"公開資訊觀測站","source_url":endpoint,"tags":["重大訊息"],
                "event_type":"material_info","source_type":"official_mops_openapi",
                "identity_verified":True,"identity_method":"official_company_code",
                "summary_method":"official_disclosure","copyright_note":"官方重大訊息，依公司代號精確篩選。"
            })
    except Exception as exc:
        errors.append(f"official_material_info:{type(exc).__name__}")
    return rows,errors

async def google_news_rss(query: str) -> list[dict[str, Any]]:
    url=f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    async with httpx.AsyncClient(timeout=18, follow_redirects=True, headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.1"}) as client:
        r=await client.get(url); r.raise_for_status()
    root=ET.fromstring(r.text)
    out=[]
    for item in root.findall('.//item')[:30]:
        title=html.unescape(item.findtext('title') or '').strip()
        link=(item.findtext('link') or '').strip()
        desc=html.unescape(item.findtext('description') or '').strip()
        desc=re.sub('<[^>]+>',' ',desc)
        pub=item.findtext('pubDate') or ''
        try: pub_date=parsedate_to_datetime(pub).date().isoformat()
        except Exception: pub_date=''
        source_el=item.find('source'); source=(source_el.text or '').strip() if source_el is not None else ''
        out.append({"title":title,"url":link,"snippet":re.sub(r'\s+',' ',desc).strip()[:420],"published_date":pub_date,"publisher":source})
    return out

async def fetch_company_events(ticker: str, company_name: str) -> dict[str, Any]:
    """V5.3.4: keep earnings calls and material information as separate event streams.

    Public search snippets are summarized rather than copied verbatim. The latest three
    distinct earnings-call dates are exposed separately so the UI/PDF can show a compact
    management-history view.
    """
    queries=[
        f'"{company_name}" {ticker} 法說 法人說明會 展望 財報',
        f'"{company_name}" {ticker} earnings call investor conference',
        f'"{company_name}" {ticker} 重大訊息 公告',
        f'"{company_name}" {ticker} 董事會 重大訊息 營收 財報',
        f'"{company_name}" {ticker} 接單 產能 客戶 訂單',
    ]
    official_task=asyncio.create_task(fetch_official_material_info(ticker,company_name))
    results=await asyncio.gather(*(google_news_rss(q) for q in queries), return_exceptions=True)
    official_rows,official_errors=await official_task
    rows=[]; seen=set(); errors=[]
    for result in results:
        if isinstance(result,Exception):
            errors.append(type(result).__name__); continue
        for x in result:
            identity_ok,identity_method=_company_event_identity(ticker,company_name,x.get("title") or "")
            if not identity_ok: continue
            key=_normalize_title(x["title"])[:100]
            if not key or key in seen: continue
            seen.add(key)
            title=x["title"] or ""; snippet=x["snippet"] or ""
            text=f"{title} {snippet}"
            low=text.lower()

            tags=[]
            for tag,words in [
                ("法說",["法說","法人說明會","earnings call","investor conference","investor meeting"]),
                ("財報",["財報","獲利","eps","毛利率","營益率"]),
                ("營收",["營收"]),
                ("展望",["展望","上修","下修","看旺","看淡","guidance","outlook"]),
                ("重大訊息",["重大訊息","重大公告","重訊","公告"]),
                ("營運",["接單","產能","客戶","訂單","capex","資本支出"]),
            ]:
                if any(w.lower() in low for w in words): tags.append(tag)
            if not tags: continue

            is_call=("法說" in tags)
            is_material=("重大訊息" in tags) or any(w in text for w in ["董事會決議","處分","取得","增資","減資","發行","訴訟","合併","股利","除權息"])
            event_type="earnings_call" if is_call else ("material_info" if is_material else "company_update")

            # Concise bullet extraction from public title/snippet. No paid/full-text reproduction.
            raw_parts=re.split(r"[。；;｜|]|(?:\s+-\s+)", snippet)
            bullets=[]
            for part in raw_parts:
                p=" ".join(part.split()).strip(" -•")
                if len(p)<10: continue
                if p not in bullets:
                    bullets.append(p[:120])
                if len(bullets)>=4: break
            if not bullets:
                t=" ".join(title.split())
                if t: bullets=[t[:120]]

            outlook=[]
            for b in bullets:
                if any(w in b for w in ["展望","預期","看好","看旺","保守","需求","成長","下修","上修","guidance","outlook"]):
                    outlook.append(b)
            rows.append({
                "ticker":ticker,"company_name":company_name,
                "date":x["published_date"],"title":title,"summary":snippet[:300],
                "summary_bullets":bullets[:4],"management_outlook":outlook[:2],
                "publisher":x["publisher"],"source_url":x["url"],"tags":tags[:4],
                "event_type":event_type,
                "source_type":"public_web_quote","identity_verified":True,"identity_method":identity_method,
                "summary_method":"public_title_snippet_summary",
                "copyright_note":"僅整理公開標題與摘要，不重製付費或完整逐字稿。"
            })

    errors.extend(official_errors)
    rows.extend(official_rows)
    deduped=[]; seen_rows=set()
    for row in sorted(rows,key=lambda z:z.get("date","") or "",reverse=True):
        key=(row.get("ticker"),_normalize_title(row.get("title") or "")[:120])
        if key in seen_rows: continue
        seen_rows.add(key); deduped.append(row)
    rows=deduped[:24]

    # Latest three distinct earnings-call dates, preferring richer rows.
    calls=[]; used_dates=set()
    for row in [r for r in rows if r.get("event_type")=="earnings_call"]:
        d=(row.get("date") or "")[:10]
        if d and d in used_dates: continue
        used_dates.add(d)
        calls.append(row)
        if len(calls)>=3: break

    material=[r for r in rows if r.get("event_type")=="material_info"][:12]
    updates=[r for r in rows if r.get("event_type")=="company_update"][:8]
    return {
        "rows":rows,
        "earnings_calls":calls,
        "material_info":material,
        "company_updates":updates,
        "earnings_call_count":len(calls),
        "material_info_count":len(material),
        "errors":errors,
        "queries":queries,
        "fetched_at":datetime.now().astimezone().isoformat(timespec="seconds"),
        "ticker":ticker,"company_name":company_name,
        "policy":"所有事件須通過股票代號或公司名稱身分驗證；上市重大訊息優先採官方公司代號，法說最多顯示最近三次不同日期。"
    }


async def fetch_public_research(ticker: str, company_name: str) -> dict[str, Any]:
    queries=[
        f'"{company_name}" {ticker} 目標價 法人 券商',
        f'"{company_name}" {ticker} EPS 上修 下修 法人',
        f'"{company_name}" {ticker} 法說 目標價 投資評等',
    ]
    rows=[]; errors=[]
    results=await asyncio.gather(*(google_news_rss(q) for q in queries), return_exceptions=True)
    seen=set()
    for result in results:
        if isinstance(result, Exception): errors.append(type(result).__name__); continue
        for x in result:
            identity_ok,identity_method=_company_event_identity(ticker,company_name,x.get("title") or "")
            if not identity_ok: continue
            key=_normalize_title(x['title'])[:90]
            if not key or key in seen: continue
            seen.add(key)
            text=f"{x['title']} {x['snippet']}"
            inst=_extract_institution(text); target=_extract_target(text); rating=_extract_rating(text); eps=_extract_eps(text); eps_year=_extract_eps_year(text)
            if not any([inst,target,rating,eps]): continue
            score=35 + (25 if inst else 0) + (25 if target else 0) + (10 if rating else 0) + (5 if eps else 0)
            rows.append({
                "ticker":ticker,"company_name":company_name,"identity_verified":True,"identity_method":identity_method,
                "institution":inst or "未辨識機構", "report_date":x['published_date'], "rating":rating,
                "target_price":target, "forward_eps":eps, "eps_year":eps_year, "title":x['title'], "summary":x['snippet'][:220],
                "publisher":x['publisher'], "source_url":x['url'], "source_type":"public_web_quote",
                "confidence":min(100,score), "copyright_note":"僅保存公開標題/摘要/數值與來源連結，不重製付費研究全文。"
            })
    rows=sorted(rows,key=lambda z:(z.get('report_date') or '', z.get('confidence') or 0),reverse=True)[:20]
    return {"rows":rows,"errors":errors,"queries":queries,"fetched_at":datetime.now().astimezone().isoformat(timespec='seconds')}

def merge_research(imported: list[dict[str, Any]], web_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows=[]
    for x in imported:
        y=dict(x); y.setdefault('source_type','manual_import'); y.setdefault('confidence',95); rows.append(y)
    rows.extend(web_rows)
    rows=sorted(rows,key=lambda x:x.get('report_date',''),reverse=True)
    cutoff=(date.today()-timedelta(days=365)).isoformat()
    eligible_target_rows=[]; seen_targets=set()
    for x in rows:
        target=safe_num(x.get('target_price')); inst=x.get('institution'); report_date=x.get('report_date') or ''
        if target is None or not inst or inst=='未辨識機構' or int(x.get('confidence') or 0)<70 or report_date<cutoff:
            continue
        key=(inst,report_date,round(target,2))
        if key in seen_targets: continue
        seen_targets.add(key); eligible_target_rows.append(x)
    targets=[safe_num(x.get('target_price')) for x in eligible_target_rows]
    market_mentions=[safe_num(x.get('target_price')) for x in rows if safe_num(x.get('target_price')) is not None and x not in eligible_target_rows]
    # Annual EPS consensus must have an explicit forecast year. Mixing quarterly/TTM/annual EPS is prohibited.
    eps_by_year_inst: dict[int,dict[str,tuple[str,float]]]={}
    for x in rows:
        ep=safe_num(x.get('forward_eps')); ey=x.get('eps_year')
        if ep is not None and isinstance(ey,int) and x.get('confidence',0)>=70 and x.get('institution') not in (None,'未辨識機構'):
            inst=str(x.get('institution')); report_date=str(x.get('report_date') or '')
            current=eps_by_year_inst.setdefault(ey,{}).get(inst)
            if current is None or report_date>current[0]: eps_by_year_inst[ey][inst]=(report_date,ep)
    eps_by_year={year:[value for _,value in institutions_map.values()] for year,institutions_map in eps_by_year_inst.items()}
    eps_consensus={str(y):median(vals) for y,vals in sorted(eps_by_year.items()) if vals}
    current_year=date.today().year
    chosen_year=next((y for y in sorted(eps_by_year) if y>=current_year),None)
    epss=eps_by_year.get(chosen_year,[]) if chosen_year else []
    institutions={x.get('institution') for x in rows if x.get('institution') and x.get('institution')!='未辨識機構'}
    ratings={"買進":0,"中立":0,"賣出":0}
    for x in rows:
        r=x.get('rating')
        if r in ratings: ratings[r]+=1
    revisions=[]; eps_revisions=[]
    by_inst={}; by_inst_eps={}
    for x in rows:
        inst=x.get('institution'); tp=safe_num(x.get('target_price')); ep=safe_num(x.get('forward_eps'))
        if inst and inst!='未辨識機構' and int(x.get('confidence') or 0)>=70 and (x.get('report_date') or '')>=cutoff:
            if tp is not None: by_inst.setdefault(inst,[]).append((x.get('report_date',''),tp))
            if ep is not None: by_inst_eps.setdefault(inst,[]).append((x.get('report_date',''),ep))
    for vals in by_inst.values():
        vals=sorted(vals,reverse=True)
        if len(vals)>=2 and vals[1][1]: revisions.append((vals[0][1]/vals[1][1]-1)*100)
    for vals in by_inst_eps.values():
        vals=sorted(vals,reverse=True)
        if len(vals)>=2 and vals[1][1]: eps_revisions.append((vals[0][1]/vals[1][1]-1)*100)
    return {
        "count":len(rows), "institution_count":len(institutions), "median_target":median(targets) if targets else None,
        "average_target":sum(targets)/len(targets) if targets else None, "high_target":max(targets) if targets else None, "low_target":min(targets) if targets else None,
        "target_coverage":len(eligible_target_rows),"eligible_target_reports":eligible_target_rows,
        "market_mention_count":len(market_mentions),"market_mention_median_target":median(market_mentions) if market_mentions else None,
        "median_forward_eps":median(epss) if epss else None, "forward_eps_year":chosen_year, "forward_eps_by_year":eps_consensus, "eps_coverage":len(epss), "target_revision_pct":median(revisions) if revisions else None, "eps_revision_pct":median(eps_revisions) if eps_revisions else None,
        "ratings":ratings, "reports":rows, "public_web_count":sum(1 for x in rows if x.get('source_type')=='public_web_quote'),
        "manual_count":sum(1 for x in rows if x.get('source_type')=='manual_import'),
        "consensus_policy":"近12個月、可辨識機構、可信度至少70，且同機構同日期同目標價去重；未辨識媒體引用不納入法人共識。",
    }


def expectation_gap_analysis(research: dict[str, Any], events: dict[str, Any], perdata: dict[str, Any], revenue: dict[str, Any], scores_map: dict[str, int], price: float | None) -> dict[str, Any]:
    """V5.1: combine estimate revisions, analyst target revisions, event tone and valuation stretch.
    This is a transparent signal framework, not a price forecast.
    """
    reports = research.get("reports") or []
    by_inst: dict[str, list[dict[str, Any]]] = {}
    for row in reports:
        inst = row.get("institution")
        if not inst or inst == "未辨識機構":
            continue
        by_inst.setdefault(inst, []).append(row)
    revision_rows = []
    for inst, rows in by_inst.items():
        rows = sorted(rows, key=lambda x: x.get("report_date") or "", reverse=True)
        if len(rows) < 2:
            continue
        newest, older = rows[0], rows[1]
        nt, ot = safe_num(newest.get("target_price")), safe_num(older.get("target_price"))
        ne, oe = safe_num(newest.get("forward_eps")), safe_num(older.get("forward_eps"))
        tr = ((nt / ot) - 1) * 100 if nt is not None and ot else None
        er = ((ne / oe) - 1) * 100 if ne is not None and oe else None
        if tr is None and er is None:
            continue
        revision_rows.append({
            "institution": inst, "latest_date": newest.get("report_date"), "previous_date": older.get("report_date"),
            "latest_target": nt, "previous_target": ot, "target_revision_pct": tr,
            "latest_eps": ne, "previous_eps": oe, "eps_revision_pct": er,
            "latest_rating": newest.get("rating"), "source_url": newest.get("source_url")
        })

    positive_words = ["上修","調升","看旺","優於預期","成長","強勁","創高","增加","改善","樂觀","需求旺","接單","擴產"]
    negative_words = ["下修","調降","看淡","不如預期","衰退","疲弱","減少","惡化","保守","庫存","延後","砍單"]
    tone_points = 0
    recent_count = 0
    for row in (events.get("rows") or [])[:12]:
        text = f"{row.get('title','')} {row.get('summary','')}"
        pos = sum(1 for w in positive_words if w in text)
        neg = sum(1 for w in negative_words if w in text)
        tone_points += pos - neg
        recent_count += 1
    event_tone_score = max(-100, min(100, tone_points * 18)) if recent_count else 0

    eps_rev = safe_num(research.get("eps_revision_pct"))
    target_rev = safe_num(research.get("target_revision_pct"))
    median_target = safe_num(research.get("median_target"))
    target_upside = ((median_target / price) - 1) * 100 if median_target is not None and price else None
    current_pe = safe_num(perdata.get("per")); p25=safe_num(perdata.get("pe_p25")); med=safe_num(perdata.get("pe_median")); p75=safe_num(perdata.get("pe_p75"))
    if current_pe is None or med is None:
        valuation_zone, valuation_risk = "資料不足", 0
    elif p75 is not None and current_pe >= p75:
        valuation_zone, valuation_risk = "高於歷史 P75", 85
    elif current_pe >= med:
        valuation_zone, valuation_risk = "高於歷史中位數", 60
    elif p25 is not None and current_pe <= p25:
        valuation_zone, valuation_risk = "低於歷史 P25", 20
    else:
        valuation_zone, valuation_risk = "歷史合理區間", 40

    rev_yoy = safe_num(revenue.get("revenue_yoy")); rev_3m = safe_num(revenue.get("revenue_3m_yoy"))
    growth_accel = (rev_yoy - rev_3m) if rev_yoy is not None and rev_3m is not None else None
    revision_score = 50
    if eps_rev is not None: revision_score += max(-25, min(25, eps_rev * 2.2))
    if target_rev is not None: revision_score += max(-20, min(20, target_rev * 1.5))
    revision_score += max(-15, min(15, event_tone_score * .15))
    if growth_accel is not None: revision_score += max(-10, min(10, growth_accel * .5))
    revision_score = round(max(0, min(100, revision_score)))

    fundamental_positive = (eps_rev is not None and eps_rev > 2) or (rev_yoy is not None and rev_yoy > 10) or event_tone_score > 20
    fundamental_negative = (eps_rev is not None and eps_rev < -2) or (rev_yoy is not None and rev_yoy < -10) or event_tone_score < -20
    if fundamental_positive and valuation_risk >= 80:
        regime = "基本面上修，但估值偏熱"
        summary = "獲利/營運預期偏上修，但目前估值已進入歷史高檔；後續股價更依賴 EPS 持續上修。"
    elif revision_score >= 65:
        regime = "市場預期上修"
        summary = "法人預估、目標價或公司事件訊號整體偏正向，市場預期正在改善。"
    elif revision_score <= 35 or fundamental_negative:
        regime = "市場預期下修"
        summary = "法人預估或公司營運訊號偏弱，市場預期存在下修風險。"
    else:
        regime = "預期中性／等待確認"
        summary = "目前上修與下修訊號未形成明確共識，宜等待下一輪法說、營收或法人修正。"

    signals = [
        {"name":"Forward EPS 修正", "value": eps_rev, "display": pct(eps_rev), "direction": "up" if eps_rev is not None and eps_rev>0 else "down" if eps_rev is not None and eps_rev<0 else "flat"},
        {"name":"法人目標價修正", "value": target_rev, "display": pct(target_rev), "direction": "up" if target_rev is not None and target_rev>0 else "down" if target_rev is not None and target_rev<0 else "flat"},
        {"name":"法人共識相對現價", "value": target_upside, "display": pct(target_upside), "direction": "up" if target_upside is not None and target_upside>0 else "down" if target_upside is not None and target_upside<0 else "flat"},
        {"name":"公司事件語氣", "value": event_tone_score, "display": f"{event_tone_score:+.0f}", "direction": "up" if event_tone_score>0 else "down" if event_tone_score<0 else "flat"},
        {"name":"估值位置", "value": valuation_risk, "display": valuation_zone, "direction": "risk" if valuation_risk>=60 else "flat"},
    ]
    return {
        "regime": regime, "summary": summary, "revision_score": revision_score, "valuation_risk": valuation_risk,
        "valuation_zone": valuation_zone, "event_tone_score": event_tone_score, "target_upside_pct": target_upside,
        "revenue_acceleration_pct": growth_accel, "signals": signals, "institution_revisions": revision_rows[:12],
        "methodology": "Forward EPS/目標價修正 + 公司事件語氣 + 營收動能 + 歷史 PER 位置；缺失欄位不補值。"
    }

def rsi(series: pd.Series, period: int = 14) -> float | None:
    if len(series) < period + 1:
        return None
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    out = 100 - (100 / (1 + rs))
    return safe_num(out.iloc[-1])


def macd(series: pd.Series) -> tuple[float | None, float | None, float | None]:
    if len(series) < 35:
        return None, None, None
    e12 = series.ewm(span=12, adjust=False).mean()
    e26 = series.ewm(span=26, adjust=False).mean()
    m = e12 - e26
    sig = m.ewm(span=9, adjust=False).mean()
    hist = m - sig
    return safe_num(m.iloc[-1]), safe_num(sig.iloc[-1]), safe_num(hist.iloc[-1])


def calc_technical(prices: list[dict[str, Any]]) -> dict[str, Any]:
    df = pd.DataFrame(prices)
    if df.empty or "close" not in df.columns:
        return {}
    df["date"] = pd.to_datetime(df["date"])
    for c in ["close", "max", "min", "open", "Trading_Volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # OHLC fallbacks keep older/partial feeds chartable without inventing values outside the close.
    for c in ["open","max","min"]:
        if c not in df.columns:
            df[c]=df["close"]
    df = df.dropna(subset=["close"]).copy()
    df = df[df["close"] > 0].sort_values("date")
    if df.empty:
        return {}

    close=df["close"].astype(float)
    high=df["max"].fillna(df["close"]).astype(float)
    low=df["min"].fillna(df["close"]).astype(float)
    open_=df["open"].fillna(df["close"]).astype(float)
    last=float(close.iloc[-1])

    ma_series={k:close.rolling(k).mean() for k in (5,10,20,60,120,240)}
    ma={k:(float(ma_series[k].iloc[-1]) if len(close)>=k and pd.notna(ma_series[k].iloc[-1]) else None) for k in ma_series}

    ema12=close.ewm(span=12,adjust=False).mean()
    ema26=close.ewm(span=26,adjust=False).mean()
    dif=ema12-ema26
    dea=dif.ewm(span=9,adjust=False).mean()
    macd_hist=(dif-dea)*2

    delta=close.diff()
    gain=delta.clip(lower=0)
    loss=(-delta.clip(upper=0))
    avg_gain=gain.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    avg_loss=loss.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    rs=avg_gain/avg_loss.replace(0,float("nan"))
    rsi_series=100-(100/(1+rs))
    if pd.isna(rsi_series.iloc[-1]) and avg_loss.iloc[-1]==0 and avg_gain.iloc[-1]>0:
        rsi_series.iloc[-1]=100.0

    ll9=low.rolling(9).min()
    hh9=high.rolling(9).max()
    denom=(hh9-ll9).replace(0,float("nan"))
    rsv=((close-ll9)/denom*100).clip(lower=0,upper=100)
    # Taiwan KD convention: K/D start at 50 and use 1/3 smoothing.
    kvals=[]; dvals=[]; kprev=50.0; dprev=50.0
    for rv in rsv:
        if pd.isna(rv):
            kvals.append(None); dvals.append(None); continue
        kprev=(2/3)*kprev+(1/3)*float(rv)
        dprev=(2/3)*dprev+(1/3)*kprev
        kvals.append(kprev); dvals.append(dprev)
    k_series=pd.Series(kvals,index=df.index,dtype="float64")
    d_series=pd.Series(dvals,index=df.index,dtype="float64")

    high60=float(close.tail(60).max()) if len(close)>=20 else float(close.max())
    low60=float(close.tail(60).min()) if len(close)>=20 else float(close.min())
    support1=ma[20] or low60
    support2=ma[60] or low60
    trend="多頭" if ma[20] and ma[60] and last>ma[20]>ma[60] else ("偏多" if ma[20] and last>ma[20] else "整理/偏弱")

    vol_ratio=None
    if "Trading_Volume" in df.columns and len(df)>=21:
        v20=df["Trading_Volume"].iloc[-21:-1].mean()
        vol_ratio=float(df["Trading_Volume"].iloc[-1]/v20) if v20 and not pd.isna(v20) else None
    returns20=(last/float(close.iloc[-21])-1)*100 if len(close)>=21 else None
    returns60=(last/float(close.iloc[-61])-1)*100 if len(close)>=61 else None

    # One trading year. Every row carries OHLC + MA + oscillators so the browser can render
    # a real daily K chart and aligned KD / MACD / RSI panels.
    view=df.tail(252)
    series=[]
    for idx,row in view.iterrows():
        def num(v):
            return None if pd.isna(v) else float(v)
        series.append({
            "date":row["date"].date().isoformat(),
            "open":num(open_.loc[idx]),"high":num(high.loc[idx]),"low":num(low.loc[idx]),"close":num(close.loc[idx]),
            "volume":num(row.get("Trading_Volume")),
            "ma20":num(ma_series[20].loc[idx]),"ma60":num(ma_series[60].loc[idx]),
            "k":num(k_series.loc[idx]),"d":num(d_series.loc[idx]),
            "macd":num(dif.loc[idx]),"macd_signal":num(dea.loc[idx]),"macd_hist":num(macd_hist.loc[idx]),
            "rsi14":num(rsi_series.loc[idx]),
        })

    return {
        "last":last,"last_date":df.iloc[-1]["date"].date().isoformat(),"ma":ma,
        "rsi14":num(rsi_series.iloc[-1]),"k":num(k_series.iloc[-1]),"d":num(d_series.iloc[-1]),
        "macd":num(dif.iloc[-1]),"macd_signal":num(dea.iloc[-1]),"macd_hist":num(macd_hist.iloc[-1]),
        "support1":support1,"support2":support2,"resistance":high60,
        "trend":trend,"volume_ratio_20":vol_ratio,"return_20d":returns20,"return_60d":returns60,
        "series":series,"series_days":len(series),"chart_period":"近一年日K",
        "high_52w":float(close.tail(252).max()),"low_52w":float(close.tail(252).min()),
    }


def calc_flow(rows: list[dict[str, Any]], margin_rows: list[dict[str, Any]], lending: dict[str, Any] | None=None) -> dict[str, Any]:
    df=pd.DataFrame(rows)
    result: dict[str,Any]={}
    if not df.empty:
        for c in df.columns:
            if c not in ("date","stock_id"):
                df[c]=pd.to_numeric(df[c],errors="coerce")
        df=df.sort_values("date")
        def net(prefix:str,n:int)->float | None:
            buy=[c for c in df.columns if c.startswith(prefix) and c.endswith("_buy")]
            sell=[c for c in df.columns if c.startswith(prefix) and c.endswith("_sell")]
            if not buy or not sell:
                return None
            tail=df.tail(n)
            buy_total=tail[buy].sum(min_count=1).sum(min_count=1)
            sell_total=tail[sell].sum(min_count=1).sum(min_count=1)
            if pd.isna(buy_total) or pd.isna(sell_total):
                return None
            return float(buy_total-sell_total)
        flow_values={
            "foreign_1":net("Foreign_",1),"foreign_5":net("Foreign_",5),"foreign_20":net("Foreign_",20),
            "trust_1":net("Investment_Trust",1),"trust_5":net("Investment_Trust",5),"trust_20":net("Investment_Trust",20),
            "dealer_1":net("Dealer",1),"dealer_5":net("Dealer",5),"dealer_20":net("Dealer",20),
        }
        result.update(flow_values)
        if any(value is not None for value in flow_values.values()):
            result["last_date"]=str(df.iloc[-1]["date"])

    mdf=pd.DataFrame(margin_rows)
    if not mdf.empty and "MarginPurchaseTodayBalance" in mdf.columns:
        mdf["MarginPurchaseTodayBalance"]=pd.to_numeric(mdf["MarginPurchaseTodayBalance"],errors="coerce")
        if "ShortSaleTodayBalance" in mdf.columns:
            mdf["ShortSaleTodayBalance"]=pd.to_numeric(mdf["ShortSaleTodayBalance"],errors="coerce")
        mdf=mdf.sort_values("date").dropna(subset=["MarginPurchaseTodayBalance"])
        if not mdf.empty:
            latest=float(mdf.iloc[-1]["MarginPurchaseTodayBalance"])
            def balance_change(n:int):
                if len(mdf)<2: return None
                idx=max(0,len(mdf)-1-n)
                prior=float(mdf.iloc[idx]["MarginPurchaseTodayBalance"])
                return ((latest/prior)-1)*100 if prior else None
            result.update({
                "margin_balance":latest,
                "margin_1_pct":balance_change(1),"margin_5_pct":balance_change(5),"margin_20_pct":balance_change(20),
                "margin_last_date":str(mdf.iloc[-1]["date"])
            })
            if "ShortSaleTodayBalance" in mdf.columns:
                short_df=mdf.dropna(subset=["ShortSaleTodayBalance"])
                if not short_df.empty:
                    short_latest=float(short_df.iloc[-1]["ShortSaleTodayBalance"])
                    def short_change(n:int):
                        if len(short_df)<2: return None
                        prior=float(short_df.iloc[max(0,len(short_df)-1-n)]["ShortSaleTodayBalance"])
                        return ((short_latest/prior)-1)*100 if prior else None
                    result.update({"short_balance":short_latest,"short_1_pct":short_change(1),"short_5_pct":short_change(5),"short_20_pct":short_change(20),
                                   "short_margin_ratio_pct":(short_latest/latest*100) if latest else None})
    if lending:
        result.update(lending)
    return result


def calc_revenue(rows: list[dict[str, Any]]) -> dict[str, Any]:
    df = pd.DataFrame(rows)
    if df.empty:
        return {}
    for c in ["revenue", "revenue_year", "revenue_month"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["revenue"]).sort_values(["revenue_year", "revenue_month"])
    if df.empty:
        return {}
    last = df.iloc[-1]
    prev = df[(df["revenue_year"] == last["revenue_year"] - 1) & (df["revenue_month"] == last["revenue_month"])]
    yoy = None if prev.empty or float(prev.iloc[-1]["revenue"]) == 0 else (float(last["revenue"]) / float(prev.iloc[-1]["revenue"]) - 1) * 100
    last3 = df.tail(3)["revenue"].sum() if len(df) >= 3 else None
    prev3 = df.iloc[-15:-12]["revenue"].sum() if len(df) >= 15 else None
    yoy3 = (last3 / prev3 - 1) * 100 if last3 and prev3 else None
    return {
        "latest_revenue": float(last["revenue"]), "revenue_yoy": yoy, "revenue_3m_yoy": yoy3,
        "revenue_period": f"{int(last['revenue_year'])}-{int(last['revenue_month']):02d}", "last_date": str(last.get("date", "")),
        "series": [{"period": f"{int(r.revenue_year)}-{int(r.revenue_month):02d}", "revenue": float(r.revenue)} for r in df.tail(24).itertuples()],
    }


def calc_per(rows: list[dict[str, Any]]) -> dict[str, Any]:
    df = pd.DataFrame(rows)
    if df.empty:
        return {}
    df = df.sort_values("date")
    for c in ["PER", "PBR", "dividend_yield"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    r = df.iloc[-1]
    valid = df["PER"].dropna() if "PER" in df.columns else pd.Series(dtype=float)
    valid = valid[(valid > 0) & (valid < 200)]
    hist = {}
    if len(valid) >= 20:
        hist = {
            "pe_p25": float(valid.quantile(.25)), "pe_median": float(valid.quantile(.50)),
            "pe_p75": float(valid.quantile(.75)), "pe_mean": float(valid.mean()), "sample_count": int(len(valid)),
        }
    return {"per": safe_num(r.get("PER")), "pbr": safe_num(r.get("PBR")), "dividend_yield": safe_num(r.get("dividend_yield")), "last_date": str(r.get("date", "")), **hist}


def calc_financials(rows: list[dict[str, Any]]) -> dict[str, Any]:
    df = pd.DataFrame(rows)
    if df.empty:
        return {}
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    dates = sorted(df["date"].unique())
    if not dates:
        return {}
    latest_date = dates[-1]
    q = df[df["date"] == latest_date]
    def pick(keys: list[str]) -> float | None:
        for k in keys:
            hit = q[q["type"].astype(str).str.lower().str.contains(k.lower(), regex=False)]
            if not hit.empty:
                return safe_num(hit.iloc[0]["value"])
        return None
    revenue = pick(["Revenue", "OperatingRevenue", "營業收入"])
    gross = pick(["GrossProfit", "營業毛利"])
    op = pick(["OperatingIncome", "營業利益"])
    net = pick(["NetIncome", "本期淨利", "ProfitLoss"])
    eps = pick(["BasicEarningsPerShare", "EarningsPerShare", "基本每股盈餘"])
    return {
        "statement_date": str(latest_date), "revenue": revenue, "gross_profit": gross, "operating_income": op, "net_income": net, "eps": eps,
        "gross_margin": (gross / revenue * 100) if gross is not None and revenue else None,
        "operating_margin": (op / revenue * 100) if op is not None and revenue else None,
        "net_margin": (net / revenue * 100) if net is not None and revenue else None,
    }


def load_research(ticker: str) -> list[dict[str, Any]]:
    p = DATA_DIR / "research_reports.json"
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return [x for x in raw if str(x.get("ticker")) == ticker]
    except Exception:
        return []


def analyst_consensus(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda x: x.get("report_date", ""), reverse=True)
    targets = [safe_num(x.get("target_price")) for x in rows]
    targets = [x for x in targets if x is not None]
    epss = [safe_num(x.get("forward_eps")) for x in rows]
    epss = [x for x in epss if x is not None]
    revision = None
    dated_eps = [(x.get("report_date", ""), safe_num(x.get("forward_eps"))) for x in rows if safe_num(x.get("forward_eps")) is not None]
    if len(dated_eps) >= 2 and dated_eps[-1][1]:
        revision = (dated_eps[0][1] / dated_eps[-1][1] - 1) * 100
    return {"count": len(rows), "median_target": median(targets) if targets else None, "median_forward_eps": median(epss) if epss else None, "eps_revision_pct": revision, "reports": rows}


def model_valuation(price: float | None, perdata: dict[str, Any], eps_stack: dict[str, Any], research: dict[str, Any], integrity: dict[str, Any] | None=None) -> dict[str, Any]:
    if not price: return {"scenarios": [], "confidence": 0}
    consensus_eps=safe_num(research.get("median_forward_eps")); coverage=int(research.get("eps_coverage") or 0)
    integrity=integrity or {}; allow_financial=bool(integrity.get("core_financials_allowed", True))
    ttm=safe_num(eps_stack.get("ttm_eps")) if allow_financial else None
    ytd=safe_num(eps_stack.get("ytd_eps")) if allow_financial else None
    q=eps_stack.get("quarter_period")
    trailing_eps=None; trailing_basis="資料不足"; trailing_conf=0
    if ttm and ttm>0:
        trailing_eps=ttm; trailing_basis=f"TTM EPS {ttm:.2f}（截至 {q or 'latest'}；不使用單季×4）"; trailing_conf=78
    elif ytd and ytd>0:
        trailing_eps=ytd; trailing_basis=f"YTD EPS {ytd:.2f}（資料不足以形成 TTM，暫不年化單季）"; trailing_conf=48
    elif perdata.get("per") and perdata["per"]>0 and allow_financial:
        trailing_eps=price/perdata["per"]; trailing_basis="由現價 / 市場 PER 反推 TTM EPS（降級模型）"; trailing_conf=32
    forward_eps=consensus_eps if consensus_eps and coverage>=2 else None
    forward_basis=f"{research.get('forward_eps_year')}E EPS 中位數（{coverage} 筆獨立可比預估）" if forward_eps else "缺少至少2筆可辨識機構、明確年度的 Forward EPS"
    if forward_eps:
        anchor_eps=forward_eps; eps_basis=forward_basis; eps_conf=82; selected_model="forward_consensus"
    elif trailing_eps:
        anchor_eps=trailing_eps; eps_basis=trailing_basis; eps_conf=trailing_conf; selected_model="trailing"
    else: return {"scenarios": [], "eps_basis":"資料不足", "confidence":0,"selected_model":"none"}
    if perdata.get("pe_median"):
        bear_pe=max(5.0,perdata["pe_p25"]); base_pe=perdata["pe_median"]; bull_pe=min(150.0,perdata["pe_p75"]); pe_basis=f"近年歷史 PER 分位數（樣本 {perdata.get('sample_count',0)} 日）"; pe_conf=85
    else:
        center=perdata.get("per") if perdata.get("per") and 5<=perdata["per"]<=120 else 20.0; bear_pe,base_pe,bull_pe=max(8.0,center*.8),center,min(150.0,center*1.2); pe_basis="目前 PER ±20%（歷史樣本不足降級模型）"; pe_conf=50
    def scenarios_for(eps: float | None):
        if not eps: return []
        values=[{"name":"悲觀","eps":eps*.90,"pe":bear_pe},{"name":"基準","eps":eps,"pe":base_pe},{"name":"樂觀","eps":eps*1.10,"pe":bull_pe}]
        for item in values:
            item["target"]=item["eps"]*item["pe"]; item["upside_pct"]=(item["target"]/price-1)*100
        return values
    trailing_scenarios=scenarios_for(trailing_eps); forward_scenarios=scenarios_for(forward_eps)
    scenarios=forward_scenarios or trailing_scenarios
    base=next((x for x in scenarios if x["name"]=="基準"),None)
    analyst_target=safe_num(research.get("median_target")); analyst_coverage=int(research.get("target_coverage") or 0)
    gap_pct=((analyst_target/base["target"])-1)*100 if analyst_target is not None and base and base.get("target") else None
    return {"eps_basis":eps_basis,"trailing_eps_basis":trailing_basis,"forward_eps_basis":forward_basis,"pe_basis":pe_basis,
            "confidence":round((eps_conf+pe_conf)/2),"anchor_eps":anchor_eps,"selected_model":selected_model,"scenarios":scenarios,
            "trailing_scenarios":trailing_scenarios,"forward_scenarios":forward_scenarios,
            "analyst_consensus":{"median_target":analyst_target,"coverage":analyst_coverage,"gap_vs_model_pct":gap_pct,
              "interpretation":"法人目標價反映未來盈餘與研究假設；模型採可驗證 EPS 與歷史 PER，兩者不強制收斂。"}}


def scores(technical: dict[str, Any], revenue: dict[str, Any], flow: dict[str, Any], perdata: dict[str, Any], financial: dict[str, Any], research: dict[str, Any]) -> dict[str, int]:
    fundamental = 50
    if revenue.get("revenue_yoy") is not None: fundamental += max(-20, min(25, revenue["revenue_yoy"] * .5))
    if revenue.get("revenue_3m_yoy") is not None: fundamental += max(-10, min(15, revenue["revenue_3m_yoy"] * .2))
    if financial.get("gross_margin") is not None: fundamental += 5 if financial["gross_margin"] > 30 else 0
    chip = 50 + (15 if flow.get("foreign_20", 0) > 0 else -15 if flow.get("foreign_20") is not None else 0) + (10 if flow.get("trust_20", 0) > 0 else -10 if flow.get("trust_20") is not None else 0)
    tech = 50 + (25 if technical.get("trend") == "多頭" else 12 if technical.get("trend") == "偏多" else -5)
    r = technical.get("rsi14")
    if r is not None: tech += 7 if 50 <= r <= 70 else (-8 if r > 80 or r < 25 else 0)
    if technical.get("macd_hist") is not None: tech += 5 if technical["macd_hist"] > 0 else -5
    valuation = 55
    pe = perdata.get("per")
    if pe and perdata.get("pe_median"):
        valuation += 15 if pe < perdata["pe_p25"] else (5 if pe <= perdata["pe_median"] else (-10 if pe > perdata["pe_p75"] else 0))
    elif pe: valuation += 8 if pe < 25 else (-8 if pe > 60 else 0)
    revision = 50
    if research.get("eps_revision_pct") is not None: revision += max(-25, min(30, research["eps_revision_pct"] * 2))
    d = {
        "基本面": round(max(0, min(100, fundamental))), "籌碼面": round(max(0, min(100, chip))),
        "技術面": round(max(0, min(100, tech))), "估值": round(max(0, min(100, valuation))),
        "預估修正": round(max(0, min(100, revision))),
    }
    d["綜合"] = round(d["基本面"]*.28 + d["籌碼面"]*.20 + d["技術面"]*.20 + d["估值"]*.17 + d["預估修正"]*.15)
    return d


def calc_confidence(source_status: list[dict[str, Any]], valuation: dict[str, Any], research: dict[str, Any]) -> dict[str, Any]:
    available = sum(1 for x in source_status if x["status"] == "ok")
    completeness = round(available / len(source_status) * 100) if source_status else 0
    research_bonus = 10 if research.get("count", 0) >= 3 else 5 if research.get("count", 0) else 0
    overall = round(completeness * .65 + valuation.get("confidence", 0) * .25 + research_bonus)
    return {"data_completeness": completeness, "valuation_confidence": valuation.get("confidence", 0), "research_coverage": research.get("count", 0), "overall": min(100, overall)}


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def make_evidence(metric: str, value: Any, *, category: str, kind: str="fact", period: str | None=None,
                  as_of: str | None=None, source: str="", source_type: str="third_party", source_url: str | None=None,
                  confidence: int=70, status: str="usable", unit: str | None=None, derived_from: list[str] | None=None,
                  note: str | None=None, definition: str | None=None) -> dict[str, Any]:
    return {
        "id": f"{category}:{metric}:{period or as_of or 'na'}:{source_type}",
        "metric": metric, "category": category, "kind": kind, "value": value, "unit": unit,
        "period": period, "as_of": as_of, "source": source, "source_type": source_type,
        "source_url": source_url, "confidence": int(max(0,min(100,confidence))), "status": status,
        "derived_from": derived_from or [], "note": note, "definition": definition or metric,
    }


def _source_type_for_financial(financial: dict[str,Any], eps_stack: dict[str,Any]) -> str:
    src=str(financial.get("source") or eps_stack.get("source") or "")
    if "TWSE" in src or "MOPS" in src or "TPEx" in src: return "official_exchange"
    if "Registry" in src or "registry" in src: return "verified_registry"
    if any(x in src for x in ("TSMC","Alchip","公司","IR","Press")): return "company_official"
    return "third_party"


def build_evidence_graph(ticker: str, tech: dict[str,Any], revenue: dict[str,Any], flow: dict[str,Any], perdata: dict[str,Any],
                         financial: dict[str,Any], eps_stack: dict[str,Any], research: dict[str,Any], company_events: dict[str,Any],
                         integrity: dict[str,Any]) -> dict[str,Any]:
    ev=[]
    if tech.get("last") is not None:
        ev.append(make_evidence("close",tech.get("last"),category="market",period=tech.get("last_date"),as_of=tech.get("last_date"),source="market feed",source_type="market_feed",confidence=82,unit="TWD"))
    for k,label in (("per","PER"),("pbr","PBR")):
        if perdata.get(k) is not None:
            ev.append(make_evidence(label,perdata.get(k),category="valuation",period=perdata.get("last_date"),as_of=perdata.get("last_date"),source="market valuation feed",source_type="market_feed",confidence=80,unit="x"))
    if revenue.get("latest_revenue") is not None:
        ev.append(make_evidence("monthly_revenue",revenue.get("latest_revenue"),category="fundamental",period=revenue.get("revenue_period"),as_of=revenue.get("last_date"),source="monthly revenue disclosure",source_type="official_or_structured",confidence=88,unit="TWD"))
    if revenue.get("revenue_yoy") is not None:
        ev.append(make_evidence("monthly_revenue_yoy",revenue.get("revenue_yoy"),category="fundamental",kind="derived_fact",period=revenue.get("revenue_period"),source="derived from monthly revenue",source_type="derived",confidence=86,unit="%",derived_from=["monthly_revenue"]))
    st=_source_type_for_financial(financial,eps_stack); fin_conf=98 if integrity.get("official_verified") else 45
    for key,metric in (("quarter_eps","quarter_eps"),("ytd_eps","ytd_eps"),("ttm_eps","ttm_eps")):
        val=eps_stack.get(key)
        if val is not None:
            qmethod=str(eps_stack.get("quarter_method") or "")
            if key=="quarter_eps":
                kind="fact" if qmethod in {"official_direct","official_registry_verified","official_ytd_q1"} else "derived_fact"
                definition="standalone_quarter_eps"
            elif key=="ttm_eps":
                kind="derived_fact"; definition="trailing_four_quarter_eps"
            else:
                kind="fact"; definition="cumulative_ytd_eps"
            ev.append(make_evidence(metric,val,category="fundamental",kind=kind,period=eps_stack.get("quarter_period") or financial.get("period"),source=(eps_stack.get("quarter_source") if key=="quarter_eps" else None) or financial.get("source") or eps_stack.get("source") or "financial feed",source_type=("company_official" if key=="quarter_eps" and qmethod in {"official_direct","official_registry_verified"} else st),confidence=fin_conf,unit="TWD/share",note=eps_stack.get("quarter_method_label") if key=="quarter_eps" else eps_stack.get("note"),definition=definition))
    for key in ("gross_margin","operating_margin"):
        if financial.get(key) is not None:
            ev.append(make_evidence(key,financial.get(key),category="fundamental",period=financial.get("period"),source=financial.get("source") or "financial statement",source_type=st,confidence=fin_conf,unit="%"))
    for key,metric in (("foreign_1","foreign_1d"),("foreign_5","foreign_5d"),("foreign_20","foreign_20d"),
                       ("trust_1","trust_1d"),("trust_5","trust_5d"),("trust_20","trust_20d"),
                       ("dealer_1","dealer_1d"),("dealer_5","dealer_5d"),("dealer_20","dealer_20d"),
                       ("margin_1_pct","margin_1d_pct"),("margin_5_pct","margin_5d_pct"),("margin_20_pct","margin_20d_pct"),
                       ("margin_balance","margin_balance")):
        if flow.get(key) is not None:
            ev.append(make_evidence(metric,flow.get(key),category="positioning",period=flow.get("last_date") or flow.get("margin_last_date"),source="institutional/margin feed",source_type="official_or_structured",confidence=82))
    if tech.get("rsi14") is not None:
        ev.append(make_evidence("RSI14",tech.get("rsi14"),category="technical",kind="derived_fact",period=tech.get("last_date"),source="derived from daily prices",source_type="derived",confidence=90,derived_from=["close_series"]))
    for key,label in (("k","KD_K"),("d","KD_D"),("macd","MACD_DIF"),("macd_signal","MACD_SIGNAL"),("macd_hist","MACD_HIST")):
        if tech.get(key) is not None:
            ev.append(make_evidence(label,tech.get(key),category="technical",kind="derived_fact",period=tech.get("last_date"),source="derived from daily OHLC",source_type="derived",confidence=90,derived_from=["ohlc_series"]))
    for n,v in (tech.get("ma") or {}).items():
        if v is not None: ev.append(make_evidence(f"MA{n}",v,category="technical",kind="derived_fact",period=tech.get("last_date"),source="derived from daily prices",source_type="derived",confidence=90,unit="TWD",derived_from=["close_series"]))
    for row in (research.get("reports") or [])[:25]:
        if row.get("target_price") is not None:
            ev.append(make_evidence("analyst_target_price",safe_num(row.get("target_price")),category="research",kind="estimate",period=str(row.get("target_period") or "forward"),as_of=row.get("report_date"),source=row.get("institution") or row.get("publisher") or "public research",source_type="public_web",source_url=row.get("source_url"),confidence=int(row.get("confidence") or 60),unit="TWD",note=row.get("title"),definition="analyst_target_price_estimate"))
        if row.get("forward_eps") is not None:
            ev.append(make_evidence("analyst_forward_eps",safe_num(row.get("forward_eps")),category="research",kind="estimate",period=str(row.get("forward_eps_year") or "forward"),as_of=row.get("report_date"),source=row.get("institution") or "public research",source_type="public_web",source_url=row.get("source_url"),confidence=int(row.get("confidence") or 60),unit="TWD/share",note=row.get("title"),definition="analyst_forward_eps_estimate"))
    # V5.3.2 quality engine: only comparable Facts/Derived Facts can conflict.
    # Estimates and model outputs are revisions/scenarios, not source conflicts.
    conflicts=[]; groups={}
    comparable_kinds={"fact","derived_fact"}
    for x in ev:
        if x.get("status")!="usable" or x.get("kind") not in comparable_kinds or not isinstance(x.get("value"),(int,float)):
            continue
        key=(x["metric"],x.get("period"),x.get("definition"),x.get("unit"))
        groups.setdefault(key,[]).append(x)
    for (metric,period,definition,unit),rows in groups.items():
        # Duplicate observations from the same source are not independent corroboration.
        independent={}
        for x in rows:
            independent[(x.get("source_type"),x.get("source"))]=x
        rows=list(independent.values())
        if len(rows)<2: continue
        vals=[float(x["value"]) for x in rows]
        base=max(1e-9,abs(sum(vals)/len(vals)))
        spread=(max(vals)-min(vals))/base
        if spread>0.02:
            conflicts.append({"metric":metric,"period":period,"definition":definition,"unit":unit,
                              "values":[{"value":x["value"],"source":x["source"],"source_type":x["source_type"]} for x in rows],
                              "spread_pct":round(spread*100,2)})

    # Estimate revisions are tracked separately and never penalize the Evidence Score.
    revisions=[]
    est_groups={}
    for x in ev:
        if x.get("status")=="usable" and x.get("kind")=="estimate" and isinstance(x.get("value"),(int,float)):
            est_groups.setdefault((x["metric"],x.get("period"),x.get("source"),x.get("definition")),[]).append(x)
    for (metric,period,source,definition),rows in est_groups.items():
        rows=sorted(rows,key=lambda r:str(r.get("as_of") or ""))
        for prev,cur in zip(rows,rows[1:]):
            pv=float(prev["value"]); cv=float(cur["value"])
            pct=None if abs(pv)<1e-9 else round((cv/pv-1)*100,2)
            revisions.append({"metric":metric,"period":period,"source":source,"definition":definition,
                              "from_as_of":prev.get("as_of"),"to_as_of":cur.get("as_of"),
                              "from_value":pv,"to_value":cv,"revision_pct":pct})

    usable=[x for x in ev if x.get("status")=="usable"]
    official=[x for x in usable if x.get("source_type") in {"official_exchange","company_official","verified_registry","official_or_structured"}]
    facts=[x for x in usable if x.get("kind")=="fact"]
    derived=[x for x in usable if x.get("kind")=="derived_fact"]
    estimates=[x for x in usable if x.get("kind")=="estimate"]
    model_outputs=[x for x in usable if x.get("kind")=="model_output"]
    score=max(0,round(min(100,(len(usable)*2.0)+(len(official)*3.0)+(len(facts)*0.5)-len(conflicts)*10))) if usable else 0
    return {"schema_version":"1.1","generated_at":_iso_now(),"ticker":ticker,"records":ev,
            "conflicts":conflicts,"estimate_revisions":revisions,
            "summary":{"records":len(ev),"usable":len(usable),"official_or_verified":len(official),
                       "facts":len(facts),"derived_facts":len(derived),"estimates":len(estimates),
                       "model_outputs":len(model_outputs),"analysis":0,"conflicts":len(conflicts),
                       "estimate_revisions":len(revisions),"evidence_score":score},
            "policy":"V5.3.2: Official Fact → Derived Fact → Estimate → Model Output. Conflict requires identical metric + period + definition + unit across independent factual sources. Estimate changes are tracked as revisions, not conflicts."}


async def probe_twstock_mcp() -> dict[str,Any]:
    out={"provider":"TWStock MCP compatible adapter","enabled":TWSTOCK_MCP_ENABLED,"url":TWSTOCK_MCP_URL,"mode":"shadow_crosscheck","status":"disabled"}
    if not TWSTOCK_MCP_ENABLED: return out
    payload={"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"ai-stock-research-terminal","version":"5.3.5"}}}
    try:
        async with httpx.AsyncClient(timeout=8,follow_redirects=True,headers={"Accept":"application/json, text/event-stream","Content-Type":"application/json"}) as client:
            r=await client.post(TWSTOCK_MCP_URL,json=payload)
        out.update({"http_status":r.status_code,"content_type":r.headers.get("content-type"),"status":"reachable" if r.status_code<500 else "error","preview":r.text[:300]})
    except Exception as e:
        out.update({"status":"error","error":f"{type(e).__name__}: {e}"})
    return out


def narrative(s: dict[str, int], tech: dict[str, Any], revenue: dict[str, Any], flow: dict[str, Any], valuation: dict[str, Any], research: dict[str, Any]) -> dict[str, Any]:
    base = next((x for x in valuation.get("scenarios", []) if x["name"] == "基準"), None)
    facts=[]
    if revenue.get("revenue_yoy") is not None: facts.append(f"最新月營收年增 {revenue['revenue_yoy']:+.1f}%")
    if revenue.get("revenue_3m_yoy") is not None: facts.append(f"近3月營收年增 {revenue['revenue_3m_yoy']:+.1f}%")
    if flow.get("foreign_20") is not None: facts.append(f"外資近20日淨買賣 {flow['foreign_20']:,.0f} 股")
    if tech.get("trend"): facts.append(f"技術趨勢 {tech['trend']}")
    if research.get("eps_revision_pct") is not None: facts.append(f"法人研究 Forward EPS 修正 {research['eps_revision_pct']:+.1f}%")
    if base: facts.append(f"模型基準合理價約 {base['target']:,.0f} 元")
    stance = "偏多" if s["綜合"] >= 75 else "中性偏多" if s["綜合"] >= 58 else "中性" if s["綜合"] >= 42 else "偏弱"
    catalysts=[]; risks=[]
    if revenue.get("revenue_yoy") is not None and revenue["revenue_yoy"] > 15: catalysts.append("營收成長動能高於中性門檻")
    if flow.get("foreign_20",0) > 0 and flow.get("trust_20",0) > 0: catalysts.append("外資與投信近20日同向買超")
    if tech.get("trend") == "多頭": catalysts.append("中期均線結構維持多頭")
    if research.get("eps_revision_pct") is not None and research["eps_revision_pct"] > 3: catalysts.append("Forward EPS 共識出現上修")
    pe = valuation.get("scenarios", [{}])[1].get("pe") if len(valuation.get("scenarios", [])) > 1 else None
    if tech.get("rsi14") and tech["rsi14"] > 75: risks.append("RSI 偏高，短線追價風險上升")
    if flow.get("margin_20_pct") and flow["margin_20_pct"] > 10: risks.append("融資餘額快速增加，籌碼波動風險升高")
    if s["估值"] < 45: risks.append("目前估值相對自身歷史區間偏昂貴")
    if revenue.get("revenue_yoy") is not None and revenue["revenue_yoy"] < 0: risks.append("最新月營收仍呈年減")
    return {"stance": stance, "thesis": "；".join(facts)+"。" if facts else "目前公開結構化資料不足，系統不產生強結論。", "catalysts": catalysts[:4] or ["等待下一次營收、財報或法人預估出現明確上修訊號"], "risks": risks[:4] or ["模型假設與市場估值可能快速變動，需持續追蹤資料更新"]}


async def build_stock(ticker: str, force_refresh: bool = False) -> dict[str, Any]:
    ticker=normalize_ticker(ticker)
    if not force_refresh and ticker in _CACHE:
        cached_age=time.time()-_CACHE[ticker][0]
        cached_ttl=int(_CACHE[ticker][1].get("cache",{}).get("ttl_seconds") or CACHE_TTL)
        if cached_age < cached_ttl:
            cached=dict(_CACHE[ticker][1]); cached["cache"]={"hit":True,"ttl_seconds":cached_ttl}; return cached
    today = date.today(); errors: list[str] = []
    provider_status={"price_provider":None,"info_provider":None,"fallback_used":False,"fallback_errors":[],
                     "finmind_available":False,"finmind_price_available":False,"official_price_fallback_success":False,
                     "twse_fallback_success":False,"tpex_fallback_success":False,"price_rows":0,"latest_price_date":None,
                     "institutional_provider":None,"margin_provider":None,"revenue_provider":None,
                     "lending_provider":None,"supplemental_fallback_datasets":[]}
    async def grab(dataset: str, days: int):
        last_error=None
        for attempt in range(2):
            try:
                rows=await finmind(dataset,ticker,today-timedelta(days=days),today)
                if rows: return rows
                last_error="empty"
            except Exception as exc:
                last_error=type(exc).__name__
            if attempt==0: await asyncio.sleep(0.25)
        errors.append(f"{dataset}: {last_error or 'empty'} after retry"); return []
    async def info_grab():
        try:
            infos = await finmind("TaiwanStockInfo")
            return next((x for x in infos if str(x.get("stock_id")) == ticker), {})
        except Exception as e: errors.append(f"TaiwanStockInfo: {type(e).__name__}"); return {}

    info, prices, inst, margin, rev, pers, fin = await asyncio.gather(
        info_grab(), grab("TaiwanStockPrice", 460), grab("TaiwanStockInstitutionalInvestorsBuySellWide", 120),
        grab("TaiwanStockMarginPurchaseShortSale", 120), grab("TaiwanStockMonthRevenue", 900),
        grab("TaiwanStockPER", 1100), grab("TaiwanStockFinancialStatements", 1100)
    )
    provider_status["finmind_available"]=any(bool(rows) for rows in (info,prices,inst,margin,rev,pers,fin))
    info=normalize_stock_info(info,ticker)
    if info:
        provider_status["info_provider"]="FinMind TaiwanStockInfo"
    else:
        provider_status["fallback_used"]=True
        provider_status["fallback_errors"].append("FinMind TaiwanStockInfo returned no matching row")
        info,official_info_errors=await fetch_official_stock_info(ticker)
        provider_status["fallback_errors"].extend(official_info_errors)
        if info:
            provider_status["info_provider"]=info.get("source")
    if prices:
        provider_status.update({"price_provider":"FinMind TaiwanStockPrice","finmind_price_available":True})
    else:
        provider_status["fallback_used"]=True
        provider_status["fallback_errors"].append("FinMind TaiwanStockPrice returned no usable rows")
        prices,official_price_provider,official_price_errors=await fetch_official_stock_day(ticker,info.get("type") or "")
        provider_status["fallback_errors"].extend(official_price_errors)
        if prices:
            provider_status.update({"price_provider":official_price_provider,"official_price_fallback_success":True})
            provider_status["twse_fallback_success"]=official_price_provider=="TWSE STOCK_DAY"
            provider_status["tpex_fallback_success"]=official_price_provider=="TPEx afterTrading/tradingStock"

    provider_status["institutional_provider"]="FinMind TaiwanStockInstitutionalInvestorsBuySellWide" if inst else None
    provider_status["margin_provider"]="FinMind TaiwanStockMarginPurchaseShortSale" if margin else None
    provider_status["revenue_provider"]="FinMind TaiwanStockMonthRevenue" if rev else None
    if (not inst or not margin or not rev) and info.get("type")=="上市":
        provider_status["fallback_used"]=True
        trading_dates=[str(row.get("date") or "") for row in prices]
        flow_task=fetch_twse_flow_fallback(ticker,trading_dates) if (not inst or not margin) else None
        revenue_task=fetch_twse_revenue_fallback(ticker) if not rev else None
        flow_result,revenue_result=await asyncio.gather(
            flow_task if flow_task else asyncio.sleep(0,result=([],[],[])),
            revenue_task if revenue_task else asyncio.sleep(0,result=([],[])),
        )
        official_inst,official_margin,flow_errors=flow_result
        official_revenue,revenue_errors=revenue_result
        provider_status["fallback_errors"].extend(flow_errors+revenue_errors)
        if not inst and official_inst:
            inst=official_inst; provider_status["institutional_provider"]="TWSE T86"; provider_status["supplemental_fallback_datasets"].append("institutional")
        if not margin and official_margin:
            margin=official_margin; provider_status["margin_provider"]="TWSE MI_MARGN"; provider_status["supplemental_fallback_datasets"].append("margin")
        if not rev and official_revenue:
            rev=official_revenue; provider_status["revenue_provider"]="TWSE/MOPS t187ap05_L"; provider_status["supplemental_fallback_datasets"].append("revenue")

    lending={}
    if info.get("type")=="上市" and prices:
        lending,lending_errors=await fetch_twse_lending_snapshot(ticker,str(prices[-1].get("date") or ""))
        provider_status["fallback_errors"].extend(lending_errors)
        if lending:
            provider_status["lending_provider"]="TWSE TWT93U / TWT72U"

    def calculate(label: str, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}")
            return {}

    tech=calculate("Technical",calc_technical,prices)
    flow=calculate("InstitutionalOrMargin",calc_flow,inst,margin,lending)
    revenue=calculate("MonthRevenue",calc_revenue,rev)
    perdata=calculate("PER",calc_per,pers)
    financial=calculate("FinancialStatements",calc_financials,fin)
    provider_status["price_rows"]=len(prices)
    provider_status["latest_price_date"]=tech.get("last_date")
    try:
        official_financial=await fetch_official_income_statement(ticker)
    except Exception as e:
        errors.append(f"OfficialFinancial: {type(e).__name__}"); official_financial={"official":False,"errors":[type(e).__name__]}
    # V5.2.8 official mapping + EPS resolver guard: force the newest official MOPS quarter into the main payload.
    try:
        official_financial=await reconcile_official_financial_snapshot(ticker, official_financial)
    except Exception as e:
        errors.append(f"OfficialReconcile: {type(e).__name__}")
    eps_stack=await build_eps_stack(ticker, fin, official_financial, financial)
    financial_integrity=assess_financial_integrity(official_financial, eps_stack, today)
    # Official snapshot has highest priority for latest period margins/amounts.
    if official_financial.get("official"):
        financial.update({
            "statement_date":official_financial.get("statement_date") or official_financial.get("period"),
            "period":official_financial.get("period"), "source":official_financial.get("source"), "official":True,
            "ytd_eps":official_financial.get("ytd_eps"), "quarter_eps":eps_stack.get("quarter_eps"), "ttm_eps":eps_stack.get("ttm_eps"),
            "revenue":official_financial.get("revenue_ytd") if official_financial.get("revenue_ytd") is not None else financial.get("revenue"),
            "gross_profit":official_financial.get("gross_profit_ytd") if official_financial.get("gross_profit_ytd") is not None else financial.get("gross_profit"),
            "operating_income":official_financial.get("operating_income_ytd") if official_financial.get("operating_income_ytd") is not None else financial.get("operating_income"),
            "net_income":official_financial.get("net_income_ytd") if official_financial.get("net_income_ytd") is not None else financial.get("net_income"),
        })
        if financial.get("revenue"):
            financial["gross_margin"]=(financial.get("gross_profit")/financial["revenue"]*100) if financial.get("gross_profit") is not None else financial.get("gross_margin")
            financial["operating_margin"]=(financial.get("operating_income")/financial["revenue"]*100) if financial.get("operating_income") is not None else financial.get("operating_margin")
            financial["net_margin"]=(financial.get("net_income")/financial["revenue"]*100) if financial.get("net_income") is not None else financial.get("net_margin")
        if official_financial.get("gross_margin_direct") is not None:
            financial["gross_margin"]=official_financial.get("gross_margin_direct")
        if official_financial.get("operating_margin_direct") is not None:
            financial["operating_margin"]=official_financial.get("operating_margin_direct")
    if not financial_integrity.get("official_verified"):
        financial["official"]=False
        financial["source"]=(financial.get("source") or "FinMind") + "（未通過官方最新季度驗證）"
        financial["integrity_warning"]=financial_integrity.get("message")
    company_name=info.get("stock_name") or ticker
    try:
        web_research, company_events = await asyncio.gather(fetch_public_research(ticker, company_name), fetch_company_events(ticker, company_name))
    except Exception as e:
        errors.append(f"PublicWebResearch: {type(e).__name__}"); web_research={"rows":[],"errors":[type(e).__name__],"fetched_at":datetime.now().astimezone().isoformat(timespec="seconds")}; company_events={"rows":[],"errors":[type(e).__name__],"fetched_at":datetime.now().astimezone().isoformat(timespec="seconds")}
    research=merge_research(load_research(ticker), web_research.get("rows", []))
    lp=tech.get("last")
    valuation=model_valuation(lp,perdata,eps_stack,research,financial_integrity)
    sc=scores(tech,revenue,flow,perdata,financial,research)
    nar=narrative(sc,tech,revenue,flow,valuation,research)
    expectation=expectation_gap_analysis(research, company_events, perdata, revenue, sc, lp)
    prev=tech.get("series",[])[-2]["close"] if len(tech.get("series",[]))>=2 else None
    change=((lp/prev-1)*100) if lp and prev else None
    evidence_graph=build_evidence_graph(ticker,tech,revenue,flow,perdata,financial,eps_stack,research,company_events,financial_integrity)
    source_status=[
        {"name":"股價","dataset":provider_status.get("price_provider") or "TaiwanStockPrice / TWSE STOCK_DAY / TPEx tradingStock","as_of":tech.get("last_date"),"status":"ok" if tech else "missing","scheduled_update":"交易日收盤後"},
        {"name":"三大法人","dataset":provider_status.get("institutional_provider") or "FinMind / TWSE T86","as_of":flow.get("last_date"),"status":"ok" if flow.get("last_date") else "missing","scheduled_update":"交易日約 20:00"},
        {"name":"融資融券","dataset":provider_status.get("margin_provider") or "FinMind / TWSE MI_MARGN","as_of":flow.get("margin_last_date"),"status":"ok" if flow.get("margin_last_date") else "missing","scheduled_update":"交易日約 21:00"},
        {"name":"借券／借券賣出","dataset":provider_status.get("lending_provider") or "TWSE TWT72U / TWT93U","as_of":flow.get("lending_last_date"),"status":"ok" if flow.get("sbl_balance") is not None or flow.get("sbl_short_balance") is not None else "missing","scheduled_update":"交易日約 20:30–23:30"},
        {"name":"月營收","dataset":provider_status.get("revenue_provider") or "FinMind / TWSE MOPS t187ap05_L","as_of":revenue.get("last_date") or revenue.get("revenue_period"),"status":"ok" if revenue else "missing","scheduled_update":"依公司公告"},
        {"name":"PER/PBR","dataset":"TaiwanStockPER","as_of":perdata.get("last_date"),"status":"ok" if perdata else "missing","scheduled_update":"交易日約 18:00"},
        {"name":"財務報表","dataset":financial.get("source") or "FinMind TaiwanStockFinancialStatements","as_of":financial.get("period") or financial.get("statement_date"),"status":"ok" if financial_integrity.get("official_verified") else "stale","scheduled_update":"僅官方最新季度驗證通過才標示 OK"},
        {"name":"財報完整性閘門","dataset":"Official period gate","as_of":financial_integrity.get("official_period") or eps_stack.get("structured_api_period"),"status":"ok" if financial_integrity.get("core_financials_allowed") else "stale","scheduled_update":financial_integrity.get("message")},
        {"name":"公開法人研究","dataset":"Google News RSS + 公開網路引用","as_of":web_research.get("fetched_at"),"status":"ok" if web_research.get("rows") else "missing","scheduled_update":"每次強制刷新重新搜尋"},
        {"name":"公司事件雷達","dataset":"公開新聞/法說/重大訊息引用","as_of":company_events.get("fetched_at"),"status":"ok" if company_events.get("rows") else "missing","scheduled_update":"每次強制刷新重新搜尋"},
    ]
    source_status.append({"name":"Evidence Engine","dataset":"Multi-Source Evidence Schema v1","as_of":evidence_graph.get("generated_at"),"status":"ok" if evidence_graph.get("summary",{}).get("usable") else "missing","scheduled_update":f"Fact {evidence_graph.get('summary',{}).get('facts',0)} / Derived {evidence_graph.get('summary',{}).get('derived_facts',0)} / Conflicts {evidence_graph.get('summary',{}).get('conflicts',0)}"})
    conf=calc_confidence(source_status,valuation,research)
    lending_complete=info.get("type")!="上市" or flow.get("sbl_balance") is not None or flow.get("sbl_short_balance") is not None
    core_supplemental_complete=bool(flow.get("last_date") and flow.get("margin_last_date") and revenue and lending_complete)
    result_cache_ttl=CACHE_TTL if core_supplemental_complete else min(CACHE_TTL,60)
    data={"ticker":ticker,"name":info.get("stock_name") or ticker,"industry":info.get("industry_category") or "—","market_type":info.get("type") or "—",
          "generated_at":datetime.now().astimezone().isoformat(timespec="seconds"),"price":lp,"change_pct":change,"technical":tech,"revenue":revenue,"flow":flow,"per":perdata,"financial":financial,
          "research":research,"expectation_gap":expectation,"valuation":valuation,"eps_stack":eps_stack,"official_financial":official_financial,"financial_integrity":financial_integrity,"scores":sc,"stance":nar["stance"],"thesis":nar["thesis"],"catalysts":nar["catalysts"],"risks":nar["risks"],"confidence":conf,
          "source_status":source_status,"evidence_graph":evidence_graph,"errors":errors,"provider_status":provider_status,"cache":{"hit":False,"ttl_seconds":result_cache_ttl,"complete":core_supplemental_complete},"web_research_meta":web_research,"company_events":company_events,
          "data_policy":"V5.3.5 Provider Fallback：FinMind 保留為結構化來源；上市股票以 TWSE 補行情、法人、融資融券、借券及月營收。法人目標價僅採近12個月、可辨識機構且通過可信度門檻的去重資料；媒體引用與模型估值分開呈現。"}
    _CACHE[ticker]=(time.time(),data)
    return data


def report_html(d: dict[str, Any]) -> str:
    esc=lambda x: html.escape("—" if x is None else str(x))
    sc=d["scores"]; tech=d["technical"]; rev=d["revenue"]; flow=d["flow"]; per=d["per"]; fin=d["financial"]; research=d["research"]; exp=d.get("expectation_gap",{}); val=d["valuation"]; conf=d["confidence"]
    src="".join(f"<tr><td>{esc(x['name'])}</td><td>{esc(x.get('dataset'))}</td><td>{esc(x.get('as_of'))}</td><td>{esc(x['scheduled_update'])}</td><td>{'OK' if x['status']=='ok' else '缺資料'}</td></tr>" for x in d["source_status"])
    scenarios="".join(f"<tr><td>{x['name']}</td><td>{x['eps']:.2f}</td><td>{x['pe']:.1f}x</td><td><b>{x['target']:,.0f}</b></td><td>{x['upside_pct']:+.1f}%</td></tr>" for x in val.get("scenarios",[])) or "<tr><td colspan='5'>估值資料不足</td></tr>"
    rrows="".join(f"<tr><td>{esc(x.get('institution'))}</td><td>{esc(x.get('report_date'))}</td><td>{esc(x.get('rating'))}</td><td>{nfmt(safe_num(x.get('target_price')),0)}</td><td>{nfmt(safe_num(x.get('forward_eps')),2)}</td></tr>" for x in research.get("reports",[])) or "<tr><td colspan='5'>目前尚未搜尋到可解析的公開法人研究引用。</td></tr>"
    erows="".join(f"<tr><td>{esc(x.get('institution'))}</td><td>{esc(x.get('previous_date'))} → {esc(x.get('latest_date'))}</td><td>{pct(x.get('eps_revision_pct'))}</td><td>{pct(x.get('target_revision_pct'))}</td><td>{nfmt(x.get('latest_target'),0)}</td></tr>" for x in exp.get("institution_revisions",[])) or "<tr><td colspan='5'>同機構前後修正資料不足。</td></tr>"
    cats="".join(f"<li>{esc(x)}</li>" for x in d.get("catalysts",[])); risks="".join(f"<li>{esc(x)}</li>" for x in d.get("risks",[]))
    return f"""<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><style>
    @page{{size:A4;margin:11mm}} body{{font-family:'Noto Sans TC','PingFang TC',sans-serif;color:#13202a;font-size:9.5pt;line-height:1.55}} h1{{font-size:23pt;margin:0}} h2{{font-size:14pt;border-bottom:2px solid #173847;padding-bottom:4px;margin:18px 0 8px}} .muted{{color:#60727c}} .head{{display:flex;justify-content:space-between;border-bottom:3px solid #173847;padding-bottom:9px}} .price{{font-size:23pt;font-weight:800;text-align:right}} .pill{{display:inline-block;border:1px solid #719188;border-radius:20px;padding:2px 8px;margin-right:5px}} .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin:10px 0}} .card{{border:1px solid #d7e0e4;border-radius:7px;padding:8px}} .card b{{font-size:14pt;display:block}} table{{width:100%;border-collapse:collapse;font-size:8.4pt}} th,td{{padding:5px;border-bottom:1px solid #dce4e8;text-align:left}} th{{background:#f2f6f7}} .call{{border-left:4px solid #17866b;background:#f4faf8;padding:9px}} .warn{{border:1px solid #d7b94b;background:#fff9e7;padding:8px;margin-top:10px}} .small{{font-size:8pt}} .page-break{{break-before:page}} .cols{{display:grid;grid-template-columns:1fr 1fr;gap:10px}} ul{{margin:4px 0 0;padding-left:18px}} .badge{{font-weight:700}}
    </style></head><body>
    <div class='head'><div><div class='muted'>AI STOCK RESEARCH TERMINAL V5.3.5 • TAIWAN EQUITY RESEARCH</div><h1>{esc(d['name'])} <span class='muted'>{esc(d['ticker'])}</span></h1><div><span class='pill'>{esc(d['industry'])}</span><span class='pill'>{esc(d['stance'])}</span><span class='pill'>可信度 {conf['overall']}/100</span></div></div><div><div class='muted'>最新收盤</div><div class='price'>{nfmt(d['price'],1)}</div><div>{pct(d['change_pct'])}</div></div></div>
    <div class='small muted'>報告產生：{esc(d['generated_at'])} ｜ 資料完整度：{conf['data_completeness']}% ｜ 估值信心：{conf['valuation_confidence']}%</div>
    <h2>1. Executive Summary</h2><div class='call'>{esc(d['thesis'])}</div>
    <div class='grid'><div class='card'>綜合評分<b>{sc['綜合']}/100</b></div><div class='card'>基本面<b>{sc['基本面']}</b></div><div class='card'>籌碼面<b>{sc['籌碼面']}</b></div><div class='card'>技術面<b>{sc['技術面']}</b></div></div>
    <div class='cols'><div><b>主要催化劑</b><ul>{cats}</ul></div><div><b>主要風險</b><ul>{risks}</ul></div></div>
    <h2>2. Expectation Gap & Revision Radar</h2><div class='call'><b>{esc(exp.get('regime'))}</b><br>{esc(exp.get('summary'))}</div><div class='grid'><div class='card'>預期修正分數<b>{exp.get('revision_score','—')}/100</b></div><div class='card'>EPS 修正<b>{pct(research.get('eps_revision_pct'))}</b></div><div class='card'>目標價修正<b>{pct(research.get('target_revision_pct'))}</b></div><div class='card'>估值區域<b style='font-size:10pt'>{esc(exp.get('valuation_zone'))}</b></div></div><table><tr><th>法人</th><th>前次 → 最新</th><th>EPS 修正</th><th>目標價修正</th><th>最新目標</th></tr>{erows}</table><p class='small muted'>{esc(exp.get('methodology'))}</p>
    <h2>3. Fundamentals & EPS Integrity</h2><table><tr><th>最新月營收</th><th>YoY</th><th>單季 EPS</th><th>YTD EPS</th><th>TTM EPS</th><th>財報期間</th></tr><tr><td>{nfmt(rev.get('latest_revenue'),0)}</td><td>{pct(rev.get('revenue_yoy'))}</td><td>{nfmt(d.get('eps_stack',{}).get('quarter_eps'),2)}</td><td>{nfmt(d.get('eps_stack',{}).get('ytd_eps'),2)}</td><td>{nfmt(d.get('eps_stack',{}).get('ttm_eps'),2)}</td><td>{esc(fin.get('period') or fin.get('statement_date'))}</td></tr></table><p class='small muted'>{esc(d.get('eps_stack',{}).get('note'))}</p>
    <h2>4. Positioning & Technicals</h2><table><tr><th>外資20日</th><th>投信20日</th><th>融資餘額</th><th>融券餘額</th><th>券資比</th><th>借券餘額</th><th>借券賣出餘額</th></tr><tr><td>{nfmt(flow.get('foreign_20'),0)}</td><td>{nfmt(flow.get('trust_20'),0)}</td><td>{nfmt(flow.get('margin_balance'),0)} 張</td><td>{nfmt(flow.get('short_balance'),0)} 張</td><td>{pct(flow.get('short_margin_ratio_pct'))}</td><td>{nfmt(flow.get('sbl_balance'),0)} 張</td><td>{nfmt(flow.get('sbl_short_balance'),0)} 張</td></tr></table><p class='small muted'>借券餘額不等於放空；借券賣出餘額較接近尚未回補的借券放空部位。資料日 {esc(flow.get('lending_last_date') or flow.get('margin_last_date'))}。</p>
    <div class='page-break'></div><h2>5. Analyst Research & Revisions</h2><p>匯入報告數：<b>{research.get('count',0)}</b> ｜ Forward EPS 修正：<b>{pct(research.get('eps_revision_pct'))}</b></p><table><tr><th>法人/券商</th><th>日期</th><th>評等</th><th>目標價</th><th>Forward EPS</th></tr>{rrows}</table><p class='small muted'>本區彙整公開網路可取得之研究引用與使用者匯入資料；僅保存標題、摘要、數值、發布者與來源連結，不重製付費研究全文。</p>
    <h2>6. Company Events & Earnings-call Radar</h2><table><tr><th>日期</th><th>事件</th><th>發布者</th></tr>{''.join(f"<tr><td>{esc(x.get('date'))}</td><td>{esc(x.get('title'))}</td><td>{esc(x.get('publisher'))}</td></tr>" for x in d.get('company_events',{{}}).get('rows',[])[:8]) or "<tr><td colspan='3'>目前未搜尋到公司事件引用。</td></tr>"}</table>
    <h2>7. Valuation Framework</h2><p>目前採用：{esc(val.get('selected_model'))}<br>TTM：{esc(val.get('trailing_eps_basis'))}<br>Forward：{esc(val.get('forward_eps_basis'))}<br>法人共識：{nfmt(val.get('analyst_consensus',{{}}).get('median_target'),0)}（{val.get('analyst_consensus',{{}}).get('coverage',0)} 筆）<br>法人相對模型差距：{pct(val.get('analyst_consensus',{{}}).get('gap_vs_model_pct'))}<br>PE：{esc(val.get('pe_basis'))}</p><table><tr><th>情境</th><th>EPS</th><th>合理 PE</th><th>模型合理價</th><th>相對現價</th></tr>{scenarios}</table>
    <p class='small muted'>歷史 PER：P25 {nfmt(per.get('pe_p25'),1)}x / Median {nfmt(per.get('pe_median'),1)}x / P75 {nfmt(per.get('pe_p75'),1)}x；模型合理價與法人目標價分開呈現。</p>
    <h2>8. Data Lineage & Freshness</h2><table><tr><th>資料</th><th>Dataset</th><th>截至</th><th>預定更新</th><th>狀態</th></tr>{src}</table>
    <div class='warn'><b>重要揭露</b><br>本報告為研究與資訊整理工具，不構成個人化投資建議、招攬或收益保證。模型估值對 EPS 與估值倍數高度敏感；請以每列資料截至日與來源為準。</div>
    </body></html>"""


def write_report_pdf(d: dict[str, Any], output_path: Path) -> None:
    """Create a self-contained Traditional Chinese investor PDF without native Pango dependencies."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font_name="NotoSansTC"
    font_path=ROOT / "assets" / "fonts" / "NotoSansTC-Regular.ttf"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name,str(font_path)))
    styles=getSampleStyleSheet()
    body=ParagraphStyle("zh-body",parent=styles["BodyText"],fontName=font_name,fontSize=9,leading=14,textColor=colors.HexColor("#263640"))
    small=ParagraphStyle("zh-small",parent=body,fontSize=7.5,leading=11,textColor=colors.HexColor("#60727c"))
    title=ParagraphStyle("zh-title",parent=body,fontSize=21,leading=27,textColor=colors.HexColor("#13202a"),spaceAfter=4)
    section=ParagraphStyle("zh-section",parent=body,fontSize=13,leading=18,textColor=colors.HexColor("#173847"),spaceBefore=10,spaceAfter=6)
    table_header=ParagraphStyle("zh-table-header",parent=small,textColor=colors.white)
    esc=lambda value: html.escape("-" if value is None else str(value))
    para=lambda value,style=body: Paragraph(esc(value),style)
    story=[]

    def table(rows,widths=None,header=True):
        normalized=[]
        for row_index,row in enumerate(rows):
            style=table_header if header and row_index==0 else small
            normalized.append([cell if isinstance(cell,Paragraph) else para(cell,style) for cell in row])
        item=Table(normalized,colWidths=widths,repeatRows=1 if header else 0,hAlign="LEFT")
        commands=[("FONTNAME",(0,0),(-1,-1),font_name),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("GRID",(0,0),(-1,-1),.35,colors.HexColor("#d7e0e4")),("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]
        if header:
            commands.extend([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#173847")),("TEXTCOLOR",(0,0),(-1,0),colors.white)])
        item.setStyle(TableStyle(commands)); return item

    def footer(canvas,doc):
        canvas.saveState(); canvas.setFont(font_name,7); canvas.setFillColor(colors.HexColor("#60727c"))
        canvas.drawString(14*mm,8*mm,"AI Stock Research Terminal V5.3.5")
        canvas.drawRightString(A4[0]-14*mm,8*mm,f"Page {doc.page}"); canvas.restoreState()

    doc=SimpleDocTemplate(str(output_path),pagesize=A4,rightMargin=13*mm,leftMargin=13*mm,topMargin=13*mm,bottomMargin=15*mm,title=f"{d.get('ticker')} AI Stock Research V5.3.5",author="AI Stock Research Terminal")
    story.extend([para(f"{d.get('name','-')}  {d.get('ticker','-')}",title),para("AI STOCK RESEARCH TERMINAL V5.3.5 - TAIWAN EQUITY RESEARCH",small)])
    header_rows=[["最新收盤","漲跌","產業／市場","報告時間"],[nfmt(d.get("price"),1),pct(d.get("change_pct")),f"{d.get('industry','-')} / {d.get('market_type','-')}",d.get("generated_at","-")]]
    story.extend([Spacer(1,4*mm),table(header_rows,[30*mm,27*mm,48*mm,60*mm]),para("1. 投資摘要",section),para(d.get("thesis") or "目前資料不足。")])
    scores_data=d.get("scores") or {}
    story.extend([Spacer(1,2*mm),table([["綜合","基本面","籌碼面","技術面","估值"],[scores_data.get("綜合"),scores_data.get("基本面"),scores_data.get("籌碼面"),scores_data.get("技術面"),scores_data.get("估值")]],[33*mm]*5)])
    story.extend([para("主要催化劑",section),para("；".join(d.get("catalysts") or ["資料不足"])),para("主要風險",section),para("；".join(d.get("risks") or ["資料不足"]))])

    eps=d.get("eps_stack") or {}; financial=d.get("financial") or {}; revenue=d.get("revenue") or {}
    story.extend([para("2. 基本面與 EPS",section),table([["項目","數值","期間／來源"],["最新月營收",nfmt(revenue.get("latest_revenue"),0),revenue.get("revenue_period") or revenue.get("last_date")],["營收 YoY",pct(revenue.get("revenue_yoy")),revenue.get("revenue_period")],["單季 EPS",nfmt(eps.get("quarter_eps"),2),eps.get("quarter_period")],["YTD EPS",nfmt(eps.get("ytd_eps"),2),eps.get("quarter_period")],["TTM EPS",nfmt(eps.get("ttm_eps"),2),financial.get("source")]], [48*mm,42*mm,75*mm])])

    tech=d.get("technical") or {}; flow=d.get("flow") or {}
    story.extend([para("3. 籌碼與技術",section),table([["指標","1日","5日","20日"],["外資",nfmt(flow.get("foreign_1"),0),nfmt(flow.get("foreign_5"),0),nfmt(flow.get("foreign_20"),0)],["投信",nfmt(flow.get("trust_1"),0),nfmt(flow.get("trust_5"),0),nfmt(flow.get("trust_20"),0)],["自營商",nfmt(flow.get("dealer_1"),0),nfmt(flow.get("dealer_5"),0),nfmt(flow.get("dealer_20"),0)],["融資變化",pct(flow.get("margin_1_pct")),pct(flow.get("margin_5_pct")),pct(flow.get("margin_20_pct"))],["融券變化",pct(flow.get("short_1_pct")),pct(flow.get("short_5_pct")),pct(flow.get("short_20_pct"))]],[42*mm,41*mm,41*mm,41*mm]),Spacer(1,3*mm),table([["融資餘額","融券餘額","券資比","借券餘額","借券賣出餘額"],[nfmt(flow.get("margin_balance"),0),nfmt(flow.get("short_balance"),0),pct(flow.get("short_margin_ratio_pct")),nfmt(flow.get("sbl_balance"),0),nfmt(flow.get("sbl_short_balance"),0)]],[33*mm]*5),Spacer(1,3*mm),table([["趨勢","MA20","MA60","RSI14","支撐","壓力"],[tech.get("trend"),nfmt((tech.get("ma") or {}).get(20) or (tech.get("ma") or {}).get("20"),1),nfmt((tech.get("ma") or {}).get(60) or (tech.get("ma") or {}).get("60"),1),nfmt(tech.get("rsi14"),1),nfmt(tech.get("support1"),1),nfmt(tech.get("resistance"),1)]],[27.5*mm]*6)])

    story.append(PageBreak())
    research=d.get("research") or {}; reports=research.get("reports") or []
    story.append(para("4. 法人研究與預期修正",section))
    report_rows=[["機構","日期","評級","目標價","Forward EPS"]]+[[r.get("institution"),r.get("report_date"),r.get("rating"),nfmt(safe_num(r.get("target_price")),0),nfmt(safe_num(r.get("forward_eps")),2)] for r in reports[:12]]
    story.append(table(report_rows if len(report_rows)>1 else [["機構","日期","評級","目標價","Forward EPS"],["目前無可解析公開研究","-","-","-","-"]],[46*mm,29*mm,28*mm,30*mm,32*mm]))
    valuation=d.get("valuation") or {}; scenarios=valuation.get("scenarios") or []
    story.append(para("5. 估值情境",section))
    scenario_rows=[["情境","EPS","PE","模型合理價","相對現價"]]+[[s.get("name"),nfmt(s.get("eps"),2),f"{nfmt(s.get('pe'),1)}x",nfmt(s.get("target"),0),pct(s.get("upside_pct"))] for s in scenarios]
    story.append(table(scenario_rows if len(scenario_rows)>1 else [["情境","EPS","PE","模型合理價","相對現價"],["資料不足","-","-","-","-"]],[34*mm,30*mm,30*mm,37*mm,34*mm]))
    analyst=valuation.get("analyst_consensus") or {}
    story.append(para(f"TTM：{valuation.get('trailing_eps_basis') or '-'}；Forward：{valuation.get('forward_eps_basis') or '-'}；合格法人共識：{nfmt(analyst.get('median_target'),0)} 元（{analyst.get('coverage',0)} 筆）；法人相對模型差距：{pct(analyst.get('gap_vs_model_pct'))}。模型與法人假設分開呈現，不強制收斂。",small))
    story.append(para("6. 資料來源與新鮮度",section))
    source_rows=[["資料","Provider / Dataset","截至","狀態"]]+[[s.get("name"),s.get("dataset"),s.get("as_of"),"OK" if s.get("status")=="ok" else "缺資料"] for s in d.get("source_status",[])]
    story.append(table(source_rows,[30*mm,72*mm,40*mm,23*mm]))
    story.extend([Spacer(1,4*mm),para("重要聲明：本報告為研究與資訊整理工具，不構成個人化投資建議、招攬或收益保證。價格、財務與籌碼資料可能因交易所或資料供應商更新時間而延遲。",small)])
    doc.build(story,onFirstPage=footer,onLaterPages=footer)


@app.get("/")
async def home(): return FileResponse(ROOT / "index.html", headers={"Cache-Control":"no-store, max-age=0"})
@app.get("/app.js")
async def js(): return FileResponse(ROOT / "app.js", media_type="application/javascript", headers={"Cache-Control":"no-cache"})
@app.get("/styles.css")
async def css(): return FileResponse(ROOT / "styles.css", media_type="text/css", headers={"Cache-Control":"no-cache"})
@app.get("/sw.js")
async def sw(): return FileResponse(ROOT / "sw.js", media_type="application/javascript", headers={"Cache-Control":"no-store, max-age=0", "Service-Worker-Allowed":"/"})


@app.get("/api/diagnostics/financial/{ticker}")
async def financial_diagnostics(ticker: str):
    ticker=require_numeric_taiwan_ticker(ticker)
    result=await diagnose_official_financial_sources(ticker)
    return JSONResponse(result, headers={"Cache-Control":"no-store"})

@app.get("/api/stock/{ticker}")
async def stock_api(ticker: str, refresh: bool = Query(False)):
    ticker=require_numeric_taiwan_ticker(ticker)
    d=await build_stock(ticker, force_refresh=refresh)
    if d["price"] is None and d["name"] == ticker: raise HTTPException(404,"查無股票或資料來源暫時無法連線")
    return JSONResponse(d, headers={"Cache-Control":"no-store"})

@app.get("/api/stock/{ticker}/pdf")
async def stock_pdf(ticker: str, refresh: bool = Query(True)):
    ticker=require_numeric_taiwan_ticker(ticker)
    d=await build_stock(ticker, force_refresh=refresh)
    if d["price"] is None: raise HTTPException(503,"目前無法取得股價資料，為避免輸出錯誤報告，PDF 未產生。")
    stamp=datetime.now().strftime("%Y%m%d_%H%M")
    out=REPORT_DIR/f"{ticker}_{stamp}_research_v5_3_5.pdf"
    try:
        write_report_pdf(d,out)
    except Exception as exc:
        raise HTTPException(500,f"PDF 產生失敗：{type(exc).__name__}") from exc
    return FileResponse(out,media_type="application/pdf",filename=f"{ticker}_AI_research_V5_3_5_{stamp}.pdf")

@app.post("/api/cache/clear")
async def cache_clear():
    _CACHE.clear(); return {"status":"ok","message":"cache cleared"}

@app.get("/api/diagnostics/eps/{ticker}")
async def eps_diagnostics(ticker: str):
    ticker=require_numeric_taiwan_ticker(ticker)
    official=await fetch_official_income_statement(ticker)
    official=await reconcile_official_financial_snapshot(ticker, official)
    fin_rows=[]
    finmind_error=None
    try:
        start=(date.today()-timedelta(days=900)).isoformat()
        fin_rows=await fm("TaiwanStockFinancialStatements", ticker, start)
    except Exception as e:
        finmind_error=f"{type(e).__name__}: {e}"
        fin_rows=[]
    stack=await build_eps_stack(ticker,fin_rows,official,{})
    raw_periods=[]
    fy=official.get("fiscal_year"); fq=official.get("fiscal_quarter")
    if fy and fq:
        periods=[]; y,q=int(fy),int(fq)
        for _ in range(5):
            periods.append((y,q)); q-=1
            if q==0: y-=1; q=4
        for y,q in periods:
            raw_periods.append({
                "period":f"{y} Q{q}",
                "raw_trace_url":f"/api/diagnostics/eps-raw/{ticker}?year={y}&quarter={q}"
            })
    return {
        "ticker":ticker,"version":"5.3.5",
        "official_current":{k:official.get(k) for k in ("source","endpoint","period","fiscal_year","fiscal_quarter","ytd_eps","quarter_eps_direct","report_id")},
        "finmind_error":finmind_error,
        "eps_stack":stack,
        "raw_period_trace_links":raw_periods,
        "note":"正式 EPS 路徑使用 MOPS 官方歷史彙總表，不依賴已封鎖的公司別舊版 HTML；raw endpoint 僅保留供故障排查。"
    }

@app.get("/api/diagnostics/eps-raw/{ticker}")
async def eps_raw_diagnostics(ticker: str, year: int = Query(..., ge=1990, le=2100), quarter: int = Query(..., ge=1, le=4)):
    ticker=require_numeric_taiwan_ticker(ticker)
    return await trace_mops_company_ifrs(ticker,year,quarter)

@app.get("/api/diagnostics/eps-registry/{ticker}")
async def eps_registry_diagnostics(ticker: str):
    ticker=require_numeric_taiwan_ticker(ticker)
    reg=_load_official_eps_registry()
    rows=[r for r in reg.get("records",[]) if str(r.get("ticker"))==ticker]
    rows=sorted(rows,key=lambda r:(r.get("year") or 0,r.get("quarter") or 0),reverse=True)
    return {"ticker":ticker,"version":"5.3.5","registry_schema_version":reg.get("schema_version"),
            "updated_at":reg.get("updated_at"),"record_count":len(rows),"records":rows}

@app.get("/api/evidence/{ticker}")
async def evidence_api(ticker: str, refresh: int = 0):
    ticker=normalize_ticker(ticker)
    if not re.fullmatch(r"[0-9A-Z]{2,10}", ticker): raise HTTPException(400,"invalid ticker")
    d=await build_stock(ticker, bool(refresh))
    return d.get("evidence_graph") or {}

@app.get("/api/diagnostics/providers/{ticker}")
async def provider_diagnostics(ticker: str):
    ticker=require_numeric_taiwan_ticker(ticker)
    d=await build_stock(ticker, True)
    status=d.get("provider_status") or {}
    provider_errors=list(dict.fromkeys([*(status.get("fallback_errors") or []),*(d.get("errors") or [])]))
    return {"version":"5.3.5","ticker":ticker,"finmind_available":bool(status.get("finmind_available")),
            "finmind_price_available":bool(status.get("finmind_price_available")),
            "official_price_fallback_success":bool(status.get("official_price_fallback_success")),
            "twse_fallback_success":bool(status.get("twse_fallback_success")),"tpex_fallback_success":bool(status.get("tpex_fallback_success")),"price_provider":status.get("price_provider"),
            "info_provider":status.get("info_provider"),"price_rows":status.get("price_rows",0),
            "latest_price_date":status.get("latest_price_date"),"provider_errors":provider_errors}

@app.get("/health")
async def health(): return {"status":"ok","version":"5.3.5","mode":"cloud-mobile-earnings-call-events","finmind_token":bool(FINMIND_TOKEN),"cache_ttl_seconds":CACHE_TTL,"pwa":True}
