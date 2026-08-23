"""V5.4.7 Margin + UI Cleanup backend wrapper.

Extends V5.4.6 with TWSE OpenAPI margin fallback and hardened upstream failure
handling. Provider HTML/CSS error pages are never exposed to the browser.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

import run_v546
import server

VERSION = "5.4.7"
server.app.version = VERSION
_v546_finmind = server.finmind
_v546_build_stock = server.build_stock


def _num(v: Any):
    try:
        if v in (None, "", "--", "—", "-"):
            return None
        return float(str(v).replace(",", "").replace("+", "").strip())
    except Exception:
        return None


def _find_value(row: dict[str, Any], include_all: tuple[str, ...], include_any: tuple[str, ...] = ()):
    for k, v in row.items():
        key = str(k).replace(" ", "").replace("（", "(").replace("）", ")")
        if all(x in key for x in include_all) and (not include_any or any(x in key for x in include_any)):
            return v
    return None


async def _openapi_margin(ticker: str) -> list[dict[str, Any]]:
    rows = await run_v546._json("https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN")
    if not isinstance(rows, list):
        return []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("股票代號") or row.get("證券代號") or row.get("證券代碼") or _find_value(row, ("代號",)) or "").strip()
        if code != ticker:
            continue
        balance = (_num(row.get("融資今日餘額")) or _num(row.get("融資餘額")) or
                   _num(_find_value(row, ("融資", "今日餘額"))) or _num(_find_value(row, ("融資",), ("餘額",))))
        if balance is None:
            continue
        raw_date = str(row.get("日期") or row.get("資料日期") or "").strip()
        digits = "".join(c for c in raw_date if c.isdigit())
        as_of = date.today().isoformat()
        if len(digits) == 8:
            as_of = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        elif len(digits) == 7:
            as_of = f"{int(digits[:3])+1911:04d}-{digits[3:5]}-{digits[5:7]}"
        return [{"date": as_of, "stock_id": ticker, "MarginPurchaseTodayBalance": balance,
                 "_source": "TWSE OpenAPI MI_MARGN official fallback"}]
    return []


async def finmind_v547(dataset: str, ticker: str | None = None, start=None, end=None):
    rows = await _v546_finmind(dataset, ticker, start, end)
    if rows or dataset != "TaiwanStockMarginPurchaseShortSale" or not ticker:
        return rows
    try:
        return await _openapi_margin(ticker)
    except Exception:
        return []


server.finmind = finmind_v547


def _cached_report(ticker: str):
    try:
        entry = server._CACHE.get(ticker)
        if not entry or not isinstance(entry, tuple) or len(entry) < 2 or not isinstance(entry[1], dict):
            return None
        d = deepcopy(entry[1])
        d["version"] = VERSION
        d["server_recovery"] = {"active": True, "reason": "live upstream provider unavailable"}
        d["data_policy"] = "V5.4.7 Server Recovery：即時上游暫時異常，使用本服務程序內最後一次成功研究結果；資料時間以報告 generated_at 為準。"
        return d
    except Exception:
        return None


async def build_stock_v547(ticker: str, force_refresh: bool = False):
    try:
        d = await _v546_build_stock(ticker, force_refresh=force_refresh)
    except Exception:
        # Never leak an upstream HTML/CSS body into API/UI. Prefer last-good server cache.
        cached = _cached_report(ticker)
        if cached is not None:
            return cached
        # A forced refresh may fail while a normal request can still use the base cache.
        if force_refresh:
            try:
                d = await _v546_build_stock(ticker, force_refresh=False)
            except Exception:
                raise RuntimeError("即時資料來源暫時異常（502）。系統未採用上游 HTML/CSS 錯誤內容，請稍後再試。")
        else:
            raise RuntimeError("即時資料來源暫時異常（502）。系統未採用上游 HTML/CSS 錯誤內容，請稍後再試。")
    if isinstance(d, dict):
        d["version"] = VERSION
        d["data_policy"] = (
            "V5.4.7 Margin + UI Cleanup：延續 V5.4.6 官方資料 fallback；融資融券新增 TWSE OpenAPI "
            "MI_MARGN 第二官方備援；上游 HTML/CSS 錯誤頁不進研究資料；V5.4.5 Data Recovery 僅在即時 API 失敗時載入最後成功報告。"
        )
    return d


server.build_stock = build_stock_v547


@server.app.middleware("http")
async def v547_runtime_metadata(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({"status": "ok", "version": VERSION,
            "mode": "margin-openapi-fallback+provider-isolation+data-recovery",
            "finmind_token": bool(server.FINMIND_TOKEN), "cache_ttl_seconds": server.CACHE_TTL,
            "pwa": True, "official_fallback": True, "margin_openapi_fallback": True,
            "provider_html_isolation": True, "server_last_good_recovery": True, "data_recovery": True,
        }, headers={"Cache-Control": "no-store"})
    try:
        response = await call_next(request)
    except Exception:
        if request.url.path.startswith("/api/stock/"):
            return JSONResponse({"detail": "即時資料來源暫時異常，已阻擋非 JSON/HTML/CSS 錯誤內容。請稍後重新整理。", "version": VERSION}, status_code=502, headers={"Cache-Control": "no-store", "X-AI-Stock-Version": VERSION})
        raise
    response.headers["X-AI-Stock-Version"] = VERSION
    return response


app = server.app
