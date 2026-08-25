from __future__ import annotations

import asyncio
import secrets
import html
import json
import math
import os
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
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
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.gzip import GZipMiddleware
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
APP_VERSION = "5.17.7"
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "generated_reports"
DATA_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)

FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "").strip()
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "600"))
CACHE_ADMIN_TOKEN = os.getenv("CACHE_ADMIN_TOKEN", "").strip()
PDF_CACHE_TTL = min(CACHE_TTL, int(os.getenv("PDF_CACHE_TTL_SECONDS", "600")))
PDF_RENDERER = os.getenv("PDF_RENDERER", "reportlab").strip().lower()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_OFFICIAL_HISTORY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_OFFICIAL_HISTORY_TASKS: dict[str, asyncio.Task] = {}
_OFFICIAL_HISTORY_JOBS: dict[str, dict[str, dict[str, Any]]] = {}
_OFFICIAL_FINANCIAL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_OFFICIAL_FINANCIAL_TASKS: dict[str, asyncio.Task] = {}
_OFFICIAL_FINANCIAL_JOBS: dict[str, dict[str, Any]] = {}
_MCP_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MCP_TASKS: dict[str, asyncio.Task] = {}
_PRICE_HISTORY_CACHE: dict[str, tuple[float, list[dict[str, Any]], str]] = {}
_LONG_PRICE_HISTORY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_PDF_REPORT_CACHE: dict[str, dict[str, Any]] = {}
_PDF_REPORT_LOCKS: dict[str, asyncio.Lock] = {}
_WEASYPRINT_DISABLED = False

app = FastAPI(title="AI Stock Research Terminal", version=APP_VERSION)
app.add_middleware(GZipMiddleware, minimum_size=800)


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


def pct(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v:+.{digits}f}%"


def nfmt(v: float | int | None, digits: int = 1) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.{digits}f}"



def _looks_like_html_or_css(text: str) -> bool:
    t=(text or "").lstrip().lower()
    return (
        t.startswith("<!doctype html") or t.startswith("<html") or t.startswith("<style")
        or "@font-face" in t[:500] or "font-family:" in t[:500]
    )

def _clean_provider_error(name: str, exc: Exception) -> str:
    msg=str(exc).replace("\n"," ").strip()
    if len(msg)>220: msg=msg[:220]+"…"
    return f"{name}: {type(exc).__name__}: {msg}"


async def _safe_http_json(client: httpx.AsyncClient, method: str, url: str, *, provider: str,
                          params: dict[str,Any] | None=None, json_body: Any=None,
                          data: Any=None, timeout: float | None=None) -> tuple[Any,dict[str,Any]]:
    """Hard-isolation HTTP wrapper.
    Any upstream HTML/CSS/font/error body becomes a compact provider error, never raw response text.
    """
    meta={"provider":provider,"url":url}
    try:
        kwargs={}
        if params is not None: kwargs["params"]=params
        if json_body is not None: kwargs["json"]=json_body
        if data is not None: kwargs["data"]=data
        if timeout is not None: kwargs["timeout"]=timeout
        r=await client.request(method,url,**kwargs)
        meta["http_status"]=r.status_code
        meta["content_type"]=(r.headers.get("content-type") or "").split(";")[0]
        raw=r.text or ""
        if r.status_code>=400:
            raise RuntimeError(f"HTTP {r.status_code}")
        ctype=(r.headers.get("content-type") or "").lower()
        if "text/html" in ctype or "text/css" in ctype or "font/" in ctype or _looks_like_html_or_css(raw):
            raise RuntimeError(f"unexpected upstream content-type {ctype or 'unknown'}")
        try:
            return r.json(),meta
        except Exception:
            raise RuntimeError("upstream response is not valid JSON")
    except Exception as e:
        meta["status"]="error"
        meta["error"]=_clean_provider_error(provider,e)
        return None,meta

def _compact_error_payload(errors: list[Any] | None, limit: int=8) -> list[str]:
    out=[]
    for e in errors or []:
        x=str(e).replace("\n"," ").strip()
        # Kill accidental HTML/CSS/font/base64 leakage.
        if "@font-face" in x or "base64," in x or "<html" in x.lower() or "<!doctype" in x.lower():
            x="上游服務回傳非 JSON 錯誤頁，內容已隱藏"
        if len(x)>180: x=x[:180]+"…"
        if x and x not in out: out.append(x)
        if len(out)>=limit: break
    return out

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
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        ctype=(r.headers.get("content-type") or "").lower()
        if "text/html" in ctype or "text/css" in ctype or _looks_like_html_or_css(r.text):
            raise RuntimeError(f"unexpected content-type/body: {ctype or 'unknown'}")
        payload = r.json()
    if payload.get("status") not in (200, None):
        raise RuntimeError(payload.get("msg") or f"FinMind {dataset} failed")
    return payload.get("data") or []


TWSE_OPENAPI = "https://openapi.twse.com.tw/v1"
TPEX_OPENAPI = "https://www.tpex.org.tw/openapi/v1"
TWSTOCK_MCP_URL = os.getenv("TWSTOCK_MCP_URL", "https://TW-Stock-MCP-Server.fastmcp.app/mcp").strip()
TWSTOCK_MCP_ENABLED = os.getenv("TWSTOCK_MCP_ENABLED", "1").strip().lower() in {"1","true","yes","on"}

PROVIDER_REGISTRY = [
    {"id":"twse","name":"TWSE OpenAPI/Web API","tier":1,"authority":"official","role":"primary","enabled":True},
    {"id":"tpex","name":"TPEx OpenAPI","tier":1,"authority":"official","role":"primary","enabled":True},
    {"id":"company_ir","name":"Company IR / Earnings Release","tier":1,"authority":"company_official","role":"primary","enabled":True},
    {"id":"eps_registry","name":"Official EPS Registry","tier":1,"authority":"verified_registry","role":"historical_backfill","enabled":True},
    {"id":"twstock_mcp","name":"TWStock MCP compatible adapter","tier":2,"authority":"aggregator","role":"secondary_crosscheck","enabled":TWSTOCK_MCP_ENABLED,"url":TWSTOCK_MCP_URL},
    {"id":"finmind","name":"FinMind","tier":3,"authority":"third_party","role":"fallback","enabled":True},
    {"id":"yahoo","name":"Yahoo Finance chart API (yfinance-compatible)","tier":3,"authority":"third_party","role":"price_history_fallback","enabled":True},
    {"id":"public_web","name":"Public Web Research","tier":4,"authority":"public_web","role":"research_context","enabled":True},
]

def parse_num_text(v: Any) -> float | None:
    if v is None: return None
    s=str(v).strip().replace(",", "").replace("％", "").replace("%", "")
    if s in ("", "-", "--", "—", "－"): return None
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


def _twse_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows: list[dict[str, Any]] = []
    for table in payload.get("tables") or [payload]:
        if not isinstance(table, dict):
            continue
        fields = table.get("fields") or []
        for row in table.get("data") or []:
            rows.append(row if isinstance(row, dict) else dict(zip(fields, row)))
    return rows


def _normalized_key(value: Any) -> str:
    return re.sub(r"[\s　]", "", str(value or ""))


def _twse_ticker_matches(row: dict[str, Any], ticker: str) -> bool:
    target = str(ticker).strip()
    for key, value in row.items():
        normalized = _normalized_key(key).lower()
        if any(token in normalized for token in ("證券代號", "股票代號", "code", "stock_id", "stockno")):
            return _normalized_key(value).removesuffix(".0") == target
    return False


def _twse_number(row: dict[str, Any], includes: tuple[str, ...], excludes: tuple[str, ...] = ()) -> float | None:
    for key, value in row.items():
        normalized = _normalized_key(key)
        if all(token in normalized for token in includes) and not any(token in normalized for token in excludes):
            parsed = parse_num_text(value)
            if parsed is not None:
                return parsed
    return None


def _t86_participant(row: dict[str, Any], participant: str) -> dict[str, float | None] | None:
    tokens = {
        "foreign": (("外陸資",), ("外資及陸資",), ("外資",)),
        "trust": (("投信",),),
        "dealer": (("自營商",),),
    }[participant]
    # Foreign columns contain "不含外資自營商", while dealer matching must not
    # interpret that phrase as the domestic dealer column.
    excludes: tuple[str, ...] = ("外陸資", "外資及陸資", "外資") if participant == "dealer" else ()

    def find(kind: str) -> float | None:
        for token_set in tokens:
            value = _twse_number(row, token_set + (kind,), excludes)
            if value is not None:
                return value
        return None

    buy, sell, net = find("買進"), find("賣出"), find("買賣超")
    if participant == "dealer" and (buy is None or sell is None):
        dealer_buy = [_twse_number(row, ("自營商", "買進", kind)) for kind in ("自行買賣", "避險")]
        dealer_sell = [_twse_number(row, ("自營商", "賣出", kind)) for kind in ("自行買賣", "避險")]
        if buy is None and any(value is not None for value in dealer_buy):
            buy = sum(value or 0 for value in dealer_buy)
        if sell is None and any(value is not None for value in dealer_sell):
            sell = sum(value or 0 for value in dealer_sell)
    if net is None and buy is not None and sell is not None:
        net = buy - sell
    return None if net is None else {"buy": buy, "sell": sell, "net": net}


