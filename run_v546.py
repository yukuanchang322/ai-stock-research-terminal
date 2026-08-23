"""V5.4.6 Data Completeness Repair.

Production wrapper around server.py. FinMind remains available, but when selected
market datasets are empty/unavailable we transparently fall back to official TWSE
public endpoints. The existing calculation / Evidence Engine stays authoritative.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

import server

VERSION = "5.4.6"
server.app.version = VERSION
_original_finmind = server.finmind
_original_build_stock = server.build_stock


def _num(v: Any) -> float | None:
    try:
        if v in (None, "", "--", "—", "-"):
            return None
        return float(str(v).replace(",", "").replace("+", "").strip())
    except Exception:
        return None


def _pick(row: dict[str, Any], *needles: str) -> Any:
    for k, v in row.items():
        ks = str(k).replace(" ", "")
        if all(n in ks for n in needles):
            return v
    return None


def _net_pair(net: float | None) -> tuple[float, float]:
    n = float(net or 0)
    return (n, 0.0) if n >= 0 else (0.0, -n)


async def _json(url: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(
        timeout=14,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 AI-Stock-Research/5.4.6"},
    ) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        if "json" not in (r.headers.get("content-type") or "").lower():
            raise RuntimeError("official endpoint returned non-JSON content")
        return r.json()


async def _official_company_info() -> list[dict[str, Any]]:
    rows = await _json("https://openapi.twse.com.tw/v1/opendata/t187ap03_L")
    out = []
    for x in rows if isinstance(rows, list) else []:
        code = str(x.get("公司代號") or x.get("公司代碼") or "").strip()
        if not code:
            continue
        out.append({
            "stock_id": code,
            "stock_name": x.get("公司簡稱") or x.get("公司名稱") or code,
            "industry_category": x.get("產業別") or x.get("產業類別") or "—",
            "type": "上市",
            "_source": "TWSE OpenAPI t187ap03_L",
        })
    return out


async def _official_revenue(ticker: str) -> list[dict[str, Any]]:
    rows = await _json("https://openapi.twse.com.tw/v1/opendata/t187ap05_L")
    hit = next((x for x in rows if str(x.get("公司代號") or "").strip() == ticker), None) if isinstance(rows, list) else None
    if not hit:
        return []
    ym = str(hit.get("資料年月") or hit.get("年月") or "").strip().replace("/", "").replace("-", "")
    digits = "".join(c for c in ym if c.isdigit())
    year = month = None
    if len(digits) >= 5:
        raw_y = int(digits[:-2]); month = int(digits[-2:]); year = raw_y + 1911 if raw_y < 1911 else raw_y
    if not year or not month:
        today = date.today(); year, month = today.year, today.month
    current = _num(hit.get("當月營收") or _pick(hit, "當月", "營收"))
    prior = _num(hit.get("去年當月營收") or _pick(hit, "去年當月", "營收"))
    out = []
    if prior is not None:
        out.append({"stock_id": ticker, "revenue_year": year - 1, "revenue_month": month, "revenue": prior,
                    "date": f"{year-1:04d}-{month:02d}-01", "_source": "TWSE OpenAPI t187ap05_L prior-year"})
    if current is not None:
        out.append({"stock_id": ticker, "revenue_year": year, "revenue_month": month, "revenue": current,
                    "date": f"{year:04d}-{month:02d}-01", "_source": "TWSE OpenAPI t187ap05_L"})
    return out


async def _t86_one(ticker: str, d: date) -> dict[str, Any] | None:
    payload = await _json(
        "https://www.twse.com.tw/rwd/zh/fund/T86",
        {"date": d.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"},
    )
    fields = payload.get("fields") or [] if isinstance(payload, dict) else []
    data = payload.get("data") or [] if isinstance(payload, dict) else []
    for raw in data:
        row = dict(zip(fields, raw))
        code = str(row.get("證券代號") or row.get("股票代號") or "").strip()
        if code != ticker:
            continue
        foreign = (_num(_pick(row, "外陸資", "買賣超", "不含外資自營商")) or 0) + (_num(_pick(row, "外資自營商", "買賣超")) or 0)
        trust = _num(_pick(row, "投信", "買賣超")) or 0
        dealer = _num(_pick(row, "自營商", "買賣超")) or 0
        fb, fs = _net_pair(foreign); tb, ts = _net_pair(trust); db, ds = _net_pair(dealer)
        return {
            "date": d.isoformat(), "stock_id": ticker,
            "Foreign_Investor_buy": fb, "Foreign_Investor_sell": fs,
            "Investment_Trust_buy": tb, "Investment_Trust_sell": ts,
            "Dealer_buy": db, "Dealer_sell": ds,
            "_source": "TWSE T86 official fallback",
        }
    return None


async def _official_institutional(ticker: str, days: int = 45) -> list[dict[str, Any]]:
    # Fetch recent dates concurrently, then keep the latest 20 trading observations.
    today = date.today()
    dates = [today - timedelta(days=i) for i in range(days)]
    sem = asyncio.Semaphore(6)
    async def one(d: date):
        async with sem:
            try:
                return await _t86_one(ticker, d)
            except Exception:
                return None
    rows = [x for x in await asyncio.gather(*(one(d) for d in dates)) if x]
    rows.sort(key=lambda x: x["date"])
    return rows[-22:]


async def _margin_one(ticker: str, d: date) -> dict[str, Any] | None:
    payload = await _json(
        "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
        {"date": d.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"},
    )
    if not isinstance(payload, dict):
        return None
    # TWSE may return multiple tables. Select the table containing a stock-code field.
    tables = payload.get("tables") or []
    candidates = []
    if tables:
        candidates = tables
    elif payload.get("fields") and payload.get("data"):
        candidates = [{"fields": payload.get("fields"), "data": payload.get("data")}]
    for table in candidates:
        fields = table.get("fields") or []
        for raw in table.get("data") or []:
            row = dict(zip(fields, raw))
            code = str(row.get("股票代號") or row.get("證券代號") or "").strip()
            if code != ticker:
                continue
            bal = _num(row.get("融資今日餘額") or row.get("融資餘額") or _pick(row, "融資", "今日餘額"))
            if bal is None:
                continue
            return {"date": d.isoformat(), "stock_id": ticker, "MarginPurchaseTodayBalance": bal,
                    "_source": "TWSE MI_MARGN official fallback"}
    return None


async def _official_margin(ticker: str, days: int = 45) -> list[dict[str, Any]]:
    today = date.today()
    dates = [today - timedelta(days=i) for i in range(days)]
    sem = asyncio.Semaphore(6)
    async def one(d: date):
        async with sem:
            try:
                return await _margin_one(ticker, d)
            except Exception:
                return None
    rows = [x for x in await asyncio.gather(*(one(d) for d in dates)) if x]
    rows.sort(key=lambda x: x["date"])
    return rows[-22:]


async def finmind_with_official_fallback(dataset: str, ticker: str | None = None, start=None, end=None):
    rows = []
    try:
        rows = await _original_finmind(dataset, ticker, start, end)
    except Exception:
        rows = []
    if rows:
        return rows
    try:
        if dataset == "TaiwanStockInfo":
            return await _official_company_info()
        if dataset == "TaiwanStockMonthRevenue" and ticker:
            return await _official_revenue(ticker)
        if dataset == "TaiwanStockInstitutionalInvestorsBuySellWide" and ticker:
            return await _official_institutional(ticker)
        if dataset == "TaiwanStockMarginPurchaseShortSale" and ticker:
            return await _official_margin(ticker)
    except Exception:
        return []
    return rows


server.finmind = finmind_with_official_fallback


async def build_stock_v546(ticker: str, force_refresh: bool = False):
    d = await _original_build_stock(ticker, force_refresh=force_refresh)
    if isinstance(d, dict):
        d["version"] = VERSION
        d["data_policy"] = (
            "V5.4.6 Data Completeness Repair：官方 TWSE/TPEX/公司 IR 為優先證據；"
            "FinMind 缺資料時啟用官方公司基本資料、月營收、三大法人與融資融券 fallback；"
            "V5.4.5 Data Recovery 僅在即時 API 失敗時載入本機最後成功報告，且明確標示非即時資料。"
        )
    return d

server.build_stock = build_stock_v546


@server.app.middleware("http")
async def v546_runtime_metadata(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({
            "status": "ok", "version": VERSION, "mode": "data-completeness-repair+data-recovery",
            "finmind_token": bool(server.FINMIND_TOKEN), "cache_ttl_seconds": server.CACHE_TTL,
            "pwa": True, "official_fallback": True, "data_recovery": True,
        }, headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    response.headers["X-AI-Stock-Version"] = VERSION
    return response

app = server.app
