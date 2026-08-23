"""V5.4.8 Valuation Recovery.

Extends V5.4.7 with an official TWSE fallback for TaiwanStockPER and treats
TWStock MCP strictly as a secondary cross-check when primary official evidence is usable.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

import run_v547
import server

VERSION = "5.4.8"
server.app.version = VERSION
_v547_finmind = server.finmind
_v547_build_stock = server.build_stock


def _num(v: Any):
    try:
        if v in (None, "", "--", "—", "-", "－"):
            return None
        return float(str(v).replace(",", "").replace("%", "").replace("％", "").strip())
    except Exception:
        return None


def _pick(row: dict[str, Any], *needles: str):
    for k, v in row.items():
        key = str(k).replace(" ", "").replace("（", "(").replace("）", ")")
        if all(n in key for n in needles):
            return v
    return None


def _iso_date(raw: Any) -> str:
    digits = "".join(c for c in str(raw or "") if c.isdigit())
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) == 7:
        return f"{int(digits[:3])+1911:04d}-{digits[3:5]}-{digits[5:7]}"
    return date.today().isoformat()


async def _official_per(ticker: str) -> list[dict[str, Any]]:
    # Primary official endpoint: TWSE daily valuation ratios (BWIBBU_ALL).
    candidates = [
        "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL",
        "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_d",
    ]
    for url in candidates:
        try:
            rows = await run_v546._json(url)
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(
                row.get("Code") or row.get("股票代號") or row.get("證券代號") or row.get("證券代碼")
                or _pick(row, "代號") or ""
            ).strip()
            if code != ticker:
                continue
            per = _num(row.get("PEratio") or row.get("PER") or row.get("本益比") or _pick(row, "本益比"))
            pbr = _num(row.get("PBratio") or row.get("PBR") or row.get("股價淨值比") or _pick(row, "股價淨值比"))
            dy = _num(row.get("DividendYield") or row.get("殖利率(%)") or row.get("殖利率") or _pick(row, "殖利率"))
            raw_date = row.get("Date") or row.get("日期") or row.get("資料日期")
            if per is None and pbr is None and dy is None:
                continue
            return [{
                "date": _iso_date(raw_date),
                "stock_id": ticker,
                "PER": per,
                "PBR": pbr,
                "dividend_yield": dy,
                "_source": "TWSE BWIBBU official valuation fallback",
            }]
    return []


async def finmind_v548(dataset: str, ticker: str | None = None, start=None, end=None):
    rows = await _v547_finmind(dataset, ticker, start, end)
    if rows or dataset != "TaiwanStockPER" or not ticker:
        return rows
    try:
        return await _official_per(ticker)
    except Exception:
        return []


server.finmind = finmind_v548


def _primary_sources_usable(d: dict[str, Any]) -> bool:
    statuses = d.get("source_status") or []
    wanted = {"股價", "三大法人", "月營收", "財務報表"}
    seen = {}
    for x in statuses:
        name = x.get("name")
        if name in wanted:
            seen[name] = x.get("status") in {"ok", "stale"}
    return bool(seen) and all(seen.get(k, False) for k in wanted)


def _normalize_mcp_secondary(d: dict[str, Any]):
    statuses = d.get("source_status") or []
    if not _primary_sources_usable(d):
        return
    for x in statuses:
        if x.get("name") == "TWStock MCP 二次驗證" and x.get("status") != "ok":
            x["status"] = "secondary_unavailable"
            x["scheduled_update"] = (
                "二次驗證來源暫時不可用；官方股價/法人/營收/財報仍為主證據，不降低主要資料可用性。"
            )
    try:
        filtered = [x for x in statuses if x.get("name") != "TWStock MCP 二次驗證"]
        d["confidence"] = server.calc_confidence(filtered, d.get("valuation") or {}, d.get("research") or {})
        d.setdefault("confidence", {})["mcp_policy"] = "secondary-only"
    except Exception:
        pass


async def build_stock_v548(ticker: str, force_refresh: bool = False):
    d = await _v547_build_stock(ticker, force_refresh=force_refresh)
    if isinstance(d, dict):
        d["version"] = VERSION
        _normalize_mcp_secondary(d)
        d["data_policy"] = (
            "V5.4.8 Valuation Recovery：延續 V5.4.7 Provider Isolation、官方資料 fallback 與 Data Recovery；"
            "PER/PBR/殖利率在 FinMind 缺資料時改由 TWSE BWIBBU 官方資料補位；"
            "TWStock MCP 僅作二次交叉驗證，官方主要證據完整時，MCP 暫時失敗不降低主要資料可用性。"
        )
    return d


server.build_stock = build_stock_v548


@server.app.middleware("http")
async def v548_runtime_metadata(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({
            "status": "ok", "version": VERSION,
            "mode": "valuation-recovery+provider-isolation+data-recovery",
            "finmind_token": bool(server.FINMIND_TOKEN),
            "cache_ttl_seconds": server.CACHE_TTL,
            "pwa": True, "official_fallback": True,
            "valuation_official_fallback": True,
            "mcp_secondary_only": True,
            "provider_html_isolation": True,
            "data_recovery": True,
        }, headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    response.headers["X-AI-Stock-Version"] = VERSION
    return response


app = server.app