async def fetch_twse_t86_latest(ticker: str, price: float | None, anchor: date | None = None) -> dict[str, Any]:
    """Fetch the nearest official T86 trading day using one stable schema."""
    end = anchor or date.today()
    attempts: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 AI-Stock-Research"}) as client:
        for back in range(12):
            day = end - timedelta(days=back)
            if day.weekday() >= 5:
                continue
            try:
                response = await client.get("https://www.twse.com.tw/rwd/zh/fund/T86", params={"date": day.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"})
                response.raise_for_status()
                rows = _twse_rows(response.json())
                row = next((item for item in rows if _twse_ticker_matches(item, ticker)), None)
                attempts.append({"date": day.isoformat(), "rows": len(rows), "matched": bool(row)})
                if not row:
                    continue
                institutional: dict[str, Any] = {}
                flat: dict[str, Any] = {"last_date": day.isoformat()}
                for participant in ("foreign", "trust", "dealer"):
                    values = _t86_participant(row, participant)
                    if not values:
                        continue
                    cash = {key: (value * price if value is not None and price else None) for key, value in values.items()}
                    cash.update({"shares_net": values["net"], "days": 1})
                    institutional[participant] = {"1": cash}
                    flat[f"{participant}_1"] = cash["net"] if cash["net"] is not None else values["net"]
                if institutional:
                    company_name = next((str(value).strip() for key, value in row.items() if "證券名稱" in _normalized_key(key) and str(value).strip()), None)
                    return {"institutional": institutional, "flow": flat, "last_date": day.isoformat(), "source": "TWSE T86 official", "company_name": company_name, "attempts": attempts}
            except Exception as exc:
                attempts.append({"date": day.isoformat(), "error": type(exc).__name__})
    return {"institutional": {}, "flow": {}, "last_date": None, "source": "TWSE T86 official", "attempts": attempts}


OFFICIAL_JSON_HEADERS = {"User-Agent": f"Mozilla/5.0 AI-Stock-Research/{APP_VERSION}"}


def _roc_date(raw: Any) -> str:
    digits = "".join(c for c in str(raw or "") if c.isdigit())
    if len(digits) == 7:
        return f"{int(digits[:3]) + 1911:04d}-{digits[3:5]}-{digits[5:7]}"
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


def parse_twse_revenue_rows(payload: Any, ticker: str) -> list[dict[str, Any]]:
    rows = payload if isinstance(payload, list) else []
    hit = next((r for r in rows if str(r.get("公司代號") or "").strip() == ticker), None)
    if not hit:
        return []
    digits = "".join(c for c in str(hit.get("資料年月") or "") if c.isdigit())
    if len(digits) < 5:
        return []
    raw_year, month = int(digits[:-2]), int(digits[-2:])
    year = raw_year + 1911 if raw_year < 1911 else raw_year
    current = parse_num_text(hit.get("營業收入-當月營收") or hit.get("當月營收"))
    previous_month = parse_num_text(hit.get("營業收入-上月營收") or hit.get("上月營收"))
    prior = parse_num_text(hit.get("營業收入-去年當月營收") or hit.get("去年當月營收"))
    previous_year, previous_month_number = (year - 1, 12) if month == 1 else (year, month - 1)
    # The MOPS OpenAPI reports revenue in NT$ thousands.
    out = []
    for y, m, value, source in ((year - 1, month, prior, "prior-year comparison"),
                                (previous_year, previous_month_number, previous_month, "previous month"),
                                (year, month, current, "current month")):
        if value is not None:
            out.append({"stock_id": ticker, "revenue_year": y, "revenue_month": m,
                        "revenue": value * 1000, "date": f"{y:04d}-{m:02d}-01",
                        "_source": f"TWSE/MOPS t187ap05_L {source}"})
    return out


def parse_mops_monthly_revenue_html(content: bytes | str, ticker: str, year: int, month: int) -> list[dict[str, Any]]:
    """Parse one official MOPS historical monthly-revenue page (unit: NT$ thousand)."""
    text = content.decode("big5", errors="ignore") if isinstance(content, bytes) else str(content)
    try:
        document = lxml_html.fromstring(text)
    except Exception:
        return []
    for row in document.xpath("//tr"):
        cells = [" ".join(cell.text_content().split()) for cell in row.xpath("./td")]
        if len(cells) < 3 or cells[0].strip() != ticker:
            continue
        value = parse_num_text(cells[2])
        if value is None:
            return []
        return [{"stock_id": ticker, "revenue_year": year, "revenue_month": month,
                 "revenue": value * 1000, "date": f"{year:04d}-{month:02d}-01",
                 "_source": "MOPS official historical monthly revenue"}]
    return []


async def fetch_mops_monthly_revenue_history(client: httpx.AsyncClient, ticker: str, anchor: date, months: int = 24) -> list[dict[str, Any]]:
    """Fetch 24 announced monthly facts from MOPS historical pages."""
    cursor = date(anchor.year, anchor.month, 1) - timedelta(days=1)
    targets: list[tuple[int, int]] = []
    for _ in range(months):
        targets.append((cursor.year, cursor.month))
        cursor = date(cursor.year, cursor.month, 1) - timedelta(days=1)
    sem = asyncio.Semaphore(6)

    async def one(year: int, month: int) -> list[dict[str, Any]]:
        roc_year = year - 1911
        async with sem:
            # MOPS splits some issuers (including KY companies) into the _1
            # report. Query both official partitions instead of assuming _0.
            for market in ("sii", "otc"):
                for partition in (0, 1):
                    try:
                        response = await client.get(f"https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{roc_year}_{month}_{partition}.html")
                        response.raise_for_status()
                        parsed = parse_mops_monthly_revenue_html(response.content, ticker, year, month)
                        if parsed:
                            return parsed
                    except Exception:
                        continue
        return []

    batches = await asyncio.gather(*(one(year, month) for year, month in targets))
    rows = [row for batch in batches for row in batch]
    return sorted(rows, key=lambda row: row["date"])[-months:]


def parse_twse_institutional_payload(payload: Any, ticker: str, day: str) -> list[dict[str, Any]]:
    row = next((r for r in _twse_rows(payload) if _twse_ticker_matches(r, ticker)), None)
    if not row:
        return []
    company_name = next((str(value).strip() for key, value in row.items()
                         if "證券名稱" in _normalized_key(key) and str(value).strip()), None)
    out = {"date": day, "stock_id": ticker, "_source": "TWSE T86 official", "_company_name": company_name}
    prefixes = {"foreign": "Foreign_Investor", "trust": "Investment_Trust", "dealer": "Dealer"}
    for participant, prefix in prefixes.items():
        values = _t86_participant(row, participant)
        if values:
            out[f"{prefix}_buy"] = values.get("buy")
            out[f"{prefix}_sell"] = values.get("sell")
    return [out] if len(out) > 3 else []


def parse_twse_margin_payload(payload: Any, ticker: str, day: str) -> list[dict[str, Any]]:
    tables = payload.get("tables") or [] if isinstance(payload, dict) else []
    if not tables and isinstance(payload, dict) and payload.get("data"):
        tables = [{"fields": payload.get("fields") or [], "data": payload.get("data") or []}]
    for table in tables:
        fields = table.get("fields") or []
        for raw in table.get("data") or []:
            row = dict(zip(fields, raw))
            code = str(row.get("代號") or row.get("股票代號") or row.get("證券代號") or "").strip()
            if code != ticker:
                continue
            # Some tables repeat generic balance headings: margin occupies the
            # first block and short sale the second block.
            if "今日餘額" in fields:
                indexes = [i for i, field in enumerate(fields) if field == "今日餘額"]
                margin = parse_num_text(raw[indexes[0]]) if indexes else None
                short = parse_num_text(raw[indexes[1]]) if len(indexes) > 1 else None
            else:
                margin = _twse_number(row, ("融資", "今日餘額")) or _twse_number(row, ("融資", "餘額"))
                short = _twse_number(row, ("融券", "今日餘額")) or _twse_number(row, ("融券", "餘額"))
            if margin is not None or short is not None:
                return [{"date": day, "stock_id": ticker, "MarginPurchaseTodayBalance": margin,
                         "ShortSaleTodayBalance": short, "_source": "TWSE MI_MARGN official"}]
    return []


def parse_twse_lending_snapshots(short_payload: dict[str, Any], borrow_payload: dict[str, Any], ticker: str, trading_date: str) -> dict[str, Any]:
    """Normalize official securities lending and SBL short-sale share counts to lots."""
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


async def fetch_official_market_supplements(ticker: str, anchor: date | None = None, history_days: int = 45,
                                            include_revenue_history: bool = True) -> dict[str, Any]:
    """Bounded official fallbacks for datasets that otherwise require FinMind."""
    end = anchor or date.today()
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=OFFICIAL_JSON_HEADERS) as client:
        async def get_json(url: str, params: dict[str, Any] | None = None):
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

        async def revenue():
            latest = parse_twse_revenue_rows(await get_json("https://openapi.twse.com.tw/v1/opendata/t187ap05_L"), ticker)
            if not history_days or not include_revenue_history:
                return latest
            history = await fetch_mops_monthly_revenue_history(client, ticker, end, 24)
            merged = {row["date"]: row for row in latest + history}
            return [merged[key] for key in sorted(merged)][-24:]

        async def valuation():
            payload = await get_json("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL")
            for row in payload if isinstance(payload, list) else []:
                if str(row.get("Code") or row.get("證券代號") or "").strip() == ticker:
                    return [{"date": _roc_date(row.get("Date") or row.get("日期")), "stock_id": ticker,
                             "PER": parse_num_text(row.get("PEratio") or row.get("本益比")),
                             "PBR": parse_num_text(row.get("PBratio") or row.get("股價淨值比")),
                             "dividend_yield": parse_num_text(row.get("DividendYield") or row.get("殖利率")),
                             "_source": "TWSE BWIBBU official"}]
            return []

        async def history(kind: str):
            output = []
            sem = asyncio.Semaphore(5)
            async def one(day: date):
                if day.weekday() >= 5:
                    return []
                path = "fund/T86" if kind == "institutional" else "marginTrading/MI_MARGN"
                try:
                    payload = await get_json(f"https://www.twse.com.tw/rwd/zh/{path}",
                                             {"date": day.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"})
                    return (parse_twse_institutional_payload if kind == "institutional" else parse_twse_margin_payload)(payload, ticker, day.isoformat())
                except Exception:
                    return []
            async def guarded(day: date):
                async with sem:
                    return await one(day)
            # Resolve the newest trading day before launching history requests;
            # this avoids a rate-limited batch silently making the data stale.
            async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=OFFICIAL_JSON_HEADERS) as latest_client:
                for i in range(12):
                    day = end - timedelta(days=i)
                    if day.weekday() >= 5:
                        continue
                    path = "fund/T86" if kind == "institutional" else "marginTrading/MI_MARGN"
                    try:
                        response = await latest_client.get(f"https://www.twse.com.tw/rwd/zh/{path}",
                                                           params={"date": day.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"})
                        response.raise_for_status()
                        parser = parse_twse_institutional_payload if kind == "institutional" else parse_twse_margin_payload
                        latest = parser(response.json(), ticker, day.isoformat())
                    except Exception:
                        latest = []
                    if latest:
                        output.extend(latest)
                        break
            # Margin/short charts need roughly 60 trading sessions. Keep the
            # institutional request bounded at its existing lookback while the
            # background-only margin job covers about 90 calendar days.
            lookback_days = max(history_days, 90) if kind == "margin" and history_days else history_days
            batches = await asyncio.gather(*(guarded(end - timedelta(days=i)) for i in range(lookback_days)))
            for batch in batches:
                output.extend(batch)
            dedup = {row["date"]: row for row in output}
            limit = 60 if kind == "margin" else 21
            return [dedup[key] for key in sorted(dedup)][-limit:]

        results = await asyncio.gather(revenue(), valuation(), history("institutional"), history("margin"), return_exceptions=True)
    keys = ("revenue", "valuation", "institutional", "margin")
    return {key: ([] if isinstance(value, Exception) else value) for key, value in zip(keys, results)}


def _history_job(ticker: str, provider: str) -> dict[str, Any]:
    jobs = _OFFICIAL_HISTORY_JOBS.setdefault(ticker, {})
    return jobs.setdefault(provider, {"status": "queued", "rows": 0, "attempted_days": 0,
                                      "errors": 0, "last_error": None, "updated_at": None})


def _save_official_history(ticker: str, provider: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    prior = _OFFICIAL_HISTORY_CACHE.get(ticker)
    merged = dict(prior[1]) if prior else {}
    existing = merged.get(provider) or []
    by_date = {str(row.get("date")): row for row in existing if row.get("date")}
    by_date.update({str(row.get("date")): row for row in rows if row.get("date")})
    limit = 60 if provider == "margin" else 24 if provider == "revenue" else 21
    merged[provider] = [by_date[key] for key in sorted(by_date)][-limit:]
    _OFFICIAL_HISTORY_CACHE[ticker] = (time.time(), merged)
    _CACHE.pop(ticker, None)


def _history_revision(ticker: str) -> float:
    cached = _OFFICIAL_HISTORY_CACHE.get(ticker)
    return cached[0] if cached and time.time() - cached[0] < 3600 else 0


def _cached_stock_is_current(ticker: str, payload: dict[str, Any]) -> bool:
    """Never serve a report built from an older incremental history snapshot."""
    return _history_revision(ticker) <= float(payload.get("history_cache_at_used") or 0)


async def _warm_market_provider(ticker: str, provider: str) -> None:
    job = _history_job(ticker, provider)
    job.update(status="running", rows=0, attempted_days=0, errors=0, last_error=None,
               started_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    target = 21 if provider == "institutional" else 60
    lookback = 60 if provider == "institutional" else 130
    path = "fund/T86" if provider == "institutional" else "marginTrading/MI_MARGN"
    parser = parse_twse_institutional_payload if provider == "institutional" else parse_twse_margin_payload
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=OFFICIAL_JSON_HEADERS) as client:
            collected: list[dict[str, Any]] = []
            for offset in range(lookback):
                day = date.today() - timedelta(days=offset)
                if day.weekday() >= 5:
                    continue
                job["attempted_days"] += 1
                last_error = None
                for attempt in range(3):
                    try:
                        response = await client.get(
                            f"https://www.twse.com.tw/rwd/zh/{path}",
                            params={"date": day.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"},
                        )
                        response.raise_for_status()
                        rows = parser(response.json(), ticker, day.isoformat())
                        if rows:
                            collected.extend(rows)
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                        job["errors"] += 1
                        if attempt < 2:
                            await asyncio.sleep(0.8 * (attempt + 1))
                if last_error:
                    job["last_error"] = last_error
                unique_count = len({row["date"] for row in collected})
                if collected and (unique_count % 4 == 0 or unique_count >= target):
                    _save_official_history(ticker, provider, collected)
                    job["rows"] = unique_count
                    job["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                if unique_count >= target:
                    break
                await asyncio.sleep(0.15)
            _save_official_history(ticker, provider, collected)
            job["rows"] = len({row["date"] for row in collected})
            job["status"] = "complete" if job["rows"] >= (20 if provider == "institutional" else 21) else "partial"
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        raise
    except Exception as exc:
        job.update(status="error", last_error=f"{type(exc).__name__}: {str(exc)[:160]}")
    finally:
        job["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _OFFICIAL_HISTORY_TASKS.pop(f"{ticker}:{provider}", None)


async def _warm_revenue_provider(ticker: str) -> None:
    provider = "revenue"
    job = _history_job(ticker, provider)
    job.update(status="running", rows=0, attempted_days=0, errors=0, last_error=None,
               started_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=OFFICIAL_JSON_HEADERS) as client:
            cursor = date(date.today().year, date.today().month, 1) - timedelta(days=1)
            collected: list[dict[str, Any]] = []
            for _ in range(24):
                year, month = cursor.year, cursor.month
                cursor = date(year, month, 1) - timedelta(days=1)
                job["attempted_days"] += 1
                found: list[dict[str, Any]] = []
                for market in ("sii", "otc"):
                    for partition in (0, 1):
                        try:
                            response = await client.get(
                                f"https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{year - 1911}_{month}_{partition}.html"
                            )
                            response.raise_for_status()
                            found = parse_mops_monthly_revenue_html(response.content, ticker, year, month)
                        except Exception as exc:
                            job["errors"] += 1
                            job["last_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                        if found:
                            break
                    if found:
                        break
                if found:
                    collected.extend(found)
                    _save_official_history(ticker, provider, collected)
                    job["rows"] = len({row["date"] for row in collected})
                    job["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                await asyncio.sleep(0.15)
            job["status"] = "complete" if job["rows"] >= 20 else "partial"
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        raise
    except Exception as exc:
        job.update(status="error", last_error=f"{type(exc).__name__}: {str(exc)[:160]}")
    finally:
        job["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _OFFICIAL_HISTORY_TASKS.pop(f"{ticker}:{provider}", None)


def schedule_official_history(ticker: str) -> None:
    cached_at, cached = _OFFICIAL_HISTORY_CACHE.get(ticker) or (0, {})
    cache_is_fresh = time.time() - cached_at < 3600
    requirements = {"institutional": 20, "margin": 21, "revenue": 20}
    for provider, required in requirements.items():
        if cache_is_fresh and len(cached.get(provider) or []) >= required:
            continue
        key = f"{ticker}:{provider}"
        task = _OFFICIAL_HISTORY_TASKS.get(key)
        if task and not task.done():
            continue
        worker = _warm_revenue_provider(ticker) if provider == "revenue" else _warm_market_provider(ticker, provider)
        _OFFICIAL_HISTORY_TASKS[key] = asyncio.create_task(worker)



MOPS_CSV_BASE = "https://mopsfin.twse.com.tw/opendata"
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
    year=val("年度","Year") or _row_value(row,["年度"]) or _row_value(row,["年"])
    quarter=val("季別") or _row_value(row,["季別"]) or _row_value(row,["季"])
    out_date=val("出表日期","資料日期","Date") or _row_value(row,["出表日期"]) or _row_value(row,["資料日期"])
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
    """Parse official historical EPS by header, never by a fixed column index."""
    snapshots: dict[str, dict[str, Any]] = {}
    for table in _mops_lxml_rows(html_text):
        rows=table.get("rows") or []
        header_index=code_index=name_index=eps_index=None
        for ri,row in enumerate(rows):
            normalized=[_norm_label(cell) for cell in row]
            for ci,label in enumerate(normalized):
                if code_index is None and ("公司代號" in label or label in {"代號","公司代碼"}): code_index=ci
                if name_index is None and ("公司名稱" in label or label in {"名稱","公司簡稱"}): name_index=ci
                if eps_index is None and ("基本每股盈餘" in label or "basicearningspershare" in label): eps_index=ci
            if code_index is not None and eps_index is not None:
                header_index=ri
                break
        if header_index is None or code_index is None or eps_index is None:
            continue
        for row in rows[header_index+1:]:
            if max(code_index,eps_index)>=len(row):
                continue
            ticker=re.sub(r"\D","",str(row[code_index] or ""))
            if not re.fullmatch(r"\d{4,6}",ticker):
                continue
            eps=parse_num_text(row[eps_index])
            if eps is None:
                continue
            company_name=str(row[name_index]).strip() if name_index is not None and name_index<len(row) else ticker
            snapshots[ticker]={
                "source":"MOPS historical income statement summary",
                "market":"上市" if market_type=="sii" else "上櫃",
                "official":True,"period":f"{year} Q{quarter}","fiscal_year":year,"fiscal_quarter":quarter,
                "feed_kind":"mops_historical_summary","company_code":ticker,"company_name":company_name,
                "ytd_eps":float(eps),"quarter_eps_direct":float(eps) if quarter==1 else None,
                "eps_provenance":"official_mops_historical","eps_confidence":100,
                "report_id":"t163sb04","table_index":table.get("table_index"),"completeness":1,
            }
    return snapshots

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


def _income_amounts_from_lxml_rows(tables: list[dict[str, Any]], quarter: int) -> dict[str, float|None]:
    """Extract current YTD income-statement amounts from company MOPS tables."""
    aliases={
        "revenue_ytd":("營業收入合計","營業收入","operating revenue"),
        "gross_profit_ytd":("營業毛利（毛損）淨額","營業毛利（毛損）","營業毛利","gross profit"),
        "operating_income_ytd":("營業利益（損失）","營業利益","profit from operations","operating income"),
        "net_income_ytd":("本期淨利（淨損）","本期淨利","本期稅後淨利","net profit for the period","net income"),
    }
    found={key:None for key in aliases}
    for table in tables:
        for row in table.get("rows") or []:
            label=" | ".join(row[:3])
            target=next((key for key,names in aliases.items()
                         if found[key] is None and any(_norm_label(name) in _norm_label(label) for name in names)),None)
            if target is None:
                continue
            nums=[]
            for value in row[1:]:
                number=parse_num_text(value)
                if number is not None:
                    nums.append(float(number))
            if not nums:
                continue
            pick=0 if quarter==1 else (2 if quarter in (2,3) and len(nums)>=3 else 0)
            found[target]=nums[pick]
    return found

def _extract_mops_ifrs_tables(html_text: str, ticker: str, year: int, quarter: int, report_id: str, url: str) -> dict[str, Any] | None:
    """Parse a company-specific MOPS IFRS page using lxml, without html5lib."""
    if "查無資料" in html_text or "無符合條件" in html_text or "HTTP Status 404" in html_text: return None
    tables=_mops_lxml_rows(html_text)
    if not tables: return None
    eps,meta=_eps_from_lxml_rows(tables,quarter)
    amounts=_income_amounts_from_lxml_rows(tables,quarter)
    if eps is None and not any(value is not None for value in amounts.values()): return None
    return {
        "source":"MOPS company IFRS report","market":"MOPS","endpoint":url,"official":True,
        "period":f"{year} Q{quarter}","fiscal_year":year,"fiscal_quarter":quarter,
        "statement_date":None,"feed_kind":f"mops_company_{report_id}","company_code":ticker,
        "ytd_eps":eps,**amounts,
        "completeness":sum(value is not None for value in (eps,*amounts.values())),
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

    # Layer 0: company-specific MOPS IFRS report for the expected quarter.
    # Aggregate OpenAPI feeds can be incomplete/staggered; direct company report is authoritative.
    try:
        ey,eq,_=expected_latest_financial_period(date.today())
        found.extend(await fetch_mops_company_ifrs(ticker,ey,eq))
    except Exception as e:
        errors.append(f"mops_company:{type(e).__name__}")

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
            # One canonical parser for TWSE, TPEx and CSV rows. Keeping a second parser here
            # previously caused TPEx's English Year/Date fields to lose the fiscal year.
            return _official_row_to_snapshot(row,source,path,market,kind)
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


FINANCIAL_AMOUNT_FIELDS=("revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd")


def _margin_block(snapshot: dict[str, Any], period: str, basis: str, method: str, note: str) -> dict[str, Any]:
    revenue=safe_num(snapshot.get("revenue_ytd")); gross=safe_num(snapshot.get("gross_profit_ytd"))
    operating=safe_num(snapshot.get("operating_income_ytd")); net=safe_num(snapshot.get("net_income_ytd"))
    return {
        "period":period,"basis":basis,"method":method,"note":note,
        "source":snapshot.get("source"),"endpoint":snapshot.get("endpoint"),
        "revenue":revenue,"gross_profit":gross,"operating_income":operating,"net_income":net,
        "gross_margin":gross/revenue*100 if revenue not in (None,0) and gross is not None else None,
        "operating_margin":operating/revenue*100 if revenue not in (None,0) and operating is not None else None,
        "net_margin":net/revenue*100 if revenue not in (None,0) and net is not None else None,
    }


async def fetch_official_financial_amounts_for_period(ticker: str, year: int, quarter: int) -> dict[str, Any] | None:
    """Resolve exact-period official YTD amounts without accepting EPS-only evidence."""
    candidates=[]
    try: candidates.extend(await fetch_mops_company_ifrs(ticker,year,quarter))
    except Exception: pass
    try:
        ir=await fetch_company_ir_financial(ticker,year,quarter)
        if ir: candidates.append(ir)
    except Exception: pass
    try: candidates.extend(await fetch_mops_material_financial(ticker,year,quarter))
    except Exception: pass
    exact=[row for row in candidates if row.get("official") and row.get("fiscal_year")==year and row.get("fiscal_quarter")==quarter]
    if not exact: return None
    exact.sort(key=lambda row:sum(row.get(field) is not None for field in FINANCIAL_AMOUNT_FIELDS),reverse=True)
    merged=dict(exact[0])
    for row in exact[1:]:
        for field in FINANCIAL_AMOUNT_FIELDS:
            if merged.get(field) is None and row.get(field) is not None: merged[field]=row[field]
    return merged if merged.get("revenue_ytd") is not None else None


async def build_financial_margin_views(ticker: str, current: dict[str, Any], resolve_prior: bool = True) -> dict[str, Any]:
    """Keep cumulative and derived standalone-quarter profitability definitions separate."""
    try: year=int(current.get("fiscal_year")); quarter=int(current.get("fiscal_quarter"))
    except (TypeError,ValueError): year=quarter=None
    if not current.get("official") or not year or not quarter:
        return {"ytd":None,"quarter":None,"warning":"缺少可驗證的官方財報期間，獲利率不進行期間轉換。"}
    if safe_num(current.get("revenue_ytd")) is None:
        same_period=await fetch_official_financial_amounts_for_period(ticker,year,quarter)
        if same_period:
            current={**current,**{field:same_period.get(field) for field in FINANCIAL_AMOUNT_FIELDS if same_period.get(field) is not None}}
    ytd_period=f"{year} Q{quarter} YTD" if quarter<4 else f"{year} FY"
    ytd=_margin_block(current,ytd_period,"cumulative_ytd","official_ytd","官方累計財報數字計算")
    quarter_block=None
    if quarter==1:
        quarter_block={**ytd,"period":f"{year} Q1 單季","basis":"standalone_quarter","method":"q1_equals_ytd","note":"Q1 單季等於 Q1 累計"}
    else:
        if current.get("gross_margin_direct") is not None or current.get("operating_margin_direct") is not None:
            quarter_block={"period":f"{year} Q{quarter} 單季","basis":"standalone_quarter","method":"company_official_direct",
                           "note":"公司官方單季公告比率","source":current.get("source"),"endpoint":current.get("endpoint"),
                           "revenue":None,"gross_profit":None,"operating_income":None,"net_income":None,
                           "gross_margin":safe_num(current.get("gross_margin_direct")),
                           "operating_margin":safe_num(current.get("operating_margin_direct")),"net_margin":None}
        elif resolve_prior:
            prior=await fetch_official_financial_amounts_for_period(ticker,year,quarter-1)
            if prior and safe_num(current.get("revenue_ytd")) is not None and safe_num(prior.get("revenue_ytd")) is not None:
                delta={field:(safe_num(current.get(field))-safe_num(prior.get(field))
                              if safe_num(current.get(field)) is not None and safe_num(prior.get(field)) is not None else None)
                       for field in FINANCIAL_AMOUNT_FIELDS}
                if delta["revenue_ytd"] and delta["revenue_ytd"]>0:
                    quarter_block=_margin_block({**current,**delta},f"{year} Q{quarter} 單季","standalone_quarter",
                                                "official_ytd_difference","依本期累計減前期累計回推，非公司單季公告")
    warning=None if quarter_block else f"缺少 {year} Q{quarter-1} 同口徑官方累計金額；只顯示 {ytd_period}，不推測單季獲利率。"
    return {"ytd":ytd,"quarter":quarter_block,"warning":warning}


async def _warm_official_financial(ticker: str) -> dict[str, Any]:
    job = _OFFICIAL_FINANCIAL_JOBS.setdefault(ticker, {})
    job.update(status="running", last_error=None,
               started_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    try:
        selected = await asyncio.wait_for(fetch_official_income_statement(ticker), timeout=90)
        selected = await asyncio.wait_for(reconcile_official_financial_snapshot(ticker, selected), timeout=45)
        if selected.get("official"):
            _OFFICIAL_FINANCIAL_CACHE[ticker] = (time.time(), dict(selected))
            _CACHE.pop(ticker, None)
            try:
                selected["margin_views"]=await build_financial_margin_views(ticker,selected,resolve_prior=False)
                _OFFICIAL_FINANCIAL_CACHE[ticker] = (time.time(), dict(selected))
                _CACHE.pop(ticker, None)
            except Exception as exc:
                job["margin_views_error"]=type(exc).__name__
            job.update(status="complete", period=selected.get("period"), source=selected.get("source"))
        else:
            job.update(status="partial", last_error="no_official_candidate")
        return selected
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        raise
    except Exception as exc:
        job.update(status="error", last_error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return {"official": False, "errors": [type(exc).__name__]}
    finally:
        job["updated_epoch"] = time.time()
        job["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _OFFICIAL_FINANCIAL_TASKS.pop(ticker, None)


def schedule_official_financial(ticker: str) -> asyncio.Task:
    cached = _OFFICIAL_FINANCIAL_CACHE.get(ticker)
    if cached and time.time() - cached[0] < 21600:
        async def cached_result() -> dict[str, Any]:
            return dict(cached[1])
        return asyncio.create_task(cached_result())
    task = _OFFICIAL_FINANCIAL_TASKS.get(ticker)
    if task and not task.done():
        return task
    prior_job = _OFFICIAL_FINANCIAL_JOBS.get(ticker) or {}
    if (prior_job.get("status") in {"partial", "error"}
            and time.time() - float(prior_job.get("updated_epoch") or 0) < 60):
        async def recent_result() -> dict[str, Any]:
            return {"official": False, "errors": [prior_job.get("last_error") or "official_financial_retry_pending"]}
        return asyncio.create_task(recent_result())
    task = asyncio.create_task(_warm_official_financial(ticker))
    _OFFICIAL_FINANCIAL_TASKS[ticker] = task
    return task

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

def guarded_margin(numerator: Any, revenue: Any, metric: str) -> tuple[float | None, str | None]:
    """Calculate a displayable margin only when units and magnitude are plausible."""
    n=safe_num(numerator); r=safe_num(revenue)
    if n is None or r is None or r <= 0: return None,"missing_or_invalid_amount"
    value=n/r*100; lower=-100.0 if metric=="gross_margin" else -200.0
    if not math.isfinite(value) or value < lower or value > 100.0:
        return None,"unit_or_period_mismatch"
    return value,None

def apply_financial_sanity(financial: dict[str, Any], direct_margins: dict[str, Any] | None=None) -> dict[str, Any]:
    """Reject impossible ratios before they reach cards, scores, or narrative."""
    direct_margins=direct_margins or {}; warnings=[]
    for metric,amount_key in (("gross_margin","gross_profit"),("operating_margin","operating_income"),("net_margin","net_income")):
        direct=safe_num(direct_margins.get(metric)); lower=-100.0 if metric=="gross_margin" else -200.0
        if direct is not None and math.isfinite(direct) and lower <= direct <= 100.0:
            financial[metric]=direct; continue
        value,reason=guarded_margin(financial.get(amount_key),financial.get("revenue"),metric)
        financial[metric]=value
        if reason: warnings.append({"metric":metric,"reason":reason})
    financial["margin_sanity"]="passed" if not warnings else "blocked_invalid_values"
    financial["margin_warnings"]=warnings
    return financial

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
        {"ticker":"3661","year":2026,"quarter":1,"quarter_eps":17.55,"ytd_eps":17.55,"source":"Alchip reviewed financial report","source_url":"https://www.alchip.com/upload/2026_05_11/3_20260511103038sgoopzYTC0.pdf","evidence_kind":"company_official_financial_pdf","verified":True},
        {"ticker":"2454","year":2025,"quarter":3,"quarter_eps":15.84,"source":"MediaTek Quarterly Earnings Release","source_url":"https://www.mediatek.com/hubfs/MediaTek%20Assets/Pdfs/Quarterly%20Earnings%20Release/2025/Quarterly%20Earnings%20Release-2025Q3/Press%20Release.pdf?hsLang=en","evidence_kind":"company_official_quarter_eps","verified":True},
        {"ticker":"2454","year":2025,"quarter":4,"quarter_eps":14.39,"source":"MediaTek Quarterly Earnings Release","source_url":"https://www.mediatek.com/hubfs/MediaTek%20Assets/Pdfs/Quarterly%20Earnings%20Release/2025/Quarterly%20Earnings%20Release-2025Q4/Press%20Release.pdf","evidence_kind":"company_official_quarter_eps","verified":True},
        {"ticker":"2454","year":2026,"quarter":1,"quarter_eps":15.17,"ytd_eps":15.17,"source":"MediaTek Quarterly Earnings Release","source_url":"https://www.mediatek.com/hubfs/MediaTek%20Assets/Pdfs/Quarterly%20Earnings%20Release/2026/Quarterly%20Earnings%20Release-2026Q1/Press%20Release.pdf","evidence_kind":"company_official_quarter_eps","verified":True},
        {"ticker":"2454","year":2026,"quarter":2,"ytd_eps":30.44,"source":"MediaTek Financial Information / MOPS cumulative EPS","source_url":"https://www.mediatek.com/investor-relations/financial-information","evidence_kind":"company_official_ytd_eps","verified":True}
    ]
}

OFFICIAL_EPS_REGISTRY_PATH = Path(__file__).resolve().parent / "data" / "official_eps_registry.json"

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
    try:
        raw=json.loads(OFFICIAL_EPS_REGISTRY_PATH.read_text(encoding="utf-8"))
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
    except Exception as exc:
        merged["file_load_error"]=type(exc).__name__
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
      2) company official IR / reviewed financial report;
      3) official MOPS board-approved disclosure;
      4) return None.  The blocked MOPS historical HTML endpoint is intentionally *not* in the
         production EPS path. Third-party/FinMind values never fill an official predecessor quarter.
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

    # Layer 2: company official IR. TSMC publishes a direct quarter EPS; mapped issuers such as
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

    # Layer 3: official board-approved disclosure, if it contains an exact period and EPS.
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
    official=dict(official or {})
    official_code=str(official.get("company_code") or official.get("ticker") or "").strip()
    identity_mismatch=bool(official_code and official_code != str(ticker))
    if identity_mismatch:
        official={"official":False,"identity_mismatch":True,"rejected_company_code":official_code,
                  "errors":[f"financial_identity_mismatch:{official_code}!={ticker}"]}
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

    # Structured history remains lower-priority. It may fill a display gap only with a provisional label.
    fin_map={(x["year"],x["quarter"]):float(x["ytd_eps"]) for x in fin_hist}

    def standalone(year:int, quarter:int, allow_backup: bool=False):
        key=(year,quarter)
        if key in direct_quarter:
            p=provenance.get(key,{})
            method="official_registry_verified" if p.get("method")=="official_registry_verified" else "official_direct"
            return direct_quarter[key], method, p
        cur=official_ytd.get(key)
        if cur is None and allow_backup:
            cur=fin_map.get(key)
            if cur is not None:
                if quarter==1: return cur,"structured_api_q1",{"source":"FinMind","confidence":55}
                prior=fin_map.get((year,quarter-1))
                if prior is not None: return cur-prior,"structured_api_difference",{"source":"FinMind","confidence":55}
        if cur is None: return None,None,None
        if quarter==1:
            p=provenance.get(key,{})
            return cur,"official_ytd_q1",p
        prev=official_ytd.get((year,quarter-1))
        if prev is None and allow_backup:
            prev=fin_map.get((year,quarter-1))
            if prev is not None:
                return round(cur-prev,4),"hybrid_ytd_difference",{"source":"官方當期 YTD + FinMind 前期 YTD","confidence":65}
        if prev is None: return None,None,None
        p=provenance.get(key,{})
        return round(cur-prev,4),"official_ytd_difference",p

    quarter_eps=None; quarter_method=None; quarter_meta=None
    if fy and fq:
        if official.get("official"):
            # Only exact official exchange/MOPS daily summaries may combine with a clearly
            # marked structured predecessor. Generic or ambiguous sources remain official-only.
            backup_sources={"TWSE/MOPS EPS Daily Summary","TPEx/MOPS EPS Daily Summary"}
            allow_backup=official.get("source") in backup_sources
            quarter_eps,quarter_method,quarter_meta=standalone(fy,fq,allow_backup=allow_backup)
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
            # TTM means four official standalone quarters. Structured/third-party history is
            # display evidence only and must never be blended into an official TTM value.
            val,method,meta=standalone(y,q,allow_backup=False)
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
            single,method,_meta=standalone(ly,lq,allow_backup=False)
            diag=diag_by_period.get(f"{ly} Q{lq}",{})
            provisional_single=single is not None and method in {"hybrid_ytd_difference","structured_api_q1","structured_api_difference"}
            status="provisional" if provisional_single else ("usable" if (direct is not None or cumulative is not None) else "missing")
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
                "source":p.get("source") or (_meta or {}).get("source") or diag.get("source"),
                "source_url":p.get("endpoint") or diag.get("endpoint"),
                "confidence":p.get("confidence") or (_meta or {}).get("confidence") or diag.get("confidence"),
                "lookup_status":diag.get("status") or ("current" if key==(fy,fq) else "unknown"),
                "missing_reason":None if status in {"usable","provisional"} else (diag.get("error") or diag.get("status") or "no_official_evidence"),
                "registry_verified": bool(p.get("method")=="official_registry_verified"),
                "evidence_url": p.get("endpoint") or diag.get("endpoint")
            })

    q_label={"official_direct":"✅ 公司/官方單季值","official_registry_verified":"✅ 官方 EPS Registry","official_ytd_q1":"✅ 官方 Q1 累計=單季","official_ytd_difference":"◇ 官方累計值差額推導（非公司單季公告）","hybrid_ytd_difference":"△ 官方當期＋備援前期暫估",
             "structured_api_q1":"△ 結構化 API","structured_api_difference":"△ 結構化 API 差額"}.get(quarter_method,"資料不足")
    provisional=quarter_method in {"hybrid_ytd_difference","structured_api_q1","structured_api_difference"}
    derivation_inputs=None
    if fy and fq and quarter_method in {"official_ytd_difference","hybrid_ytd_difference"}:
        prior_key=(fy,fq-1)
        prior_value=official_ytd.get(prior_key)
        prior_meta=provenance.get(prior_key,{})
        if prior_value is None:
            prior_value=fin_map.get(prior_key); prior_meta={"source":"FinMind","confidence":55}
        derivation_inputs={"current":{"value":official_ytd.get((fy,fq)),"source":official.get("source"),"official":True},
                           "prior":{"value":prior_value,"source":prior_meta.get("source"),"official":prior_key in official_ytd}}
    return {
        "quarter_eps":quarter_eps,"quarter_period":latest_period,"quarter_method":quarter_method,"quarter_method_label":q_label,
        "quarter_status":"provisional" if provisional else ("official_derived" if quarter_method in {"official_ytd_q1","official_ytd_difference"} else ("official" if quarter_eps is not None else "missing")),
        "quarter_badge":"△" if provisional else ("◇" if quarter_method in {"official_ytd_q1","official_ytd_difference"} else ("✅" if quarter_eps is not None else "—")),
        "quarter_source":(quarter_meta or {}).get("source") if quarter_meta else source,
        "quarter_source_url":(quarter_meta or {}).get("endpoint") if quarter_meta else None,
        "quarter_derivation_inputs":derivation_inputs,
        "ytd_eps":ytd,"ytd_period":f"{fy} Q{fq} YTD" if fy and fq else latest_period,
        "ytd_method_label":"✅ 官方累計值" if official.get("official") and ytd is not None else ("△ 結構化 API" if ytd is not None else "資料不足"),
        "ttm_eps":ttm,"ttm_period":latest_period,"ttm_method_label":("✅ 四個官方單季加總" if ttm is not None else "四季官方資料不足"),
        "ttm_status":"official" if ttm is not None else "missing",
        "replacement_policy":"每次查詢優先重取官方同期間資料；取得後自動覆蓋備援暫估。",
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
            "provisional_periods":[x.get("period") for x in evidence_ledger[1:] if x.get("status")=="provisional"],
            "missing_periods":[x.get("period") for x in evidence_ledger[1:] if x.get("status")=="missing"],
            "policy":"official sources first; marked structured backup may fill display gaps and is replaced by a later official fetch",
        },
        "evidence_ledger_version":"5.4.4",
        "blocked_mops_html_removed":True,
        "identity_verified":not identity_mismatch,
        "rejected_company_code":official_code if identity_mismatch else None,
        "note":"V5.17.4：全股票共用官方 EPS/TTM 規則；官方累計利潤率先顯示，單季回推不得阻塞背景財報更新。"
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
        r"目標價(?:調升|上調|上看|調降|下調|降至|升至|至|為|看|衝|喊到|[:：])?\s*(?:新台幣|NT\$?|TWD)?\s*([0-9][0-9,]{1,6}(?:\.[0-9]+)?)",
        r"(?:喊|上看|調高到|升至)\s*(?:新台幣|NT\$?|TWD)?\s*([0-9][0-9,]{2,6}(?:\.[0-9]+)?)\s*元",
        r"target price(?: raised| lowered| to| of|[:：])?\s*(?:NT\$?|TWD)?\s*([0-9]{2,5}(?:\.[0-9]+)?)",
    ]
    for pat in patterns:
        m=re.search(pat,text,re.I)
        if m:
            v=safe_num(m.group(1).replace(",",""))
            if v and 5 <= v <= 100000: return v
    return None

def _extract_eps(text: str) -> float | None:
    pats=[r"(?:EPS|每股盈餘)[^0-9]{0,16}([0-9]{1,4}(?:\.[0-9]+)?)", r"([0-9]{1,4}(?:\.[0-9]+)?)\s*元[^。；,，]{0,8}(?:EPS|每股盈餘)"]
    for pat in pats:
        m=re.search(pat,text,re.I)
        if m:
            between=text[m.start():m.end()]
            if "目標價" in between: continue
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

def _research_mentions_company(text: str, ticker: str, company_name: str) -> bool:
    """Reject search-engine near matches before any number reaches valuation."""
    normalized=_normalize_title(text)
    company=_normalize_title(company_name)
    if company and company != ticker and len(company) >= 2 and company in normalized:
        return True
    # A bare code can itself be a quoted target. Require a stock-code context
    # when the official company name is unavailable.
    return bool(re.search(rf"(?:股票|代號|上市|上櫃|\(|（)\s*{re.escape(ticker)}(?:\s*[)）]|\b)",text,re.I))

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


def _event_source_priority(url: str | None, publisher: str | None, ticker: str) -> int:
    u=(url or "").lower()
    p=(publisher or "").lower()
    official_hints=[
        "mops.twse.com.tw","twse.com.tw","tpex.org.tw",
        "tsmc.com","investor.tsmc.com","pr.tsmc.com",
        "alchip.com","mediatek.com","honhai.com",
    ]
    if any(x in u for x in official_hints): return 100
    if any(x in p for x in ["twse","mops","證交所","櫃買","investor relations"]): return 95
    if any(x in u for x in ["moneydj","cnyes","udn","yahoo"]): return 72
    return 60

def _event_quarter_hint(text: str, event_date: str | None) -> str | None:
    t=text or ""
    year=None
    m=re.search(r"(20\d{2})",t)
    if m: year=int(m.group(1))
    if year is None and event_date:
        m=re.match(r"(20\d{2})",event_date)
        if m: year=int(m.group(1))
    qm=re.search(r"(?:Q|第\s*)([1-4])(?:季|季度)?",t,re.I)
    if qm and year:
        return f"{year} Q{qm.group(1)}"
    if year:
        try:
            month=int((event_date or "")[5:7])
            q=4 if month<=2 else (1 if month<=5 else (2 if month<=8 else 3))
            return f"{year if q!=4 else year-1} Q{q}"
        except Exception:
            return None
    return None

def _classify_call_bullets(title: str, snippet: str) -> dict[str,list[str]]:
    parts=[]
    for raw in re.split(r"[。；;｜|]|(?:\s+-\s+)",f"{title}。{snippet}"):
        p=" ".join(raw.split()).strip(" -•")
        if 9 <= len(p) <= 180 and p not in parts:
            parts.append(p)
    cats={"financial":[],"operations":[],"outlook":[],"risk":[]}
    keywords={
        "financial":["營收","毛利","營益","獲利","eps","財報","淨利","匯率","margin","revenue","profit"],
        "operations":["接單","訂單","客戶","產能","產品","先進製程","ai","伺服器","asic","出貨","需求","capex","資本支出"],
        "outlook":["展望","預期","預估","看好","看旺","成長","上修","guidance","outlook","目標"],
        "risk":["風險","下修","保守","庫存","遞延","延後","砍單","競爭","匯損","關稅","地緣","uncertainty"],
    }
    for p in parts:
        low=p.lower()
        matched=False
        for cat,words in keywords.items():
            if any(w.lower() in low for w in words):
                cats[cat].append(p[:140]); matched=True
        if not matched and len(cats["operations"])<2:
            cats["operations"].append(p[:140])
    for k in cats:
        seen=[]; out=[]
        for x in cats[k]:
            key=re.sub(r"\s+","",x)
            if key in seen: continue
            seen.append(key); out.append(x)
            if len(out)>=3: break
        cats[k]=out
    return cats

def _call_group_key(row: dict[str,Any]) -> str:
    return str(row.get("quarter_hint") or (row.get("date") or "")[:10] or _normalize_title(row.get("title") or "")[:60])

def _company_event_identity(ticker: str, company_name: str, title: str) -> tuple[bool,str]:
    codes=set(re.findall(r"(?<!\d)(\d{4})(?!\d)",title or ""))
    if any(code != ticker for code in codes):
        return False,"different_ticker_in_title"
    if ticker in codes:
        return True,"ticker_in_title"
    company=_normalize_title(company_name)
    if company and company != ticker and company in _normalize_title(title):
        return True,"company_name_in_title"
    return False,"target_identity_missing"

async def fetch_official_material_info(ticker: str) -> tuple[list[dict[str,Any]],list[str]]:
    """Extension point for official material disclosures; empty is safer than cross-ticker news."""
    return [],[]

async def fetch_company_events(ticker: str, company_name: str) -> dict[str, Any]:
    queries=[
        f'"{company_name}" {ticker} site:twse.com.tw 法人說明會',
        f'"{company_name}" {ticker} site:mops.twse.com.tw 法人說明會',
        f'"{company_name}" {ticker} 法說 法人說明會 展望 財報',
        f'"{company_name}" {ticker} earnings call investor conference',
        f'"{company_name}" {ticker} 重大訊息 公告',
        f'"{company_name}" {ticker} 董事會 重大訊息',
        f'"{company_name}" {ticker} 接單 產能 客戶 訂單',
    ]
    results=await asyncio.gather(*(google_news_rss(q) for q in queries),return_exceptions=True)
    official_rows,official_errors=await fetch_official_material_info(ticker)
    rows=[]; seen=set(); errors=[]
    for result in results:
        if isinstance(result,Exception):
            errors.append(type(result).__name__); continue
        for x in result:
            title=x.get("title") or ""; snippet=x.get("snippet") or ""
            identity_ok,identity_reason=_company_event_identity(ticker,company_name,title)
            if not identity_ok: continue
            key=_normalize_title(title)[:100]
            if not key or key in seen: continue
            seen.add(key)
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

            is_call="法說" in tags
            is_material=("重大訊息" in tags) or any(w in text for w in ["董事會決議","處分","取得","增資","減資","發行","訴訟","合併","股利","除權息"])
            event_type="earnings_call" if is_call else ("material_info" if is_material else "company_update")
            source_priority=_event_source_priority(x.get("url"),x.get("publisher"),ticker)
            classified=_classify_call_bullets(title,snippet)
            summary_bullets=[]
            for cat in ["financial","operations","outlook","risk"]:
                for b in classified[cat]:
                    if b not in summary_bullets: summary_bullets.append(b)
                    if len(summary_bullets)>=5: break
                if len(summary_bullets)>=5: break
            quarter_hint=_event_quarter_hint(text,x.get("published_date"))
            info_density=sum(len(v) for v in classified.values())+len(tags)
            rows.append({
                "ticker":ticker,"identity_verified":True,"identity_reason":identity_reason,
                "date":x.get("published_date"),"title":title,"summary":snippet[:320],
                "summary_bullets":summary_bullets[:5],
                "financial_highlights":classified["financial"],
                "operating_highlights":classified["operations"],
                "management_outlook":classified["outlook"],
                "risk_highlights":classified["risk"],
                "publisher":x.get("publisher"),"source_url":x.get("url"),"tags":tags[:4],
                "event_type":event_type,"quarter_hint":quarter_hint,
                "source_priority":source_priority,"official_source":source_priority>=95,
                "information_density":info_density,
                "summary_method":"official_first_public_summary",
                "copyright_note":"僅整理公開標題與摘要；完整數字與原文以官方來源為準。"
            })

    for row in official_rows:
        rows.append({**row,"ticker":ticker,"identity_verified":True,"identity_reason":"official_exact_ticker","official_source":True})
    errors.extend(official_errors)
    rows=sorted(rows,key=lambda z:(z.get("date") or "",z.get("source_priority") or 0,z.get("information_density") or 0),reverse=True)[:36]
    grouped={}
    for row in [r for r in rows if r.get("event_type")=="earnings_call"]:
        g=_call_group_key(row)
        score=(row.get("source_priority") or 0)*10+(row.get("information_density") or 0)
        if g not in grouped or score>grouped[g][0]:
            grouped[g]=(score,row)
    calls=[v[1] for v in grouped.values()]
    calls=sorted(calls,key=lambda z:(z.get("date") or "",z.get("source_priority") or 0),reverse=True)[:3]

    material=[r for r in rows if r.get("event_type")=="material_info"]
    material=sorted(material,key=lambda z:(z.get("date") or "",z.get("source_priority") or 0),reverse=True)[:15]
    updates=[r for r in rows if r.get("event_type")=="company_update"][:8]

    return {
        "rows":rows,"earnings_calls":calls,"material_info":material,"company_updates":updates,
        "earnings_call_count":len(calls),"material_info_count":len(material),
        "official_call_count":sum(1 for r in calls if r.get("official_source")),
        "errors":errors,"queries":queries,
        "fetched_at":datetime.now().astimezone().isoformat(timespec="seconds"),
        "policy":"官方/交易所來源優先；最近三次法說依季度/日期去重。內容拆分財務、營運、展望、風險四類。"
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
            key=_normalize_title(x['title'])[:90]
            if not key or key in seen: continue
            seen.add(key)
            text=f"{x['title']} {x['snippet']}"
            if not _research_mentions_company(text,ticker,company_name): continue
            inst=_extract_institution(text); target=_extract_target(text); rating=_extract_rating(text); eps=_extract_eps(text); eps_year=_extract_eps_year(text)
            if not any([inst,target,rating,eps]): continue
            score=35 + (25 if inst else 0) + (25 if target else 0) + (10 if rating else 0) + (5 if eps else 0)
            rows.append({
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
    trusted_targets=[]; market_mentions=[]; target_seen=set()
    for x in rows:
        tp=safe_num(x.get('target_price')); inst=x.get('institution')
        trusted=bool(tp is not None and (x.get('source_type')=='manual_import' or (inst and inst!='未辨識機構' and x.get('confidence',0)>=70)))
        key=(inst,x.get('report_date'),tp)
        if trusted:
            if key in target_seen: continue
            trusted_targets.append(tp); target_seen.add(key)
        elif tp is not None:
            market_mentions.append(tp)
    # Annual EPS consensus must have an explicit forecast year. Mixing quarterly/TTM/annual EPS is prohibited.
    eps_by_year: dict[int,list[float]]={}
    for x in rows:
        ep=safe_num(x.get('forward_eps')); ey=x.get('eps_year')
        if ep is not None and isinstance(ey,int) and x.get('confidence',0)>=60 and x.get('institution') not in (None,'未辨識機構'):
            eps_by_year.setdefault(ey,[]).append(ep)
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
        if inst and inst!='未辨識機構':
            if tp is not None: by_inst.setdefault(inst,[]).append((x.get('report_date',''),tp))
            if ep is not None: by_inst_eps.setdefault(inst,[]).append((x.get('report_date',''),ep))
    for vals in by_inst.values():
        vals=sorted(vals,reverse=True)
        if len(vals)>=2 and vals[1][1]: revisions.append((vals[0][1]/vals[1][1]-1)*100)
    for vals in by_inst_eps.values():
        vals=sorted(vals,reverse=True)
        if len(vals)>=2 and vals[1][1]: eps_revisions.append((vals[0][1]/vals[1][1]-1)*100)
    return {
        "count":len(rows), "institution_count":len(institutions), "median_target":median(trusted_targets) if trusted_targets else None,
        "average_target":sum(trusted_targets)/len(trusted_targets) if trusted_targets else None, "high_target":max(trusted_targets) if trusted_targets else None, "low_target":min(trusted_targets) if trusted_targets else None,
        "target_coverage":len(trusted_targets), "market_mention_count":len(market_mentions),
        "median_forward_eps":median(epss) if epss else None, "forward_eps_year":chosen_year, "forward_eps_by_year":eps_consensus, "eps_coverage":len(epss), "target_revision_pct":median(revisions) if revisions else None, "eps_revision_pct":median(eps_revisions) if eps_revisions else None,
        "ratings":ratings, "reports":rows, "public_web_count":sum(1 for x in rows if x.get('source_type')=='public_web_quote'),
        "manual_count":sum(1 for x in rows if x.get('source_type')=='manual_import'),
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


def calc_technical(prices: list[dict[str, Any]], view_limit: int | None = 252,
                   chart_period: str = "近一年日K") -> dict[str, Any]:
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

    # Every row carries OHLC + MA + oscillators. The main report keeps one trading year;
    # the independent weekly/monthly endpoint requests the full aggregated range.
    view=df if view_limit is None else df.tail(view_limit)
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
        "series":series,"series_days":len(series),"chart_period":chart_period,
        "high_52w":float(close.tail(252).max()),"low_52w":float(close.tail(252).min()),
    }


def calc_flow(rows: list[dict[str, Any]], margin_rows: list[dict[str, Any]], lending: dict[str, Any] | None = None,
              price_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    df=pd.DataFrame(rows)
    result: dict[str,Any]={}
    if not df.empty:
        for c in df.columns:
            if c not in ("date","stock_id"):
                df[c]=pd.to_numeric(df[c],errors="coerce").fillna(0)
        df=df.sort_values("date")
        def net(prefix:str,n:int)->float | None:
            buy=[c for c in df.columns if c.startswith(prefix) and c.endswith("_buy")]
            sell=[c for c in df.columns if c.startswith(prefix) and c.endswith("_sell")]
            if len(df) < n or not (buy or sell):
                return None
            tail=df.tail(n)
            return float(tail[buy].sum().sum()-tail[sell].sum().sum())
        values = {
            "foreign_1":net("Foreign_",1),"foreign_5":net("Foreign_",5),"foreign_20":net("Foreign_",20),
            "trust_1":net("Investment_Trust",1),"trust_5":net("Investment_Trust",5),"trust_20":net("Investment_Trust",20),
            "dealer_1":net("Dealer",1),"dealer_5":net("Dealer",5),"dealer_20":net("Dealer",20),
        }
        result.update(values)
        if any(value is not None for value in values.values()):
            result["last_date"] = str(df.iloc[-1]["date"])
        # Per-stock T86/FinMind institutional feeds publish shares, not traded
        # consideration. Estimate the amount with each session's close and
        # expose the method so the UI never calls it an official amount.
        price_by_date: dict[str, float] = {}
        for row in price_rows or []:
            day = str(row.get("date") or "")[:10]
            close = safe_num(row.get("close") if row.get("close") is not None else row.get("Close"))
            if day and close is not None:
                price_by_date[day] = close

        def estimated_amount(prefix: str, n: int) -> float | None:
            buy=[c for c in df.columns if c.startswith(prefix) and c.endswith("_buy")]
            sell=[c for c in df.columns if c.startswith(prefix) and c.endswith("_sell")]
            if len(df) < n or not (buy or sell):
                return None
            total = 0.0
            for _, row in df.tail(n).iterrows():
                close = price_by_date.get(str(row.get("date") or "")[:10])
                if close is None:
                    return None
                total += (sum(float(row[c]) for c in buy) - sum(float(row[c]) for c in sell)) * close
            return total

        amounts = {
            "foreign_1_amount":estimated_amount("Foreign_",1),"foreign_5_amount":estimated_amount("Foreign_",5),"foreign_20_amount":estimated_amount("Foreign_",20),
            "trust_1_amount":estimated_amount("Investment_Trust",1),"trust_5_amount":estimated_amount("Investment_Trust",5),"trust_20_amount":estimated_amount("Investment_Trust",20),
            "dealer_1_amount":estimated_amount("Dealer",1),"dealer_5_amount":estimated_amount("Dealer",5),"dealer_20_amount":estimated_amount("Dealer",20),
        }
        result.update(amounts)
        if any(value is not None for value in amounts.values()):
            result.update({"institutional_amount_unit":"TWD","institutional_amount_method":"net_shares_x_daily_close","institutional_amount_label":"估算淨買賣金額"})

    mdf=pd.DataFrame(margin_rows)
    if not mdf.empty and "MarginPurchaseTodayBalance" in mdf.columns:
        mdf["MarginPurchaseTodayBalance"]=pd.to_numeric(mdf["MarginPurchaseTodayBalance"],errors="coerce")
        if "ShortSaleTodayBalance" in mdf.columns:
            mdf["ShortSaleTodayBalance"]=pd.to_numeric(mdf["ShortSaleTodayBalance"],errors="coerce")
        mdf=mdf.sort_values("date").dropna(subset=["MarginPurchaseTodayBalance"])
        if not mdf.empty:
            mdf=mdf.drop_duplicates(subset=["date"],keep="last").tail(60)
            latest=float(mdf.iloc[-1]["MarginPurchaseTodayBalance"])
            def balance_change(n:int, column: str = "MarginPurchaseTodayBalance"):
                if len(mdf)<=n or column not in mdf.columns: return None
                prior=float(mdf.iloc[-1-n][column])
                current=float(mdf.iloc[-1][column])
                if pd.isna(prior) or pd.isna(current): return None
                return ((current/prior)-1)*100 if prior else None
            result.update({
                "margin_balance":latest,
                "margin_1_pct":balance_change(1),"margin_5_pct":balance_change(5),"margin_20_pct":balance_change(20),
                "margin_last_date":str(mdf.iloc[-1]["date"])
            })
            history=[]
            previous_margin=None
            previous_short=None
            for _, row in mdf.iterrows():
                margin_balance=None if pd.isna(row.get("MarginPurchaseTodayBalance")) else float(row.get("MarginPurchaseTodayBalance"))
                raw_short=row.get("ShortSaleTodayBalance")
                short_balance=None if raw_short is None or pd.isna(raw_short) else float(raw_short)
                history.append({
                    "date":str(row.get("date")),
                    "margin_balance":margin_balance,
                    "margin_change":None if previous_margin is None or margin_balance is None else margin_balance-previous_margin,
                    "short_balance":short_balance,
                    "short_change":None if previous_short is None or short_balance is None else short_balance-previous_short,
                })
                if margin_balance is not None: previous_margin=margin_balance
                if short_balance is not None: previous_short=short_balance
            result["margin_history"]=history
            result["margin_history_unit"]="trading_lots"
            if "ShortSaleTodayBalance" in mdf.columns and not pd.isna(mdf.iloc[-1]["ShortSaleTodayBalance"]):
                short = float(mdf.iloc[-1]["ShortSaleTodayBalance"])
                result.update({"short_balance": short, "short_1_pct": balance_change(1, "ShortSaleTodayBalance"),
                               "short_5_pct": balance_change(5, "ShortSaleTodayBalance"),
                               "short_20_pct": balance_change(20, "ShortSaleTodayBalance"),
                               "short_margin_ratio_pct": (short / latest * 100) if latest else None})
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


def calc_per(rows: list[dict[str, Any]], ticker: str | None=None) -> dict[str, Any]:
    df = pd.DataFrame(rows)
    if df.empty:
        return {}
    if ticker and "stock_id" in df.columns:
        df = df[df["stock_id"].astype(str).str.strip() == str(ticker)]
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
    result = {
        "statement_date": str(latest_date), "revenue": revenue, "gross_profit": gross, "operating_income": op, "net_income": net, "eps": eps,
        "source":"FinMind TaiwanStockFinancialStatements","official":False,"provisional":True,"display_badge":"△",
        "replacement_policy":"每次查詢優先重取官方同期間資料；取得後自動覆蓋備援值。",
    }
    return apply_financial_sanity(result)


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
    forward_valid=bool(consensus_eps and consensus_eps>0 and coverage>=2 and research.get("forward_eps_year"))
    if forward_valid:
        anchor_eps=consensus_eps; eps_basis=f"{research.get('forward_eps_year')}E EPS 中位數（{coverage} 筆明確年度可比預估）"; eps_conf=82
        selected_model="forward_consensus"
    elif ttm and ttm>0:
        anchor_eps=ttm; eps_basis=f"TTM EPS {ttm:.2f}（截至 {q or 'latest'}；不使用單季×4）"; eps_conf=78
        selected_model="trailing_ttm"
    elif ytd and ytd>0:
        qm=re.search(r"Q([1-4])",str(q or eps_stack.get("ytd_period") or ""),re.I)
        elapsed=int(qm.group(1)) if qm else None
        if not elapsed: return {"scenarios": [], "eps_basis":"YTD 期間不明，禁止直接當全年 EPS", "confidence":0}
        anchor_eps=ytd/elapsed*4; eps_basis=f"YTD EPS {ytd:.2f} ÷ {elapsed}季 × 4 = {anchor_eps:.2f}（年化暫估，非 TTM／非公司指引）"; eps_conf=35
        selected_model="annualized_ytd_estimate"
    elif perdata.get("per") and perdata["per"]>0 and allow_financial:
        anchor_eps=price/perdata["per"]; eps_basis="由現價 / 市場 PER 反推 TTM EPS（降級模型）"; eps_conf=32
        selected_model="market_implied"
    else: return {"scenarios": [], "eps_basis":"資料不足", "confidence":0}
    if perdata.get("pe_median"):
        bear_pe=max(5.0,perdata["pe_p25"]); base_pe=perdata["pe_median"]; bull_pe=min(150.0,perdata["pe_p75"]); pe_basis=f"近年歷史 PER 分位數（樣本 {perdata.get('sample_count',0)} 日）"; pe_conf=85
    else:
        center=perdata.get("per") if perdata.get("per") and 5<=perdata["per"]<=120 else 20.0; bear_pe,base_pe,bull_pe=max(8.0,center*.8),center,min(150.0,center*1.2); pe_basis="目前 PER ±20%（歷史樣本不足降級模型）"; pe_conf=50
    def scenario_set(eps: float | None) -> list[dict[str,Any]]:
        if not eps or eps<=0: return []
        out=[{"name":"悲觀","eps":eps*.90,"pe":bear_pe},{"name":"基準","eps":eps,"pe":base_pe},{"name":"樂觀","eps":eps*1.10,"pe":bull_pe}]
        for x in out: x["target"]=x["eps"]*x["pe"]; x["upside_pct"]=(x["target"]/price-1)*100
        return out
    trailing_scenarios=scenario_set(ttm)
    forward_indicative=bool(consensus_eps and consensus_eps>0 and coverage>=1 and research.get("forward_eps_year"))
    forward_scenarios=scenario_set(consensus_eps if forward_indicative else None)
    scenarios=scenario_set(anchor_eps)
    implied_pe=price/ttm if ttm and ttm>0 else safe_num(perdata.get("per"))
    hist_median=safe_num(perdata.get("pe_median"))
    pe_gap_ratio=(implied_pe/hist_median) if implied_pe and hist_median else None
    regime_warning=bool(pe_gap_ratio and pe_gap_ratio>=1.6)
    if regime_warning:
        pe_conf=min(pe_conf,45)
    return {"eps_basis":eps_basis,"pe_basis":pe_basis,"confidence":round((eps_conf+pe_conf)/2),
            "anchor_eps":anchor_eps,"selected_model":selected_model,"scenarios":scenarios,
            "trailing_scenarios":trailing_scenarios,"forward_scenarios":forward_scenarios,
            "forward_status":"consensus" if forward_valid else "single_source" if forward_indicative else "unavailable",
            "analyst_consensus":{"median_target":safe_num(research.get("median_target")),"coverage":int(research.get("target_coverage") or 0),"status":"consensus" if int(research.get("target_coverage") or 0)>=2 else "single_source" if int(research.get("target_coverage") or 0)==1 else "unavailable"},
            "historical_model_label":"TTM 歷史均值回歸價","regime_shift_warning":regime_warning,"pe_gap_ratio":pe_gap_ratio,
            "valuation_warning":"目前市場本益比明顯高於歷史區間，歷史均值回歸價不可直接視為短期目標價。" if regime_warning else None,
            "market_implied":{"price":price,"ttm_eps":ttm,"implied_pe":implied_pe,
                              "official_market_pe":safe_num(perdata.get("per")),"official_market_pbr":safe_num(perdata.get("pbr")),
                              "as_of":perdata.get("last_date")}}


def scores(technical: dict[str, Any], revenue: dict[str, Any], flow: dict[str, Any], perdata: dict[str, Any], financial: dict[str, Any], research: dict[str, Any]) -> dict[str, int]:
    fundamental = 50
    if revenue.get("revenue_yoy") is not None: fundamental += max(-20, min(25, revenue["revenue_yoy"] * .5))
    if revenue.get("revenue_3m_yoy") is not None: fundamental += max(-10, min(15, revenue["revenue_3m_yoy"] * .2))
    if financial.get("gross_margin") is not None: fundamental += 5 if financial["gross_margin"] > 30 else 0
    foreign20, trust20 = flow.get("foreign_20"), flow.get("trust_20")
    chip = 50 + (15 if foreign20 is not None and foreign20 > 0 else -15 if foreign20 is not None else 0) + (10 if trust20 is not None and trust20 > 0 else -10 if trust20 is not None else 0)
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
    trusted_coverage=int(research.get("target_coverage") or 0) + int(research.get("eps_coverage") or 0)
    research_bonus = 8 if trusted_coverage >= 4 else 4 if trusted_coverage >= 2 else 1 if trusted_coverage else 0
    overall = round(completeness * .65 + valuation.get("confidence", 0) * .25 + research_bonus)
    return {"data_completeness": completeness, "valuation_confidence": valuation.get("confidence", 0), "research_coverage": trusted_coverage, "raw_research_mentions": research.get("count", 0), "overall": min(100, overall)}


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
                         integrity: dict[str,Any], mcp_snapshot: dict[str,Any] | None=None) -> dict[str,Any]:
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
    for key in ("gross_margin","operating_margin","net_margin"):
        if financial.get(key) is not None:
            definition="standalone_quarter_margin" if financial.get("margin_basis")=="standalone_quarter" else "cumulative_ytd_margin"
            ev.append(make_evidence(key,financial.get(key),category="fundamental",period=financial.get("margin_period") or financial.get("period"),source=financial.get("source") or "financial statement",source_type=st,confidence=fin_conf,unit="%",definition=definition))
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
    # V5.4.1: TWStock MCP is an independent secondary evidence provider.
    # It never silently overwrites official data; it can corroborate or trigger a conflict.
    for row in ((mcp_snapshot or {}).get("records") or []):
        if row.get("value") is None: continue
        metric=row.get("metric")
        # Align definitions to existing primary evidence only when semantics are sufficiently clear.
        definition=row.get("definition") or metric
        period=row.get("period")
        if metric=="close" and not period: period=tech.get("last_date")
        if metric=="monthly_revenue" and not period: period=revenue.get("revenue_period")
        if metric in {"PER","PBR"} and not period: period=perdata.get("last_date")
        ev.append(make_evidence(metric,row.get("value"),category="crosscheck",kind="fact",period=period,
                                source="TWStock MCP",source_type="mcp_aggregator",confidence=int(row.get("confidence") or 76),
                                unit=row.get("unit"),note=f"tool={row.get('tool')} path={row.get('raw_path')}",
                                definition=definition))

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


def _mcp_text_payload(resp_json: Any) -> Any:
    """Decode common MCP tool result shapes into JSON-ish content."""
    if isinstance(resp_json,dict) and "result" in resp_json:
        result=resp_json.get("result")
    else:
        result=resp_json
    if isinstance(result,dict) and isinstance(result.get("content"),list):
        texts=[]
        for item in result["content"]:
            if isinstance(item,dict) and item.get("type")=="text":
                texts.append(str(item.get("text") or ""))
            elif isinstance(item,(str,int,float)):
                texts.append(str(item))
        joined="\n".join(texts).strip()
        if joined:
            try: return json.loads(joined)
            except Exception: return joined
    return result

def _mcp_response_json(response: httpx.Response) -> dict[str,Any]:
    """Decode JSON-RPC carried as JSON or Streamable HTTP SSE."""
    ctype=(response.headers.get("content-type") or "").lower()
    if "text/event-stream" not in ctype:
        payload=response.json()
        if not isinstance(payload,dict): raise ValueError("MCP response is not an object")
        return payload
    messages=[]
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        raw=line[5:].strip()
        if not raw or raw=="[DONE]":
            continue
        try:
            item=json.loads(raw)
        except Exception:
            continue
        if isinstance(item,dict): messages.append(item)
    if not messages: raise ValueError("MCP SSE response has no JSON-RPC message")
    return messages[-1]

def _walk_dict_values(obj: Any, prefix: str="") -> list[tuple[str,Any]]:
    out=[]
    if isinstance(obj,dict):
        for k,v in obj.items():
            path=f"{prefix}.{k}" if prefix else str(k)
            out.append((path,v))
            out.extend(_walk_dict_values(v,path))
    elif isinstance(obj,list):
        for i,v in enumerate(obj[:80]):
            out.extend(_walk_dict_values(v,f"{prefix}[{i}]"))
    return out

def _extract_mcp_metric(obj: Any, aliases: list[str]) -> tuple[float|None,str|None]:
    if isinstance(obj,str):
        for alias in aliases:
            match=re.search(rf"{re.escape(alias)}\s*[:：]\s*(-?[\d,]+(?:\.\d+)?)",obj,re.I)
            if match:
                value=parse_num_text(match.group(1))
                if value is not None: return value,f"text:{alias}"
    pairs=_walk_dict_values(obj)
    for path,v in pairs:
        p=re.sub(r"[^a-z0-9\u4e00-\u9fff]","",path.lower())
        if any(re.sub(r"[^a-z0-9\u4e00-\u9fff]","",a.lower()) in p for a in aliases):
            n=parse_num_text(v)
            if n is not None: return n,path
    return None,None

def _extract_mcp_date(obj: Any) -> str|None:
    if isinstance(obj,str):
        compact=re.search(r"\b(20\d{2})(\d{2})(\d{2})\b",obj)
        if compact: return f"{compact.group(1)}-{compact.group(2)}-{compact.group(3)}"
    for path,v in _walk_dict_values(obj):
        pl=path.lower()
        if any(k in pl for k in ["date","time","日期","交易日","reportdate","period"]):
            sv=str(v or "")
            m=re.search(r"(20\d{2})[-/.]?(\d{1,2})[-/.]?(\d{1,2})",sv)
            if m: return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None

def _tool_score(tool: dict[str,Any], words:list[str]) -> int:
    text=f"{tool.get('name','')} {tool.get('description','')}".lower()
    return sum(3 if w.lower() in str(tool.get("name","")).lower() else 1 for w in words if w.lower() in text)

def _args_for_mcp_tool(tool: dict[str,Any], ticker:str) -> dict[str,Any]:
    schema=tool.get("inputSchema") or tool.get("input_schema") or {}
    props=schema.get("properties") or {}
    required=set(schema.get("required") or [])
    args={}
    ticker_keys=["ticker","stock_id","stockid","symbol","code","stock_code","stockcode","stock_no","stockno","co_id","company_code"]
    for k,meta in props.items():
        lk=k.lower().replace("-","_")
        typ=(meta or {}).get("type")
        if lk in {"stock_nos","stock_ids","symbols","codes"} and typ=="array":
            args[k]=[ticker]
        elif lk in ticker_keys:
            args[k]=ticker
        elif lk in {"date","trade_date","query_date"}:
            args[k]=date.today().strftime("%Y%m%d")
        elif lk in {"market","exchange"}:
            args[k]="TWSE"
        elif lk in {"days","limit","count","n"}:
            args[k]=20
        elif lk in {"period","timeframe","interval"}:
            args[k]="1d"
        elif k in required:
            if typ=="string": args[k]=ticker
            elif typ=="integer": args[k]=20
            elif typ=="number": args[k]=20
            elif typ=="boolean": args[k]=False
    return args

async def fetch_twstock_mcp_snapshot(ticker:str) -> dict[str,Any]:
    """V5.4.1 live secondary provider.
    Discovers MCP tools at runtime, invokes best matching Taiwan-stock tools using their schemas,
    and returns cross-check evidence. Any MCP failure is non-blocking.
    """
    out={"provider":"TWStock MCP compatible adapter","enabled":TWSTOCK_MCP_ENABLED,"url":TWSTOCK_MCP_URL,
         "mode":"secondary_crosscheck","status":"disabled","records":[],"tool_calls":[],"discovered_tools":[]}
    if not TWSTOCK_MCP_ENABLED: return out
    headers={"Accept":"application/json, text/event-stream","Content-Type":"application/json"}
    try:
        async with httpx.AsyncClient(timeout=12,follow_redirects=True,headers=headers) as client:
            init={"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"ai-stock-research-terminal","version":APP_VERSION}}}
            ir=await client.post(TWSTOCK_MCP_URL,json=init)
            out["initialize_status"]=ir.status_code
            if ir.status_code>=400:
                out.update({"status":"error","error":f"initialize HTTP {ir.status_code}"}); return out
            ij=_mcp_response_json(ir)
            if ij.get("error"):
                out.update({"status":"error","error":f"initialize RPC error: {ij['error']}"}); return out

            lr=await client.post(TWSTOCK_MCP_URL,json={"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
            out["tools_list_status"]=lr.status_code
            if lr.status_code>=400:
                out.update({"status":"error","error":f"tools/list HTTP {lr.status_code}","preview":lr.text[:400]}); return out
            lj=_mcp_response_json(lr)
            if lj.get("error"):
                out.update({"status":"error","error":f"tools/list RPC error: {lj['error']}"}); return out
            tools=((lj.get("result") or {}).get("tools") if isinstance(lj,dict) else None) or []
            out["discovered_tools"]=[{"name":t.get("name"),"description":(t.get("description") or "")[:140]} for t in tools[:80]]
            out["tool_count"]=len(tools)

            categories={
                "quote":["quote","price","realtime","real-time","即時","成交價","stock price"],
                "institutional":["institution","investor","foreign","法人","三大法人","buy sell"],
                "margin":["margin","short","融資","融券"],
                "revenue":["revenue","monthly","營收"],
                "valuation":["per","pbr","valuation","本益比","股價淨值比"],
                "financial":["financial","income","eps","財報","損益","基本面"],
            }
            chosen={}
            for cat,words in categories.items():
                ranked=sorted((( _tool_score(t,words),t) for t in tools),key=lambda x:x[0],reverse=True)
                if ranked and ranked[0][0]>0: chosen[cat]=ranked[0][1]

            raw_by_cat={}
            async def call_one(cat,tool,call_id):
                args=_args_for_mcp_tool(tool,ticker)
                payload={"jsonrpc":"2.0","id":call_id,"method":"tools/call","params":{"name":tool.get("name"),"arguments":args}}
                try:
                    rr=await client.post(TWSTOCK_MCP_URL,json=payload)
                    rec={"category":cat,"tool":tool.get("name"),"args":args,"http_status":rr.status_code}
                    if rr.status_code<400:
                        try:
                            decoded=_mcp_text_payload(_mcp_response_json(rr))
                            rec["status"]="ok"; rec["preview"]=str(decoded)[:240]
                            return rec,decoded
                        except Exception as e:
                            rec["status"]="parse_error"; rec["error"]=f"{type(e).__name__}: {e}"
                    else:
                        rec["status"]="http_error"; rec["preview"]=rr.text[:240]
                    return rec,None
                except Exception as e:
                    return {"category":cat,"tool":tool.get("name"),"args":args,"status":"error","error":f"{type(e).__name__}: {e}"},None

            results=await asyncio.gather(*(call_one(cat,tool,10+i) for i,(cat,tool) in enumerate(chosen.items())))
            for (cat,_),(rec,decoded) in zip(chosen.items(),results):
                out["tool_calls"].append(rec)
                if decoded is not None: raw_by_cat[cat]=decoded

        metric_specs=[
            ("close","quote",["close","price","lastprice","成交價","收盤價","成交"],"TWD","market_close"),
            ("foreign_20d","institutional",["foreign20","foreign_net","外資","外陸資"],None,"institutional_net_20d"),
            ("margin_balance","margin",["marginbalance","margin_purchase_balance","融資餘額"],None,"margin_balance"),
            ("monthly_revenue","revenue",["revenue","monthlyrevenue","營業收入-當月營收","營收"],"TWD","monthly_revenue"),
            ("PER","valuation",["per","pe","本益比"],"x","price_earnings_ratio"),
            ("PBR","valuation",["pbr","pb","股價淨值比"],"x","price_book_ratio"),
            ("quarter_eps","financial",["eps","earningspershare","每股盈餘","基本每股盈餘"],"TWD/share","quarter_or_latest_eps"),
        ]
        records=[]
        for metric,cat,aliases,unit,definition in metric_specs:
            obj=raw_by_cat.get(cat)
            if obj is None: continue
            val,path=_extract_mcp_metric(obj,aliases)
            if val is None: continue
            # MOPS monthly-revenue fields are reported in NT$ thousands.
            if metric=="monthly_revenue" and path and "營業收入-當月營收" in path:
                val*=1000
            records.append({"metric":metric,"value":val,"unit":unit,"period":_extract_mcp_date(obj),
                            "source":"TWStock MCP","source_type":"mcp_aggregator","confidence":76,
                            "definition":definition,"raw_path":path,"tool":chosen.get(cat,{}).get("name")})
        out["records"]=records
        ok_calls=sum(1 for x in out["tool_calls"] if x.get("status")=="ok")
        out["status"]="ok" if records else ("degraded" if ok_calls else "error")
        out["successful_calls"]=ok_calls
        out["fetched_at"]=datetime.now().astimezone().isoformat(timespec="seconds")
        return out
    except Exception as e:
        out.update({"status":"error","error":f"{type(e).__name__}: {e}","fetched_at":datetime.now().astimezone().isoformat(timespec="seconds")})
        return out

async def _warm_twstock_mcp(ticker:str) -> None:
    try:
        snap=await asyncio.wait_for(fetch_twstock_mcp_snapshot(ticker),timeout=45)
        snap["optional"]=True
        _MCP_CACHE[ticker]=(time.time(),snap)
        _CACHE.pop(ticker,None)
    except Exception:
        pass
    finally:
        _MCP_TASKS.pop(ticker,None)

def compact_mcp_snapshot(snapshot: dict[str,Any]) -> dict[str,Any]:
    """Keep stock responses small; verbose tool discovery remains in the diagnostics route."""
    keys=("provider","enabled","url","mode","status","records","tool_count","successful_calls",
          "fetched_at","optional","message","error","cache")
    return {key:snapshot.get(key) for key in keys if key in snapshot}

def get_twstock_mcp_snapshot_cached(ticker:str) -> dict[str,Any]:
    cached=_MCP_CACHE.get(ticker)
    if cached and time.time()-cached[0]<600:
        return compact_mcp_snapshot({**cached[1],"cache":{"hit":True,"ttl_seconds":600}})
    task=_MCP_TASKS.get(ticker)
    if task is None or task.done():
        _MCP_TASKS[ticker]=asyncio.create_task(_warm_twstock_mcp(ticker))
    return compact_mcp_snapshot({"provider":"TWStock MCP compatible adapter","enabled":TWSTOCK_MCP_ENABLED,
            "url":TWSTOCK_MCP_URL,"mode":"secondary_crosscheck","status":"pending",
            "optional":True,"records":[],"tool_calls":[],"discovered_tools":[],
            "tool_count":0,"successful_calls":0,"message":"選用二次驗證正在背景準備，不影響官方核心資料。"})

async def probe_twstock_mcp() -> dict[str,Any]:
    # Kept for diagnostics compatibility; live stock requests use fetch_twstock_mcp_snapshot().
    snap=await fetch_twstock_mcp_snapshot("2330")
    return {k:v for k,v in snap.items() if k!="records"}



def build_data_boundary(source_status, integrity, evidence_graph):
    missing=[x for x in source_status if x.get("status")=="missing" and x.get("required",True)]
    stale=[x for x in source_status if x.get("status")=="stale" and x.get("required",True)]
    optional_unavailable=[x for x in source_status if not x.get("required",True) and x.get("status")!="ok"]
    conflicts=evidence_graph.get("conflicts") or []
    material=[]
    if not integrity.get("official_verified"): material.append("最新財報季度尚未通過官方驗證")
    if conflicts: material.append(f"{len(conflicts)} 組同期間/同定義核心數據存在來源衝突")
    if missing: material.append("缺少："+"、".join(x.get("name","資料") for x in missing[:4]))
    if stale and integrity.get("official_verified"): material.append("部分資料源存在時效落差")
    grade="A" if not material else ("B" if len(material)==1 else ("C" if len(material)<=3 else "D"))
    return {"grade":grade,"material_boundaries":material,"missing_count":len(missing),"stale_count":len(stale),
            "optional_unavailable_count":len(optional_unavailable),
            "conflict_count":len(conflicts),"message":"；".join(material) if material else "目前未發現會改變核心結論的重大資料邊界。"}

def build_research_pipeline(ticker,scores_map,narrative_map,evidence_graph,financial_integrity,expectation,valuation,source_status,technical,flow,revenue,research):
    records=evidence_graph.get("records") or []
    counts={k:len([x for x in records if x.get("kind")==k and x.get("status")=="usable"]) for k in ["fact","derived_fact","estimate","model_output"]}
    boundary=build_data_boundary(source_status,financial_integrity,evidence_graph)
    score=scores_map.get("綜合",50)
    stance="偏多 / Positive" if score>=80 else ("中性偏多 / Constructive" if score>=65 else ("中性 / Neutral" if score>=45 else "審慎 / Cautious"))
    conditions=[]
    ma60=(technical.get("ma") or {}).get(60)
    if ma60 is not None: conditions.append({"type":"trend","condition":f"股價維持 MA60 {ma60:.1f} 之上","meaning":"中期趨勢維持"})
    if flow.get("foreign_20") is not None: conditions.append({"type":"positioning","condition":"外資20日籌碼改善或維持買超","meaning":"籌碼確認"})
    if research.get("eps_revision_pct") is not None: conditions.append({"type":"estimate","condition":"Forward EPS 持續上修","meaning":"市場預期改善"})
    if valuation.get("scenarios"):
        base=next((x for x in valuation["scenarios"] if x.get("name")=="基準"),valuation["scenarios"][0])
        conditions.append({"type":"valuation","condition":f"基準合理價約 {base.get('target',0):,.0f} 元","meaning":"估值參考，不代表保證價格"})
    invalidation=["營收年增明顯轉負或連續惡化","Forward EPS 由上修轉為連續下修","跌破 MA60 且法人20日籌碼同步惡化","重大公司事件改變原本獲利假設"]
    return {"ticker":ticker,"scene":"single_stock_deep_dive","stance":stance,"research_score":score,
            "evidence_counts":{"facts":counts["fact"],"derived_facts":counts["derived_fact"],"estimates":counts["estimate"],"model_outputs":counts["model_output"]},
            "data_boundary":boundary,"investment_view":narrative_map.get("thesis"),"expectation_regime":expectation.get("regime"),
            "catalysts":narrative_map.get("catalysts") or [],"risks":narrative_map.get("risks") or [],
            "action_conditions":conditions[:5],"invalidation_conditions":invalidation,
            "workflow":["定義研究問題與時間邊界","多來源取得行情/基本面/籌碼/公司揭露","Evidence 正規化與官方性/期間/新鮮度驗證","只計算可重現 Derived Facts","整合基本面/籌碼面/技術面/預期差/估值","形成結論、成立條件、失效條件與 PDF"],
            "policy":"Evidence first. Missing data is not zero; estimates are not facts; material conflicts are disclosed."}

def narrative(s: dict[str, int], tech: dict[str, Any], revenue: dict[str, Any], flow: dict[str, Any], valuation: dict[str, Any], research: dict[str, Any]) -> dict[str, Any]:
    base = next((x for x in valuation.get("scenarios", []) if x["name"] == "基準"), None)
    facts=[]
    if revenue.get("revenue_yoy") is not None: facts.append(f"最新月營收年增 {revenue['revenue_yoy']:+.1f}%")
    if revenue.get("revenue_3m_yoy") is not None: facts.append(f"近3月營收年增 {revenue['revenue_3m_yoy']:+.1f}%")
    if flow.get("foreign_20_amount") is not None: facts.append(f"外資近20日估算淨買賣金額 {flow['foreign_20_amount']/100_000_000:+,.2f} 億元")
    if tech.get("trend"): facts.append(f"技術趨勢 {tech['trend']}")
    if research.get("eps_revision_pct") is not None: facts.append(f"法人研究 Forward EPS 修正 {research['eps_revision_pct']:+.1f}%")
    if base: facts.append(f"模型基準合理價約 {base['target']:,.0f} 元")
    stance = "偏多" if s["綜合"] >= 75 else "中性偏多" if s["綜合"] >= 58 else "中性" if s["綜合"] >= 42 else "偏弱"
    catalysts=[]; risks=[]
    if revenue.get("revenue_yoy") is not None and revenue["revenue_yoy"] > 15: catalysts.append("營收成長動能高於中性門檻")
    if flow.get("foreign_20") is not None and flow.get("trust_20") is not None and flow["foreign_20"] > 0 and flow["trust_20"] > 0: catalysts.append("外資與投信近20日同向買超")
    if tech.get("trend") == "多頭": catalysts.append("中期均線結構維持多頭")
    if research.get("eps_revision_pct") is not None and research["eps_revision_pct"] > 3: catalysts.append("Forward EPS 共識出現上修")
    pe = valuation.get("scenarios", [{}])[1].get("pe") if len(valuation.get("scenarios", [])) > 1 else None
    if tech.get("rsi14") and tech["rsi14"] > 75: risks.append("RSI 偏高，短線追價風險上升")
    if flow.get("margin_20_pct") and flow["margin_20_pct"] > 10: risks.append("融資餘額快速增加，籌碼波動風險升高")
    if s["估值"] < 45: risks.append("目前估值相對自身歷史區間偏昂貴")
    if revenue.get("revenue_yoy") is not None and revenue["revenue_yoy"] < 0: risks.append("最新月營收仍呈年減")
    return {"stance": stance, "thesis": "；".join(facts)+"。" if facts else "目前公開結構化資料不足，系統不產生強結論。", "catalysts": catalysts[:4] or ["等待下一次營收、財報或法人預估出現明確上修訊號"], "risks": risks[:4] or ["模型假設與市場估值可能快速變動，需持續追蹤資料更新"]}



def normalize_ticker(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().upper()


def require_numeric_taiwan_ticker(value: Any) -> str:
    ticker = normalize_ticker(value)
    if not ticker.isdigit() or len(ticker) not in (4, 5, 6):
        raise ValueError("invalid Taiwan ticker")
    return ticker


def normalize_market_type(value: Any) -> str:
    market = str(value or "").strip()
    lowered = market.lower()
    if lowered in {"tpex", "otc", "上櫃"}:
        return "上櫃"
    if lowered in {"twse", "tse", "上市"}:
        return "上市"
    return market


def merge_market_rows_by_date(base: list[dict[str, Any]], official: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve history while allowing the official row to win on the same date."""
    merged = {str(row.get("date")): row for row in base if row.get("date")}
    merged.update({str(row.get("date")): row for row in official if row.get("date")})
    return [merged[key] for key in sorted(merged)]


def parse_yahoo_chart_payload(payload: dict[str, Any], ticker: str) -> list[dict[str, Any]]:
    chart = payload.get("chart") if isinstance(payload, dict) else None
    result = ((chart or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        return []
    timestamps = result.get("timestamp") or []
    quote = ((((result.get("indicators") or {}).get("quote") or [None])[0]) or {})
    rows: list[dict[str, Any]] = []
    for index, stamp in enumerate(timestamps):
        try:
            values = {key: (quote.get(key) or [])[index] for key in ("open", "high", "low", "close", "volume")}
        except (IndexError, TypeError):
            continue
        close = safe_num(values.get("close"))
        if close is None:
            continue
        rows.append({
            "date": datetime.fromtimestamp(int(stamp), timezone.utc).date().isoformat(),
            "stock_id": ticker,
            "open": safe_num(values.get("open")), "max": safe_num(values.get("high")),
            "min": safe_num(values.get("low")), "close": close,
            "Trading_Volume": safe_num(values.get("volume")),
            "_source": "Yahoo Finance chart API",
        })
    return rows


async def fetch_yahoo_stock_history(ticker: str, market_hint: str | None = None) -> tuple[list[dict[str, Any]], str, list[str]]:
    """Independent yfinance-compatible OHLC fallback; never supplies Taiwan flow data."""
    ticker = require_numeric_taiwan_ticker(ticker)
    market = normalize_market_type(market_hint)
    suffixes = ["TWO", "TW"] if market == "上櫃" else ["TW", "TWO"]
    errors: list[str] = []
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=headers) as client:
        for suffix in suffixes:
            symbol = f"{ticker}.{suffix}"
            for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
                try:
                    response = await client.get(f"https://{host}/v8/finance/chart/{symbol}",
                                                params={"range": "18mo", "interval": "1d", "events": "history"})
                    response.raise_for_status()
                    rows = parse_yahoo_chart_payload(response.json(), ticker)
                    if rows:
                        return rows, f"Yahoo Finance {symbol}", errors
                    errors.append(f"Yahoo {symbol}:no_rows")
                except Exception as exc:
                    errors.append(f"Yahoo {symbol}:{type(exc).__name__}")
    return [], "Yahoo Finance unavailable", errors


async def fetch_yahoo_long_history(ticker: str, market_hint: str | None = None) -> tuple[list[dict[str, Any]], str, list[str]]:
    """Ten-year daily OHLC fallback used only by the independent long-period chart route."""
    ticker = require_numeric_taiwan_ticker(ticker)
    market = normalize_market_type(market_hint)
    suffixes = ["TWO", "TW"] if market == "上櫃" else ["TW", "TWO"]
    errors: list[str] = []
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as client:
        for suffix in suffixes:
            symbol = f"{ticker}.{suffix}"
            for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
                try:
                    response = await client.get(f"https://{host}/v8/finance/chart/{symbol}",
                                                params={"range": "10y", "interval": "1d", "events": "history"})
                    response.raise_for_status()
                    rows = parse_yahoo_chart_payload(response.json(), ticker)
                    if rows:
                        return rows, f"Yahoo Finance {symbol} 10y", errors
                    errors.append(f"Yahoo {symbol}:no_rows")
                except Exception as exc:
                    errors.append(f"Yahoo {symbol}:{type(exc).__name__}")
    return [], "Yahoo Finance unavailable", errors


def aggregate_price_history(rows: list[dict[str, Any]], interval: str) -> list[dict[str, Any]]:
    """Aggregate normalized daily OHLC rows into calendar weeks or months."""
    if interval not in {"week", "month"}:
        raise ValueError("interval must be week or month")
    normalized: list[tuple[date, dict[str, Any]]] = []
    for row in rows:
        try:
            day = date.fromisoformat(str(row.get("date"))[:10])
        except ValueError:
            continue
        if safe_num(row.get("close")) is not None:
            normalized.append((day, row))
    normalized.sort(key=lambda item: item[0])
    groups: dict[str, list[tuple[date, dict[str, Any]]]] = {}
    for day, row in normalized:
        iso = day.isocalendar()
        key = f"{iso.year:04d}-W{iso.week:02d}" if interval == "week" else day.strftime("%Y-%m")
        groups.setdefault(key, []).append((day, row))
    output: list[dict[str, Any]] = []
    for period_rows in groups.values():
        _, first = period_rows[0]
        last_day, last = period_rows[-1]
        highs = [safe_num(row.get("max") if row.get("max") is not None else row.get("high")) for _, row in period_rows]
        lows = [safe_num(row.get("min") if row.get("min") is not None else row.get("low")) for _, row in period_rows]
        volumes = [safe_num(row.get("Trading_Volume") if row.get("Trading_Volume") is not None else row.get("volume")) for _, row in period_rows]
        valid_highs = [value for value in highs if value is not None]
        valid_lows = [value for value in lows if value is not None]
        closes = [safe_num(row.get("close")) for _, row in period_rows]
        output.append({
            "date": last_day.isoformat(), "stock_id": last.get("stock_id"),
            "open": safe_num(first.get("open")) or safe_num(first.get("close")),
            "max": max(valid_highs or closes), "min": min(valid_lows or closes),
            "close": safe_num(last.get("close")),
            "Trading_Volume": sum(value for value in volumes if value is not None),
        })
    return output


def parse_tpex_stock_day_payload(payload: dict[str, Any], ticker: str | None = None) -> list[dict[str, Any]]:
    if str(payload.get("stat") or "").lower() != "ok":
        raise RuntimeError(str(payload.get("stat") or "TPEx response is not ok"))
    table = next((x for x in payload.get("tables") or [] if x.get("data")), None)
    if not table:
        return []
    rows: list[dict[str, Any]] = []
    for raw in table.get("data") or []:
        if len(raw) < 7:
            continue
        dm = re.match(r"(\d{3})/(\d{2})/(\d{2})", str(raw[0]).strip())
        if not dm:
            continue
        def number(value: Any) -> float | None:
            return safe_num(str(value).replace(",", "").replace("--", ""))
        rows.append({
            "date": f"{int(dm.group(1)) + 1911:04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}",
            **({"stock_id": ticker} if ticker else {}),
            "Trading_Volume": (number(raw[1]) or 0) * 1000,
            "open": number(raw[3]), "max": number(raw[4]), "min": number(raw[5]), "close": number(raw[6]),
            **({"_source": "TPEx afterTrading/tradingStock"} if ticker else {}),
        })
    return rows


async def fetch_tpex_stock_day(ticker: str, months: int = 13) -> tuple[list[dict[str, Any]], list[str]]:
    ticker = require_numeric_taiwan_ticker(ticker)
    cursor = date(date.today().year, date.today().month, 1)
    periods: list[date] = []
    for _ in range(months):
        periods.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    errors: list[str] = []
    semaphore = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=18, follow_redirects=True, headers=OFFICIAL_JSON_HEADERS) as client:
        async def one_month(month: date) -> list[dict[str, Any]]:
            async with semaphore:
                try:
                    response = await client.get(
                        "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock",
                        params={"date": month.strftime("%Y/%m/01"), "code": ticker, "response": "json"},
                    )
                    response.raise_for_status()
                    return parse_tpex_stock_day_payload(response.json(), ticker)
                except Exception as exc:
                    errors.append(f"{month:%Y-%m}: {type(exc).__name__}")
                    return []
        batches = await asyncio.gather(*(one_month(month) for month in periods))
    dedup = {row["date"]: row for batch in batches for row in batch}
    return [dedup[key] for key in sorted(dedup)], errors


async def fetch_twse_stock_day(ticker: str, months: int = 13) -> tuple[list[dict[str, Any]], list[str]]:
    rows = await fetch_twse_stock_day_history(ticker, months)
    return rows, ([] if rows else ["TWSE STOCK_DAY:no_rows"])


async def fetch_official_stock_day(ticker: str, market_hint: str | None = None) -> tuple[list[dict[str, Any]], str, list[str]]:
    market = str(market_hint or "")
    if "上櫃" in market or "OTC" in market.upper():
        rows, errors = await fetch_tpex_stock_day(ticker, 13)
        return rows, "TPEx afterTrading/tradingStock", errors
    twse_rows, twse_errors = await fetch_twse_stock_day(ticker, 13)
    if twse_rows:
        return twse_rows, "TWSE STOCK_DAY", twse_errors
    tpex_rows, tpex_errors = await fetch_tpex_stock_day(ticker, 13)
    if tpex_rows:
        return tpex_rows, "TPEx afterTrading/tradingStock", twse_errors + tpex_errors
    return [], "official price unavailable", twse_errors + tpex_errors


async def _official_info_feed(base: str, path: str, ticker: str, market_type: str) -> tuple[dict[str, Any], list[str]]:
    try:
        async with httpx.AsyncClient(timeout=18, follow_redirects=True, headers=OFFICIAL_JSON_HEADERS) as client:
            response = await client.get(f"{base}{path}")
            response.raise_for_status()
            rows = response.json()
        for row in rows if isinstance(rows, list) else []:
            code = normalize_ticker(row.get("SecuritiesCompanyCode") or row.get("公司代號"))
            if code != ticker:
                continue
            return {
                "stock_id": ticker,
                "stock_name": row.get("CompanyAbbreviation") or row.get("CompanyName") or row.get("公司簡稱") or row.get("公司名稱"),
                "industry_category": row.get("SecuritiesIndustryCode") or row.get("產業別"),
                "type": market_type,
            }, []
        return {}, [f"{market_type} company info:no_match"]
    except Exception as exc:
        return {}, [f"{market_type} company info:{type(exc).__name__}"]


async def fetch_twse_stock_info(ticker: str) -> tuple[dict[str, Any], list[str]]:
    return await _official_info_feed("https://openapi.twse.com.tw/v1", "/opendata/t187ap03_L", ticker, "上市")


async def fetch_tpex_stock_info(ticker: str) -> tuple[dict[str, Any], list[str]]:
    return await _official_info_feed(TPEX_OPENAPI, "/mopsfin_t187ap03_O", ticker, "上櫃")


async def fetch_official_stock_info(ticker: str) -> tuple[dict[str, Any], list[str]]:
    ticker = require_numeric_taiwan_ticker(ticker)
    twse, twse_errors = await fetch_twse_stock_info(ticker)
    if twse:
        return twse, twse_errors
    tpex, tpex_errors = await fetch_tpex_stock_info(ticker)
    return tpex, ([] if tpex else twse_errors + tpex_errors)


async def fetch_tpex_latest_supplements(ticker: str) -> dict[str, list[dict[str, Any]]]:
    """Latest official OTC flow rows; history workers may extend them later."""
    endpoints = ("tpex_3insti_daily_trading", "tpex_mainboard_margin_balance")
    async with httpx.AsyncClient(timeout=18, follow_redirects=True, headers=OFFICIAL_JSON_HEADERS) as client:
        async def fetch(endpoint: str) -> list[dict[str, Any]]:
            try:
                response = await client.get(f"{TPEX_OPENAPI}/{endpoint}")
                response.raise_for_status()
                payload = response.json()
                return payload if isinstance(payload, list) else []
            except Exception:
                return []
        institutional_rows, margin_rows = await asyncio.gather(*(fetch(endpoint) for endpoint in endpoints))
    institutional_hit = next((row for row in institutional_rows if normalize_ticker(row.get("SecuritiesCompanyCode")) == ticker), None)
    margin_hit = next((row for row in margin_rows if normalize_ticker(row.get("SecuritiesCompanyCode")) == ticker), None)
    institutional: list[dict[str, Any]] = []
    margin: list[dict[str, Any]] = []
    if institutional_hit:
        institutional.append({
            "date": _roc_date(institutional_hit.get("Date")), "stock_id": ticker,
            "Foreign_Investor_buy": safe_num(institutional_hit.get("ForeignInvestorsIncludeMainlandAreaInvestors-TotalBuy")),
            "Foreign_Investor_sell": safe_num(institutional_hit.get("ForeignInvestorsIncludeMainlandAreaInvestors-TotalSell")),
            "Investment_Trust_buy": safe_num(institutional_hit.get("SecuritiesInvestmentTrustCompanies-TotalBuy")),
            "Investment_Trust_sell": safe_num(institutional_hit.get("SecuritiesInvestmentTrustCompanies-TotalSell")),
            "Dealer_buy": safe_num(institutional_hit.get("Dealers-TotalBuy")),
            "Dealer_sell": safe_num(institutional_hit.get("Dealers-TotalSell") or institutional_hit.get("Dealers -TotalSell")),
            "_company_name": institutional_hit.get("CompanyName"), "_source": "TPEx 3insti official",
        })
    if margin_hit:
        margin.append({
            "date": _roc_date(margin_hit.get("Date")), "stock_id": ticker,
            "MarginPurchaseBuy": safe_num(margin_hit.get("MarginPurchase")),
            "MarginPurchaseSell": safe_num(margin_hit.get("MarginSales")),
            "MarginPurchaseCashRepayment": safe_num(margin_hit.get("CashRedemption")),
            "MarginPurchaseTodayBalance": safe_num(margin_hit.get("MarginPurchaseBalance")),
            "ShortSaleBuy": safe_num(margin_hit.get("ShortSale")),
            "ShortSaleSell": safe_num(margin_hit.get("ShortConvering")),
            "ShortSaleCashRepayment": safe_num(margin_hit.get("StockRedemption")),
            "ShortSaleTodayBalance": safe_num(margin_hit.get("ShortSaleBalance")),
            "_source": "TPEx margin official",
        })
    return {"institutional": institutional, "margin": margin}


async def fetch_twse_stock_day_history(ticker: str, months: int = 13) -> list[dict[str,Any]]:
    """Official TWSE STOCK_DAY fallback for OHLC when FinMind price is unavailable.
    Fetches up to ~13 calendar months and normalizes into the existing price schema.
    """
    today=date.today()
    ym=[]
    y,m=today.year,today.month
    for _ in range(months):
        ym.append((y,m))
        m-=1
        if m==0:
            y-=1; m=12

    async def one_month(year:int, month:int):
        url="https://www.twse.com.tw/exchangeReport/STOCK_DAY"
        params={"response":"json","date":f"{year:04d}{month:02d}01","stockNo":ticker}
        try:
            async with httpx.AsyncClient(timeout=10,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0"}) as client:
                r=await client.get(url,params=params)
                if r.status_code!=200: return []
                j=r.json()
                if str(j.get("stat","")).upper()!="OK": return []
                out=[]
                for row in j.get("data") or []:
                    if len(row)<9: continue
                    # ROC date yyyy/mm/dd
                    dm=re.match(r"(\d{3})/(\d{2})/(\d{2})",str(row[0]))
                    if not dm: continue
                    gy=int(dm.group(1))+1911
                    d=f"{gy:04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
                    def n(v):
                        try: return float(str(v).replace(",",""))
                        except Exception: return None
                    out.append({
                        "date":d,"stock_id":ticker,
                        "Trading_Volume":n(row[1]),
                        "open":n(row[3]),"max":n(row[4]),"min":n(row[5]),"close":n(row[6]),
                        "spread":n(str(row[7]).replace("+","")),
                        "Trading_turnover":n(row[8]),
                        "_source":"TWSE STOCK_DAY"
                    })
                return out
        except Exception:
            return []

    batches=await asyncio.gather(*(one_month(y,m) for y,m in ym))
    rows=[x for batch in batches for x in batch]
    dedup={}
    for x in rows:
        dedup[x["date"]]=x
    return [dedup[k] for k in sorted(dedup)]

async def build_stock(ticker: str, force_refresh: bool = False) -> dict[str, Any]:
    if (not force_refresh and ticker in _CACHE and time.time() - _CACHE[ticker][0] < CACHE_TTL
            and _cached_stock_is_current(ticker, _CACHE[ticker][1])):
        cached = dict(_CACHE[ticker][1]); cached["cache"] = {"hit": True, "ttl_seconds": CACHE_TTL}; return cached
    today = date.today(); errors: list[str] = []
    async def grab(dataset: str, days: int):
        # FinMind supports a bounded anonymous quota.  Render intentionally has
        # no token, but skipping the provider entirely removed the only
        # independent fallback when TPEx rejected the Render egress address.
        # Keep the same request path in token and anonymous modes; failures are
        # still isolated per dataset and never overwrite official rows.
        try: return await finmind(dataset, ticker, today - timedelta(days=days), today)
        except Exception as e: errors.append(f"{dataset}: {type(e).__name__}"); return []
    async def info_grab():
        try:
            infos = await finmind("TaiwanStockInfo", ticker)
            info = next((x for x in infos if str(x.get("stock_id")) == ticker), {})
            if info.get("type"):
                info = {**info, "type": normalize_market_type(info.get("type"))}
            return info
        except Exception as e: errors.append(f"TaiwanStockInfo: {type(e).__name__}"); return {}

    # Start every independent official chain together. On a free single-worker
    # Render instance, serial fallback stages otherwise add up beyond the
    # browser/proxy timeout even when each provider is healthy.
    supplements_task = asyncio.create_task(asyncio.wait_for(fetch_official_market_supplements(ticker, today, history_days=0), timeout=16))
    official_financial_task = schedule_official_financial(ticker)
    async def official_market_context():
        # Company lookup and OHLC discovery do not depend on each other.  The
        # former serial chain doubled the TPEx outage penalty on every new
        # ticker and could hold an otherwise complete FinMind fallback for 55s.
        (official_info, info_errors), (official_prices, provider, price_errors) = await asyncio.gather(
            fetch_official_stock_info(ticker), fetch_official_stock_day(ticker)
        )
        otc_supplements = await fetch_tpex_latest_supplements(ticker) if official_info.get("type") == "上櫃" else {}
        return official_info, official_prices, provider, info_errors + price_errors, otc_supplements
    official_market_task = asyncio.create_task(asyncio.wait_for(official_market_context(), timeout=24))
    info, prices, inst, margin, rev, pers, fin = await asyncio.gather(
        info_grab(),
        grab("TaiwanStockPrice",460),
        grab("TaiwanStockInstitutionalInvestorsBuySellWide",120),
        grab("TaiwanStockMarginPurchaseShortSale",120),
        grab("TaiwanStockMonthRevenue",900),
        grab("TaiwanStockPER",1100),
        grab("TaiwanStockFinancialStatements",1100),
        return_exceptions=False
    )
    price_source="FinMind TaiwanStockPrice"
    otc_supplements: dict[str, list[dict[str, Any]]] = {}
    try:
        official_info, official_prices, official_price_provider, official_market_errors, otc_supplements = await official_market_task
        if official_info:
            info = {**official_info, **{key:value for key,value in info.items() if value not in (None, "", "—")}}
        if official_prices:
            prices = merge_market_rows_by_date(prices, official_prices)
            price_source = official_price_provider if len(official_prices) >= len(prices) else f"{official_price_provider} latest + FinMind history"
        elif not prices:
            yahoo_prices, yahoo_provider, yahoo_errors = await fetch_yahoo_stock_history(ticker, info.get("type"))
            errors.extend(yahoo_errors)
            if yahoo_prices:
                prices = yahoo_prices
                price_source = yahoo_provider
        errors.extend(official_market_errors)
    except Exception as e:
        errors.append(f"OfficialMarketRouting: {type(e).__name__}")
        if not prices:
            yahoo_prices, yahoo_provider, yahoo_errors = await fetch_yahoo_stock_history(ticker, info.get("type"))
            errors.extend(yahoo_errors)
            if yahoo_prices:
                prices = yahoo_prices
                price_source = yahoo_provider
    try:
        supplements = await supplements_task
    except Exception as e:
        errors.append(f"OfficialMarketSupplements: {type(e).__name__}")
        supplements = {"institutional": [], "margin": [], "revenue": [], "valuation": []}
    if otc_supplements.get("institutional"):
        supplements["institutional"] = otc_supplements["institutional"]
    if otc_supplements.get("margin"):
        supplements["margin"] = otc_supplements["margin"]

    cached_history = _OFFICIAL_HISTORY_CACHE.get(ticker)
    history_cache_at_used = cached_history[0] if cached_history and time.time() - cached_history[0] < 3600 else 0
    if cached_history and time.time() - cached_history[0] < 3600:
        history_data = cached_history[1]
        if history_data.get("institutional"):
            supplements["institutional"] = history_data["institutional"]
        if history_data.get("margin"):
            supplements["margin"] = history_data["margin"]
        if history_data.get("revenue"):
            supplements["revenue"] = history_data["revenue"]

    # TWSE is authoritative for market flows and the latest published figures.
    # FinMind history remains useful, but an official row wins on the same date.
    if supplements.get("institutional"):
        inst = merge_market_rows_by_date(inst, supplements["institutional"])
        if not info.get("stock_name"):
            official_name = next((row.get("_company_name") for row in reversed(inst) if row.get("_company_name")), None)
            if official_name:
                info["stock_name"] = official_name
    if supplements.get("margin"):
        margin = merge_market_rows_by_date(margin, supplements["margin"])
    if supplements.get("revenue"):
        rev = merge_market_rows_by_date(rev, supplements["revenue"])
    if supplements.get("valuation"):
        pers = merge_market_rows_by_date(pers, supplements["valuation"])

    if prices:
        _PRICE_HISTORY_CACHE[ticker] = (time.time(), list(prices), price_source)
    else:
        last_good = _PRICE_HISTORY_CACHE.get(ticker)
        if last_good and time.time() - last_good[0] < 21600:
            prices = list(last_good[1])
            price_source = f"{last_good[2]} last-good cache"
            errors.append("Price providers unavailable; preserved last successful history")

    tech=calc_technical(prices); flow=calc_flow(inst,margin,None,prices); revenue=calc_revenue(rev); perdata=calc_per(pers,ticker); financial=calc_financials(fin)
    flow["institutional_history_count"]=len({str(row.get("date")) for row in inst if row.get("date")})
    flow["institutional_history_required"]=20
    flow["margin_history_required"]=21
    flow["history_complete"]=(flow["institutional_history_count"]>=20 and len(flow.get("margin_history") or [])>=21)
    is_otc = info.get("type") == "上櫃"
    institutional_source = (("TPEx 3insti official" if is_otc else "TWSE T86 official history")
                            if supplements.get("institutional") else "FinMind TaiwanStockInstitutionalInvestorsBuySellWide")
    margin_source = (("TPEx margin official" if is_otc else "TWSE MI_MARGN official history")
                     if supplements.get("margin") else "FinMind TaiwanStockMarginPurchaseShortSale")
    flow["margin_history_source"] = margin_source
    institutional_payload: dict[str, Any] = {}

    async def official_financial_bounded():
        cached_financial = _OFFICIAL_FINANCIAL_CACHE.get(ticker)
        if cached_financial and time.time() - cached_financial[0] < 21600:
            return dict(cached_financial[1])
        try:
            return await asyncio.wait_for(asyncio.shield(official_financial_task), timeout=28)
        except Exception as e:
            errors.append(f"OfficialFinancial: {type(e).__name__}")
            cached_after_wait = _OFFICIAL_FINANCIAL_CACHE.get(ticker)
            if cached_after_wait:
                return dict(cached_after_wait[1])
            return {"official":False,"errors":[type(e).__name__]}

    async def institutional_bounded():
        if flow.get("last_date"):
            return {}
        try:
            return await asyncio.wait_for(fetch_twse_t86_latest(ticker, tech.get("last")), timeout=18)
        except Exception as e:
            errors.append(f"TWSET86: {type(e).__name__}")
            return {}

    # Both official providers are independent; waiting for them serially made a
    # free Render cold start exceed the browser/proxy request window.
    official_financial, institutional_payload = await asyncio.gather(
        official_financial_bounded(), institutional_bounded()
    )
    if institutional_payload.get("institutional"):
        flow.update(institutional_payload.get("flow") or {})
        institutional_source = institutional_payload["source"]
        if not info.get("stock_name") and institutional_payload.get("company_name"):
            info["stock_name"] = institutional_payload["company_name"]
    elif not flow.get("last_date"):
        errors.append("TWSET86: ticker unavailable within provider budget")
    # V5.2.8 official mapping + EPS resolver guard: force the newest official MOPS quarter into the main payload.
    if not official_financial.get("mapping_reconciled"):
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
        apply_financial_sanity(financial,{"gross_margin":official_financial.get("gross_margin_direct"),"operating_margin":official_financial.get("operating_margin_direct")})
        margin_views=official_financial.get("margin_views") or {}
        preferred_margins=margin_views.get("quarter") or margin_views.get("ytd") or {}
        if preferred_margins:
            for key in ("gross_margin","operating_margin","net_margin"):
                if preferred_margins.get(key) is not None:
                    financial[key]=preferred_margins[key]
            financial["margin_views"]=margin_views
            financial["margin_period"]=preferred_margins.get("period")
            financial["margin_basis"]=preferred_margins.get("basis")
            financial["margin_warning"]=margin_views.get("warning")
        financial["provisional"]=False; financial["display_badge"]="✅"
        financial["replacement_policy"]="官方同期間資料已覆蓋備援值。"
    if not financial_integrity.get("official_verified"):
        financial["official"]=False
        financial["source"]=(financial.get("source") or "FinMind") + "（未通過官方最新季度驗證）"
        financial["integrity_warning"]=financial_integrity.get("message")
        financial["provisional"]=True; financial["display_badge"]="△"
    company_name=info.get("stock_name") or ticker
    async def _safe_provider(label, coro, fallback):
        try:
            return await coro
        except Exception as e:
            errors.append(_clean_provider_error(label,e))
            fb=dict(fallback)
            fb["error"]=_clean_provider_error(label,e)
            fb["fetched_at"]=datetime.now().astimezone().isoformat(timespec="seconds")
            return fb

    web_research, company_events = await asyncio.gather(
        _safe_provider("PublicWebResearch", asyncio.wait_for(fetch_public_research(ticker, company_name), timeout=10), {"rows":[],"errors":[]}),
        _safe_provider("CompanyEvents", asyncio.wait_for(fetch_company_events(ticker, company_name), timeout=10), {"rows":[],"earnings_calls":[],"material_info":[],"errors":[]})
    )
    mcp_snapshot=get_twstock_mcp_snapshot_cached(ticker)
    # Final price rescue from MCP cross-check when both FinMind and TWSE history are unavailable.
    if not tech:
        mcp_close=next((r for r in (mcp_snapshot.get("records") or []) if r.get("metric")=="close" and r.get("value") is not None),None)
        if mcp_close:
            px=float(mcp_close["value"])
            tech={"last":px,"last_date":mcp_close.get("period") or date.today().isoformat(),"ma":{},
                  "trend":"資料不足","series":[],"series_days":0,"chart_period":"即時價格 fallback",
                  "rsi14":None,"k":None,"d":None,"macd":None,"macd_signal":None,"macd_hist":None,
                  "support1":None,"support2":None,"resistance":None,"volume_ratio_20":None,"return_20d":None,"return_60d":None}
            price_source="TWStock MCP last-price fallback"
            errors.append("Price history unavailable; MCP last-price fallback active")
    research=merge_research(load_research(ticker), web_research.get("rows", []))
    lp=tech.get("last")
    valuation=model_valuation(lp,perdata,eps_stack,research,financial_integrity)
    sc=scores(tech,revenue,flow,perdata,financial,research)
    nar=narrative(sc,tech,revenue,flow,valuation,research)
    expectation=expectation_gap_analysis(research, company_events, perdata, revenue, sc, lp)
    prev=tech.get("series",[])[-2]["close"] if len(tech.get("series",[]))>=2 else None
    change=((lp/prev-1)*100) if lp and prev else None
    evidence_graph=build_evidence_graph(ticker,tech,revenue,flow,perdata,financial,eps_stack,research,company_events,financial_integrity,mcp_snapshot)
    margin_source = (("TPEx margin official" if is_otc else "TWSE MI_MARGN official history")
                     if supplements.get("margin") else "FinMind TaiwanStockMarginPurchaseShortSale")
    revenue_source = "TWSE/MOPS official current + historical monthly revenue" if supplements.get("revenue") else "FinMind TaiwanStockMonthRevenue"
    valuation_source = "TWSE BWIBBU official latest + FinMind history" if supplements.get("valuation") else "FinMind TaiwanStockPER"
    source_status=[
        {"name":"股價","dataset":price_source,"as_of":tech.get("last_date"),"status":"ok" if tech else "missing","scheduled_update":"交易日約 17:30"},
        {"name":"三大法人","dataset":institutional_source,"as_of":flow.get("last_date"),"status":"ok" if flow.get("foreign_20") is not None else ("warming" if flow.get("last_date") else "missing"),"scheduled_update":"交易日約 20:00；需 20 個交易日才完成"},
        {"name":"融資融券","dataset":margin_source,"as_of":flow.get("margin_last_date"),"status":"ok" if len(flow.get("margin_history") or [])>=21 else ("warming" if flow.get("margin_last_date") else "missing"),"scheduled_update":"交易日約 21:00；需 21 個交易日才完成"},
        {"name":"月營收","dataset":revenue_source,"as_of":revenue.get("last_date") or revenue.get("revenue_period"),"status":"ok" if revenue else "missing","scheduled_update":"依公司公告"},
        {"name":"PER/PBR","dataset":valuation_source,"as_of":perdata.get("last_date"),"status":"ok" if perdata else "missing","scheduled_update":"交易日約 18:00"},
        {"name":"財務報表","dataset":financial.get("source") or "FinMind TaiwanStockFinancialStatements","as_of":financial.get("period") or financial.get("statement_date"),"status":"ok" if financial_integrity.get("official_verified") else "stale","scheduled_update":"僅官方最新季度驗證通過才標示 OK"},
        {"name":"財報完整性閘門","dataset":"Official period gate","as_of":financial_integrity.get("official_period") or eps_stack.get("structured_api_period"),"status":"ok" if financial_integrity.get("core_financials_allowed") else "stale","scheduled_update":financial_integrity.get("message")},
        {"name":"公開法人研究","dataset":"Google News RSS + 公開網路引用","as_of":web_research.get("fetched_at"),"status":"ok" if web_research.get("rows") else "missing","scheduled_update":"每次強制刷新重新搜尋"},
        {"name":"公司事件雷達","dataset":"公開新聞/法說/重大訊息引用","as_of":company_events.get("fetched_at"),"status":"ok" if company_events.get("rows") else "missing","scheduled_update":"每次強制刷新重新搜尋"},
    ]
    source_status.append({"name":"TWStock MCP 二次驗證（選用）","dataset":"TWStock MCP live adapter","as_of":mcp_snapshot.get("fetched_at"),
                          "status":"ok" if mcp_snapshot.get("status")=="ok" else "optional","required":False,
                          "scheduled_update":f"工具 {mcp_snapshot.get('tool_count',0)} / 成功呼叫 {mcp_snapshot.get('successful_calls',0)} / Evidence {len(mcp_snapshot.get('records') or [])}"})
    source_status.append({"name":"Evidence Engine","dataset":"Multi-Source Evidence Schema v1","as_of":evidence_graph.get("generated_at"),"status":"ok" if evidence_graph.get("summary",{}).get("usable") else "missing","scheduled_update":f"Fact {evidence_graph.get('summary',{}).get('facts',0)} / Derived {evidence_graph.get('summary',{}).get('derived_facts',0)} / Conflicts {evidence_graph.get('summary',{}).get('conflicts',0)}"})
    conf=calc_confidence(source_status,valuation,research)
    research_pipeline=build_research_pipeline(ticker,sc,nar,evidence_graph,financial_integrity,expectation,valuation,source_status,tech,flow,revenue,research)
    errors=_compact_error_payload(errors)
    data={"ticker":ticker,"name":info.get("stock_name") or ticker,"industry":info.get("industry_category") or "—","market_type":info.get("type") or "—",
          "generated_at":datetime.now().astimezone().isoformat(timespec="seconds"),"price":lp,"change_pct":change,"technical":tech,"revenue":revenue,"flow":flow,"per":perdata,"financial":financial,
          "research":research,"expectation_gap":expectation,"valuation":valuation,"eps_stack":eps_stack,"official_financial":official_financial,"financial_integrity":financial_integrity,"scores":sc,"stance":nar["stance"],"thesis":nar["thesis"],"catalysts":nar["catalysts"],"risks":nar["risks"],"confidence":conf,
          "source_status":source_status,"evidence_graph":evidence_graph,"research_pipeline":research_pipeline,"errors":errors,"cache":{"hit":False,"ttl_seconds":CACHE_TTL},"web_research_meta":web_research,"company_events":company_events,"twstock_mcp":mcp_snapshot,
          "cashflow":{"institutional":institutional_payload.get("institutional") or {},"institutional_source":institutional_source,"last_date":flow.get("last_date")},"version":APP_VERSION,
          "data_policy":"Cross-Validation：所有資料先正規化為 Evidence Record，依官方性、新鮮度、期間與衝突檢查後才進研究模型；Fact、Derived Fact、Estimate 分層；TWStock MCP 僅作第二來源交叉驗證，不靜默覆蓋官方值。",
          "history_cache_at_used":history_cache_at_used,
          "financial_backfill_status":(_OFFICIAL_FINANCIAL_JOBS.get(ticker) or {}).get("status")}
    # A background history job may finish while this slower report is still
    # building. Never let that older snapshot overwrite the newly completed
    # official history for the full cache TTL.
    if _history_revision(ticker) <= history_cache_at_used:
        _CACHE[ticker]=(time.time(),data)
    else:
        data["cache"]["skipped_stale_history_write"] = True
    schedule_official_history(ticker)
    return data


def report_html(d: dict[str, Any]) -> str:
    esc=lambda x: html.escape("—" if x is None else str(x))
    sc=d["scores"]; tech=d["technical"]; rev=d["revenue"]; flow=d["flow"]; per=d["per"]; fin=d["financial"]; research=d["research"]; exp=d.get("expectation_gap",{}); val=d["valuation"]; conf=d["confidence"]; rp=d.get("research_pipeline",{}); db=rp.get("data_boundary",{})
    src="".join(f"<tr><td>{esc(x['name'])}</td><td>{esc(x.get('dataset'))}</td><td>{esc(x.get('as_of'))}</td><td>{esc(x['scheduled_update'])}</td><td>{'OK' if x['status']=='ok' else '缺資料'}</td></tr>" for x in d["source_status"])
    scenarios="".join(f"<tr><td>{x['name']}</td><td>{x['eps']:.2f}</td><td>{x['pe']:.1f}x</td><td><b>{x['target']:,.0f}</b></td><td>{x['upside_pct']:+.1f}%</td></tr>" for x in val.get("scenarios",[])) or "<tr><td colspan='5'>估值資料不足</td></tr>"
    rrows="".join(f"<tr><td>{esc(x.get('institution'))}</td><td>{esc(x.get('report_date'))}</td><td>{esc(x.get('rating'))}</td><td>{nfmt(safe_num(x.get('target_price')),0)}</td><td>{nfmt(safe_num(x.get('forward_eps')),2)}</td></tr>" for x in research.get("reports",[])) or "<tr><td colspan='5'>目前尚未搜尋到可解析的公開法人研究引用。</td></tr>"
    erows="".join(f"<tr><td>{esc(x.get('institution'))}</td><td>{esc(x.get('previous_date'))} → {esc(x.get('latest_date'))}</td><td>{pct(x.get('eps_revision_pct'))}</td><td>{pct(x.get('target_revision_pct'))}</td><td>{nfmt(x.get('latest_target'),0)}</td></tr>" for x in exp.get("institution_revisions",[])) or "<tr><td colspan='5'>同機構前後修正資料不足。</td></tr>"
    cats="".join(f"<li>{esc(x)}</li>" for x in d.get("catalysts",[])); risks="".join(f"<li>{esc(x)}</li>" for x in d.get("risks",[]))
    return f"""<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><style>
    @page{{size:A4;margin:11mm}} body{{font-family:'Noto Sans TC','PingFang TC',sans-serif;color:#13202a;font-size:9.5pt;line-height:1.55}} h1{{font-size:23pt;margin:0}} h2{{font-size:14pt;border-bottom:2px solid #173847;padding-bottom:4px;margin:18px 0 8px}} .muted{{color:#60727c}} .head{{display:flex;justify-content:space-between;border-bottom:3px solid #173847;padding-bottom:9px}} .price{{font-size:23pt;font-weight:800;text-align:right}} .pill{{display:inline-block;border:1px solid #719188;border-radius:20px;padding:2px 8px;margin-right:5px}} .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin:10px 0}} .card{{border:1px solid #d7e0e4;border-radius:7px;padding:8px}} .card b{{font-size:14pt;display:block}} table{{width:100%;border-collapse:collapse;font-size:8.4pt}} th,td{{padding:5px;border-bottom:1px solid #dce4e8;text-align:left}} th{{background:#f2f6f7}} .call{{border-left:4px solid #17866b;background:#f4faf8;padding:9px}} .warn{{border:1px solid #d7b94b;background:#fff9e7;padding:8px;margin-top:10px}} .small{{font-size:8pt}} .page-break{{break-before:page}} .cols{{display:grid;grid-template-columns:1fr 1fr;gap:10px}} ul{{margin:4px 0 0;padding-left:18px}} .badge{{font-weight:700}}
    </style></head><body>
    <div class='head'><div><div class='muted'>AI STOCK RESEARCH TERMINAL V{APP_VERSION} • EVIDENCE-DRIVEN TAIWAN EQUITY RESEARCH</div><h1>{esc(d['name'])} <span class='muted'>{esc(d['ticker'])}</span></h1><div><span class='pill'>{esc(d['industry'])}</span><span class='pill'>{esc(d['stance'])}</span><span class='pill'>可信度 {conf['overall']}/100</span></div></div><div><div class='muted'>最新收盤</div><div class='price'>{nfmt(d['price'],1)}</div><div>{pct(d['change_pct'])}</div></div></div>
    <div class='small muted'>報告產生：{esc(d['generated_at'])} ｜ 資料完整度：{conf['data_completeness']}% ｜ 估值信心：{conf['valuation_confidence']}%</div>
    <h2>1. Executive Summary</h2><div class='call'>{esc(d['thesis'])}</div>
    <div class='grid'><div class='card'>綜合評分<b>{sc['綜合']}/100</b></div><div class='card'>基本面<b>{sc['基本面']}</b></div><div class='card'>籌碼面<b>{sc['籌碼面']}</b></div><div class='card'>技術面<b>{sc['技術面']}</b></div></div>
    <div class='cols'><div><b>主要催化劑</b><ul>{cats}</ul></div><div><b>主要風險</b><ul>{risks}</ul></div></div>
    <h2>2. Expectation Gap & Revision Radar</h2><div class='call'><b>{esc(exp.get('regime'))}</b><br>{esc(exp.get('summary'))}</div><div class='grid'><div class='card'>預期修正分數<b>{exp.get('revision_score','—')}/100</b></div><div class='card'>EPS 修正<b>{pct(research.get('eps_revision_pct'))}</b></div><div class='card'>目標價修正<b>{pct(research.get('target_revision_pct'))}</b></div><div class='card'>估值區域<b style='font-size:10pt'>{esc(exp.get('valuation_zone'))}</b></div></div><table><tr><th>法人</th><th>前次 → 最新</th><th>EPS 修正</th><th>目標價修正</th><th>最新目標</th></tr>{erows}</table><p class='small muted'>{esc(exp.get('methodology'))}</p>
    <h2>3. Research Pipeline & Data Boundary</h2><div class='grid'><div class='card'>Research View<b style='font-size:11pt'>{esc(rp.get('stance'))}</b></div><div class='card'>Boundary Grade<b>{esc(db.get('grade'))}</b></div><div class='card'>Facts<b>{rp.get('evidence_counts',{}).get('facts','—')}</b></div><div class='card'>Estimates<b>{rp.get('evidence_counts',{}).get('estimates','—')}</b></div></div><div class='call'>{esc(rp.get('investment_view'))}</div><p><b>Data Boundary:</b> {esc(db.get('message'))}</p><div class='cols'><div><b>成立條件</b><ul>{''.join(f"<li>{esc(x.get('condition'))}</li>" for x in rp.get('action_conditions',[]))}</ul></div><div><b>失效條件</b><ul>{''.join(f"<li>{esc(x)}</li>" for x in rp.get('invalidation_conditions',[]))}</ul></div></div>
    <h2>4. Fundamentals & EPS Integrity</h2><table><tr><th>最新月營收</th><th>YoY</th><th>單季 EPS</th><th>YTD EPS</th><th>TTM EPS</th><th>財報期間</th></tr><tr><td>{nfmt(rev.get('latest_revenue'),0)}</td><td>{pct(rev.get('revenue_yoy'))}</td><td>{nfmt(d.get('eps_stack',{}).get('quarter_eps'),2)}</td><td>{nfmt(d.get('eps_stack',{}).get('ytd_eps'),2)}</td><td>{nfmt(d.get('eps_stack',{}).get('ttm_eps'),2)}</td><td>{esc(fin.get('period') or fin.get('statement_date'))}</td></tr></table><p class='small muted'>{esc(d.get('eps_stack',{}).get('note'))}</p>
    <h2>5. Positioning & Technicals</h2><table><tr><th>外資20日估算金額</th><th>投信20日估算金額</th><th>融資20日</th><th>趨勢</th><th>RSI14</th><th>量比</th><th>支撐 / 壓力</th></tr><tr><td>{nfmt(flow.get('foreign_20_amount'),0)} 元</td><td>{nfmt(flow.get('trust_20_amount'),0)} 元</td><td>{pct(flow.get('margin_20_pct'))}</td><td>{esc(tech.get('trend'))}</td><td>{nfmt(tech.get('rsi14'),1)}</td><td>{nfmt(tech.get('volume_ratio_20'),2)}x</td><td>{nfmt(tech.get('support1'),1)} / {nfmt(tech.get('resistance'),1)}</td></tr></table><p class='small muted'>法人金額以每日淨買賣股數 × 當日收盤價換算，為估算值，非官方逐筆成交金額。</p>
    <div class='page-break'></div><h2>6. Analyst Research & Revisions</h2><p>匯入報告數：<b>{research.get('count',0)}</b> ｜ Forward EPS 修正：<b>{pct(research.get('eps_revision_pct'))}</b></p><table><tr><th>法人/券商</th><th>日期</th><th>評等</th><th>目標價</th><th>Forward EPS</th></tr>{rrows}</table><p class='small muted'>本區彙整公開網路可取得之研究引用與使用者匯入資料；僅保存標題、摘要、數值、發布者與來源連結，不重製付費研究全文。</p>
    <h2>7. Company Events & Earnings-call Radar</h2><table><tr><th>日期</th><th>事件</th><th>發布者</th></tr>{''.join(f"<tr><td>{esc(x.get('date'))}</td><td>{esc(x.get('title'))}</td><td>{esc(x.get('publisher'))}</td></tr>" for x in d.get('company_events',{{}}).get('rows',[])[:8]) or "<tr><td colspan='3'>目前未搜尋到公司事件引用。</td></tr>"}</table>
    <h2>8. Valuation Framework</h2><p>EPS：{esc(val.get('eps_basis'))}<br>PE：{esc(val.get('pe_basis'))}</p><table><tr><th>情境</th><th>EPS</th><th>合理 PE</th><th>模型合理價</th><th>相對現價</th></tr>{scenarios}</table>
    <p class='small muted'>歷史 PER：P25 {nfmt(per.get('pe_p25'),1)}x / Median {nfmt(per.get('pe_median'),1)}x / P75 {nfmt(per.get('pe_p75'),1)}x；模型合理價與法人目標價分開呈現。</p>
    <h2>9. Data Lineage & Freshness</h2><table><tr><th>資料</th><th>Dataset</th><th>截至</th><th>預定更新</th><th>狀態</th></tr>{src}</table>
    <div class='warn'><b>重要揭露</b><br>本報告為研究與資訊整理工具，不構成個人化投資建議、招攬或收益保證。模型估值對 EPS 與估值倍數高度敏感；請以每列資料截至日與來源為準。</div>
    </body></html>"""


def write_reportlab_fallback_pdf(d: dict[str, Any], out: Path) -> None:
    """Create a dependency-light Traditional Chinese PDF when HTML rendering fails."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font_name="NotoSansTC"
    font_path=ROOT/"assets"/"fonts"/"NotoSansTC-VF.ttf"
    if not font_path.is_file():
        raise FileNotFoundError("bundled Traditional Chinese PDF font is missing")
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name,str(font_path)))
    styles=getSampleStyleSheet()
    title=ParagraphStyle("ChineseTitle",parent=styles["Title"],fontName=font_name,fontSize=20,leading=26,alignment=TA_CENTER,textColor=colors.HexColor("#13202a"))
    heading=ParagraphStyle("ChineseHeading",parent=styles["Heading2"],fontName=font_name,fontSize=13,leading=18,spaceBefore=10,spaceAfter=6,textColor=colors.HexColor("#173847"))
    body=ParagraphStyle("ChineseBody",parent=styles["BodyText"],fontName=font_name,fontSize=9.5,leading=15,textColor=colors.HexColor("#263840"))
    small=ParagraphStyle("ChineseSmall",parent=body,fontSize=8,leading=12,textColor=colors.HexColor("#60727c"))
    doc=SimpleDocTemplate(str(out),pagesize=A4,rightMargin=13*mm,leftMargin=13*mm,topMargin=12*mm,bottomMargin=12*mm,
                          title=f"{d.get('ticker','')} AI Stock Research V{APP_VERSION}",author="AI Stock Research Terminal")
    esc=lambda value: html.escape("—" if value is None else str(value))
    story=[Paragraph(f"{esc(d.get('name'))} {esc(d.get('ticker'))}",title),
           Paragraph(f"AI Stock Research Terminal V{APP_VERSION}｜產生時間 {esc(d.get('generated_at'))}",small),Spacer(1,5*mm)]
    story.extend([Paragraph("投資摘要",heading),Paragraph(esc(d.get("thesis") or "目前資料不足以形成完整摘要。"),body)])
    scores=d.get("scores") or {}; financial=d.get("financial") or {}; revenue=d.get("revenue") or {}; technical=d.get("technical") or {}; valuation=d.get("valuation") or {}
    overview=[
        ["最新收盤","綜合評分","財報期間","TTM EPS"],
        [nfmt(safe_num(d.get("price")),1),esc(scores.get("綜合")),esc(financial.get("period") or financial.get("statement_date")),nfmt(safe_num(financial.get("ttm_eps")),2)],
        ["最新月營收","營收年增","技術趨勢","可信度"],
        [nfmt(safe_num(revenue.get("latest_revenue")),0),pct(safe_num(revenue.get("revenue_yoy"))),esc(technical.get("trend")),esc((d.get("confidence") or {}).get("overall"))],
    ]
    table=Table(overview,colWidths=[43*mm]*4)
    table.setStyle(TableStyle([("FONTNAME",(0,0),(-1,-1),font_name),("FONTSIZE",(0,0),(-1,-1),8.5),("LEADING",(0,0),(-1,-1),12),
                               ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#eaf2f1")),("BACKGROUND",(0,2),(-1,2),colors.HexColor("#eaf2f1")),
                               ("GRID",(0,0),(-1,-1),.4,colors.HexColor("#b7c6cc")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("PADDING",(0,0),(-1,-1),6)]))
    story.extend([Spacer(1,4*mm),table,Paragraph("估值情境",heading)])
    scenarios=valuation.get("scenarios") or []
    value_rows=[["情境","EPS","合理 PE","模型合理價","相對現價"]]+[[esc(x.get("name")),nfmt(safe_num(x.get("eps")),2),f"{nfmt(safe_num(x.get('pe')),1)}x",nfmt(safe_num(x.get("target")),0),pct(safe_num(x.get("upside_pct")))] for x in scenarios]
    if len(value_rows)==1:value_rows.append(["資料不足","—","—","—","—"])
    value_table=Table(value_rows,colWidths=[34*mm]*5,repeatRows=1)
    value_table.setStyle(TableStyle([("FONTNAME",(0,0),(-1,-1),font_name),("FONTSIZE",(0,0),(-1,-1),8),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#173847")),
                                     ("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),.35,colors.HexColor("#c6d1d5")),("PADDING",(0,0),(-1,-1),5)]))
    story.append(value_table)
    for label,items in (("主要催化劑",d.get("catalysts") or []),("主要風險",d.get("risks") or [])):
        story.append(Paragraph(label,heading))
        story.append(Paragraph("<br/>".join(f"• {esc(item)}" for item in items) or "—",body))
    story.extend([Paragraph("資料與揭露",heading),Paragraph("本檔為 HTML PDF 引擎不可用時產生的繁體中文備援報告；核心價格、財報、評分、估值與風險資料取自同一次網頁研究結果。",small),
                  Paragraph("重要聲明：本系統為研究與資訊整理工具，不構成個人化投資建議、招攬或收益保證。",small)])
    doc.build(story)


@app.middleware("http")
async def runtime_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-AI-Stock-Version"] = APP_VERSION
    if request.url.path == "/" or request.url.path.endswith((".js", ".css", ".webmanifest")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/")
async def home(): return FileResponse(ROOT / "index.html", headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0"})
@app.get("/app.js")
async def js(): return FileResponse(ROOT / "app.js", media_type="application/javascript", headers={"Cache-Control":"no-cache"})
@app.get("/styles.css")
async def css(): return FileResponse(ROOT / "styles.css", media_type="text/css", headers={"Cache-Control":"no-cache"})
@app.get("/sw.js")
async def sw(): return FileResponse(ROOT / "sw.js", media_type="application/javascript", headers={"Cache-Control":"no-cache", "Service-Worker-Allowed":"/"})

STATIC_ASSETS={
    "app.js":("app.js","application/javascript"),
    "styles.css":("styles.css","text/css"),
    "manifest.webmanifest":("manifest.webmanifest","application/manifest+json"),
    "icon-192.png":("icon-192.png","image/png"),
    "icon-512.png":("icon-512.png","image/png"),
}

@app.get("/static/{asset_name}")
async def static_asset(asset_name: str):
    asset=STATIC_ASSETS.get(asset_name)
    if not asset:
        raise HTTPException(404,"static asset not found")
    filename,media_type=asset
    return FileResponse(ROOT / filename,media_type=media_type,headers={"Cache-Control":"no-cache"})


@app.get("/api/diagnostics/financial/{ticker}")
async def financial_diagnostics(ticker: str):
    ticker=ticker.strip()
    if not ticker.isdigit() or len(ticker) not in (4,5,6): raise HTTPException(400,"請輸入有效台股代號")
    result=await diagnose_official_financial_sources(ticker)
    return JSONResponse(result, headers={"Cache-Control":"no-store"})

@app.get("/api/stock/{ticker}")
async def stock_api(ticker: str, refresh: bool = Query(False)):
    ticker=ticker.strip()
    if not ticker.isdigit() or len(ticker) not in (4,5,6):
        return JSONResponse({"status":"error","message":"請輸入有效台股代號","ticker":ticker},status_code=400,headers={"Cache-Control":"no-store"})
    try:
        d=await build_stock(ticker,force_refresh=refresh)
    except Exception as e:
        return JSONResponse(
            {"status":"degraded","ticker":ticker,"message":"研究報告暫時無法完整產生","errors":_compact_error_payload([_clean_provider_error("build_stock",e)])},
            status_code=503,headers={"Cache-Control":"no-store"}
        )
    if not isinstance(d,dict):
        return JSONResponse({"status":"degraded","ticker":ticker,"message":"後端回傳格式異常","errors":[]},status_code=503,headers={"Cache-Control":"no-store"})
    d["errors"]=_compact_error_payload(d.get("errors"))
    if d.get("price") is None:
        return JSONResponse(
            {"status":"degraded","ticker":ticker,"name":d.get("name") or ticker,
             "message":"目前無法取得有效價格資料；其他資料來源若可用仍保留診斷結果",
             "errors":d.get("errors",[]),"provider_status":d.get("source_status",[])},
            status_code=503,headers={"Cache-Control":"no-store"}
        )
    return JSONResponse(d,headers={"Cache-Control":"no-store"})

@app.get("/api/history/{ticker}")
async def long_price_history(ticker: str, interval: str = Query("week")):
    ticker = ticker.strip()
    if not ticker.isdigit() or len(ticker) not in (4, 5, 6):
        raise HTTPException(400, "請輸入有效台股代號")
    if interval not in {"week", "month"}:
        raise HTTPException(400, "interval 僅支援 week 或 month")
    key = f"{ticker}:{interval}"
    cached = _LONG_PRICE_HISTORY_CACHE.get(key)
    if cached and time.time() - cached[0] < 21600:
        return JSONResponse(cached[1], headers={"Cache-Control": "public, max-age=300"})
    stock_cache = (_CACHE.get(ticker) or (0, {}))[1]
    market_type = stock_cache.get("market_type") or stock_cache.get("market")
    daily, source, errors = await fetch_yahoo_long_history(ticker, market_type)
    recent = _PRICE_HISTORY_CACHE.get(ticker)
    if recent and recent[1]:
        daily = merge_market_rows_by_date(daily, recent[1])
        source = f"{source}；近期資料以 {recent[2]} 覆蓋"
    if not daily:
        raise HTTPException(503, "近十年價格資料目前無法取得")
    aggregated = aggregate_price_history(daily, interval)
    label = "週線" if interval == "week" else "月線"
    technical = calc_technical(aggregated, view_limit=None, chart_period=f"近10年{label}")
    payload = {
        "ticker": ticker, "interval": interval, "label": label, "years": 10,
        "series": technical.get("series") or [],
        "technical": {key:value for key,value in technical.items() if key != "series"}, "source": source,
        "as_of": technical.get("last_date"), "version": APP_VERSION,
        "fallback": "Yahoo Finance" in source, "errors": _compact_error_payload(errors),
    }
    _LONG_PRICE_HISTORY_CACHE[key] = (time.time(), payload)
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=300"})


@app.get("/api/diagnostics/history/{ticker}")
async def history_diagnostics(ticker: str):
    ticker=ticker.strip()
    if not ticker.isdigit() or len(ticker) not in (4,5,6):
        raise HTTPException(400,"請輸入有效台股代號")
    cached = (_OFFICIAL_HISTORY_CACHE.get(ticker) or (0, {}))[1]
    return JSONResponse({
        "ticker": ticker, "version": APP_VERSION,
        "counts": {key: len(cached.get(key) or []) for key in ("institutional", "margin", "revenue")},
        "jobs": _OFFICIAL_HISTORY_JOBS.get(ticker, {}),
        "financial_job": _OFFICIAL_FINANCIAL_JOBS.get(ticker, {}),
        "financial_cached": ticker in _OFFICIAL_FINANCIAL_CACHE,
        "active": sorted(key.split(":",1)[1] for key, task in _OFFICIAL_HISTORY_TASKS.items()
                         if key.startswith(f"{ticker}:") and not task.done()),
    }, headers={"Cache-Control":"no-store"})

@app.get("/api/stock/{ticker}/pdf")
async def stock_pdf(ticker: str, refresh: bool = Query(False)):
    global _WEASYPRINT_DISABLED
    ticker=ticker.strip()
    if not ticker.isdigit() or len(ticker) not in (4,5,6):
        raise HTTPException(400,"請輸入有效台股代號")
    def data_revision() -> tuple[float,float,float]:
        return (_history_revision(ticker),
                float((_OFFICIAL_FINANCIAL_JOBS.get(ticker) or {}).get("updated_epoch") or 0),
                float((_MCP_CACHE.get(ticker) or (0,{}))[0] or 0))
    def cached_response() -> FileResponse | None:
        entry=_PDF_REPORT_CACHE.get(ticker)
        if not entry or refresh or time.time()-float(entry.get("created_epoch") or 0)>PDF_CACHE_TTL:
            return None
        path=Path(entry.get("path") or "")
        if not path.is_file() or tuple(entry.get("data_revision") or ())!=data_revision():
            return None
        filename=str(entry.get("filename") or path.name)
        return FileResponse(path,media_type="application/pdf",headers={"Content-Disposition":f'inline; filename="{filename}"',
                            "X-PDF-Renderer":str(entry.get("renderer") or "reportlab"),"X-PDF-Cache":"HIT","Cache-Control":"no-store"})
    hit=cached_response()
    if hit:return hit
    lock=_PDF_REPORT_LOCKS.setdefault(ticker,asyncio.Lock())
    async with lock:
        hit=cached_response()
        if hit:return hit
        try:
            d=await build_stock(ticker,force_refresh=refresh)
        except Exception as exc:
            raise HTTPException(503,"研究資料暫時無法準備完成，請稍後再產生 PDF。") from exc
        if d.get("price") is None: raise HTTPException(503,"目前無法取得股價資料，為避免輸出錯誤報告，PDF 未產生。")
        stamp=datetime.now().strftime("%Y%m%d_%H%M")
        version_slug=APP_VERSION.replace(".","_")
        out=REPORT_DIR/f"{ticker}_{stamp}_research_v{version_slug}.pdf"
        renderer="reportlab"
        if PDF_RENDERER=="weasyprint" and not _WEASYPRINT_DISABLED:
            try:
                from weasyprint import HTML
                HTML(string=report_html(d),base_url=str(ROOT)).write_pdf(out)
                renderer="weasyprint"
            except Exception:
                _WEASYPRINT_DISABLED=True
        if renderer!="weasyprint":
            try:
                write_reportlab_fallback_pdf(d,out)
            except Exception as exc:
                raise HTTPException(503,"PDF 引擎目前無法完成報告，請稍後再試。") from exc
        filename=f"{ticker}_AI_research_V{version_slug}_{stamp}.pdf"
        _PDF_REPORT_CACHE[ticker]={"created_epoch":time.time(),"path":str(out),"filename":filename,
                                   "renderer":renderer,"data_revision":data_revision(),"generated_at":d.get("generated_at")}
        return FileResponse(out,media_type="application/pdf",headers={"Content-Disposition":f'inline; filename="{filename}"',
                            "X-PDF-Renderer":renderer,"X-PDF-Cache":"MISS","Cache-Control":"no-store"})

@app.post("/api/cache/clear")
async def cache_clear(request: Request):
    if not CACHE_ADMIN_TOKEN:
        raise HTTPException(404,"not found")
    supplied=request.headers.get("authorization","")
    expected=f"Bearer {CACHE_ADMIN_TOKEN}"
    if not secrets.compare_digest(supplied,expected):
        raise HTTPException(403,"forbidden")
    _CACHE.clear()
    _OFFICIAL_HISTORY_CACHE.clear()
    _OFFICIAL_FINANCIAL_CACHE.clear()
    _PDF_REPORT_CACHE.clear()
    return {"status":"ok","message":"cache cleared"}

@app.get("/api/diagnostics/eps/{ticker}")
async def eps_diagnostics(ticker: str):
    ticker=ticker.strip().upper()
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
        "ticker":ticker,"version":APP_VERSION,
        "official_current":{k:official.get(k) for k in ("source","endpoint","period","fiscal_year","fiscal_quarter","ytd_eps","quarter_eps_direct","report_id")},
        "finmind_error":finmind_error,
        "eps_stack":stack,
        "raw_period_trace_links":raw_periods,
        "note":"V5.2.13 production EPS no longer depends on blocked MOPS historical HTML; raw endpoint diagnostics are retained only for troubleshooting."
    }

@app.get("/api/diagnostics/eps-raw/{ticker}")
async def eps_raw_diagnostics(ticker: str, year: int = Query(..., ge=1990, le=2100), quarter: int = Query(..., ge=1, le=4)):
    ticker=ticker.strip().upper()
    if not ticker.isdigit(): raise HTTPException(400,"ticker must be numeric")
    return await trace_mops_company_ifrs(ticker,year,quarter)

@app.get("/api/diagnostics/eps-registry/{ticker}")
async def eps_registry_diagnostics(ticker: str):
    ticker=normalize_ticker(ticker)
    reg=_load_official_eps_registry()
    rows=[r for r in reg.get("records",[]) if str(r.get("ticker"))==ticker]
    rows=sorted(rows,key=lambda r:(r.get("year") or 0,r.get("quarter") or 0),reverse=True)
    return {"ticker":ticker,"version":APP_VERSION,"registry_schema_version":reg.get("schema_version"),
            "updated_at":reg.get("updated_at"),"record_count":len(rows),"records":rows}

@app.get("/api/evidence/{ticker}")
async def evidence_api(ticker: str, refresh: int = 0):
    ticker=ticker.strip().upper()
    if not re.fullmatch(r"[0-9A-Z]{2,10}", ticker): raise HTTPException(400,"invalid ticker")
    d=await build_stock(ticker, bool(refresh))
    return d.get("evidence_graph") or {}

@app.get("/api/diagnostics/providers/{ticker}")
async def provider_diagnostics(ticker: str):
    ticker=ticker.strip().upper()
    d=await build_stock(ticker, False)
    mcp=await probe_twstock_mcp()
    return {"version":APP_VERSION,"ticker":ticker,"providers":PROVIDER_REGISTRY,"twstock_mcp":mcp,"source_status":d.get("source_status",[]),"evidence_summary":(d.get("evidence_graph") or {}).get("summary",{}),"conflicts":(d.get("evidence_graph") or {}).get("conflicts",[])}


@app.get("/api/diagnostics/mcp/{ticker}")
async def mcp_diagnostics(ticker: str):
    ticker=ticker.strip().upper()
    if not re.fullmatch(r"[0-9A-Z]{2,10}",ticker): raise HTTPException(400,"invalid ticker")
    try:
        snap=await asyncio.wait_for(fetch_twstock_mcp_snapshot(ticker),timeout=50)
    except asyncio.TimeoutError:
        snap={"provider":"TWStock MCP compatible adapter","status":"timeout","optional":True,
              "records":[],"message":"選用二次驗證逾時，不影響官方核心資料。"}
    return snap


@app.get("/api/diagnostics/provider-health/{ticker}")
async def provider_health(ticker: str):
    ticker=ticker.strip()
    if not ticker.isdigit() or len(ticker) not in (4,5,6):
        return JSONResponse({"status":"error","message":"invalid ticker"},status_code=400)
    result={"ticker":ticker,"version":APP_VERSION,"providers":{},"summary":{"ok":0,"degraded":0,"error":0}}
    checks=[
        ("FinMind 股價", finmind("TaiwanStockPrice",ticker,date.today()-timedelta(days=10),date.today())),
        ("FinMind 法人", finmind("TaiwanStockInstitutionalInvestorsBuySellWide",ticker,date.today()-timedelta(days=30),date.today())),
        ("FinMind 融資融券", finmind("TaiwanStockMarginPurchaseShortSale",ticker,date.today()-timedelta(days=30),date.today())),
        ("FinMind 月營收", finmind("TaiwanStockMonthRevenue",ticker,date.today()-timedelta(days=400),date.today())),
        ("TWSE STOCK_DAY", fetch_twse_stock_day_history(ticker,1)),
        ("Yahoo Finance OHLC", fetch_yahoo_stock_history(ticker)),
        ("TWStock MCP", fetch_twstock_mcp_snapshot(ticker)),
    ]
    async def run(name,coro):
        try:
            v=await coro
            if isinstance(v, tuple):
                n=len(v[0] or [])
            else:
                n=len(v) if isinstance(v,list) else len((v or {}).get("records") or [])
            status="ok" if n>0 else "degraded"
            return name,{"status":status,"records":n}
        except Exception as e:
            return name,{"status":"error","records":0,"error":_compact_error_payload([_clean_provider_error(name,e)])[0]}
    pairs=await asyncio.gather(*(run(n,c) for n,c in checks))
    for k,v in pairs:
        result["providers"][k]=v
        result["summary"][v["status"]]=result["summary"].get(v["status"],0)+1
    return JSONResponse(result,headers={"Cache-Control":"no-store"})

@app.get("/health")
async def health(): return {"status":"ok","version":APP_VERSION,"mode":"multi-source-official-first","finmind_token":bool(FINMIND_TOKEN),"finmind_mode":"token" if FINMIND_TOKEN else "anonymous-fallback","yahoo_price_fallback":True,"twstock_mcp_crosscheck":TWSTOCK_MCP_ENABLED,"cache_ttl_seconds":CACHE_TTL,"pwa":False}
