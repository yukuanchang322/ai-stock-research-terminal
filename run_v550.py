"""V5.5.0 Price Integrity Guard.

Extends V5.4.9 with a final official TWSE STOCK_DAY close-price validation step.
If the research payload price differs from the latest official close, the official close
wins and all price-sensitive valuation/score outputs are recalculated before response.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

import run_v546
import run_v549
import server

VERSION = "5.5.0"
server.app.version = VERSION
_v549_build_stock = server.build_stock


def _num(v: Any):
    try:
        if v in (None, "", "--", "—", "-"):
            return None
        return float(str(v).replace(",", "").replace("+", "").strip())
    except Exception:
        return None


def _roc_to_iso(raw: Any) -> str:
    s = str(raw or "").strip()
    parts = s.replace("-", "/").split("/")
    if len(parts) == 3:
        try:
            y = int(parts[0])
            if y < 1911:
                y += 1911
            return f"{y:04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        except Exception:
            pass
    return s


async def _twse_month(ticker: str, y: int, m: int) -> list[dict[str, Any]]:
    payload = await run_v546._json(
        "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY",
        {"date": f"{y:04d}{m:02d}01", "stockNo": ticker, "response": "json"},
    )
    if not isinstance(payload, dict):
        return []
    fields = payload.get("fields") or []
    data = payload.get("data") or []
    out = []
    for raw in data:
        row = dict(zip(fields, raw))
        close = _num(row.get("收盤價"))
        if close is None:
            continue
        out.append({
            "date": _roc_to_iso(row.get("日期")),
            "close": close,
            "open": _num(row.get("開盤價")),
            "high": _num(row.get("最高價")),
            "low": _num(row.get("最低價")),
            "volume": _num(row.get("成交股數")),
        })
    return out


async def _official_recent_closes(ticker: str) -> list[dict[str, Any]]:
    today = date.today()
    months = [(today.year, today.month)]
    if today.month == 1:
        months.append((today.year - 1, 12))
    else:
        months.append((today.year, today.month - 1))
    rows = []
    for y, m in months:
        try:
            rows.extend(await _twse_month(ticker, y, m))
        except Exception:
            continue
    dedup = {x["date"]: x for x in rows if x.get("date")}
    return [dedup[k] for k in sorted(dedup)][-30:]


def _rebuild_price_sensitive(d: dict[str, Any], official: list[dict[str, Any]]):
    if not official:
        return d
    latest = official[-1]
    prior = official[-2] if len(official) >= 2 else None
    official_close = latest["close"]
    old_price = _num(d.get("price"))

    d["price_integrity"] = {
        "status": "verified" if old_price == official_close else "corrected",
        "source": "TWSE STOCK_DAY official close",
        "official_close": official_close,
        "official_date": latest.get("date"),
        "original_payload_price": old_price,
    }

    # Official exchange close is authoritative for listed stocks.
    d["price"] = official_close
    tech = d.setdefault("technical", {})
    tech["last"] = official_close
    tech["last_date"] = latest.get("date")
    if prior and prior.get("close"):
        d["change_pct"] = (official_close / prior["close"] - 1) * 100
    if tech.get("series"):
        last = tech["series"][-1]
        if str(last.get("date")) == str(latest.get("date")):
            last["close"] = official_close
            if latest.get("open") is not None: last["open"] = latest["open"]
            if latest.get("high") is not None: last["high"] = latest["high"]
            if latest.get("low") is not None: last["low"] = latest["low"]
            if latest.get("volume") is not None: last["volume"] = latest["volume"]

    # Recalculate every output that directly depends on price/valuation.
    d["valuation"] = server.model_valuation(
        official_close, d.get("per") or {}, d.get("eps_stack") or {},
        d.get("research") or {}, d.get("financial_integrity") or {},
    )
    d["scores"] = server.scores(
        d.get("technical") or {}, d.get("revenue") or {}, d.get("flow") or {},
        d.get("per") or {}, d.get("financial") or {}, d.get("research") or {},
    )
    nar = server.narrative(
        d["scores"], d.get("technical") or {}, d.get("revenue") or {},
        d.get("flow") or {}, d.get("valuation") or {}, d.get("research") or {},
    )
    d["stance"] = nar.get("stance")
    d["thesis"] = nar.get("thesis")
    d["catalysts"] = nar.get("catalysts")
    d["risks"] = nar.get("risks")
    d["expectation_gap"] = server.expectation_gap_analysis(
        d.get("research") or {}, d.get("company_events") or {}, d.get("per") or {},
        d.get("revenue") or {}, d.get("scores") or {}, official_close,
    )

    # Correct source freshness label and make the repair visible/auditable.
    for x in d.get("source_status") or []:
        if x.get("name") == "股價":
            x["dataset"] = "TWSE STOCK_DAY official close guard"
            x["as_of"] = latest.get("date")
            x["status"] = "ok"
    d["data_policy"] = (d.get("data_policy") or "") + (
        " V5.5.0 Price Integrity Guard：最終輸出前以 TWSE STOCK_DAY 官方收盤價二次驗證；"
        "若主資料價格不一致，交易所收盤價優先並重算估值、Research Score 與價格敏感結論。"
    )
    return d


async def build_stock_v550(ticker: str, force_refresh: bool = False):
    d = await _v549_build_stock(ticker, force_refresh=force_refresh)
    if not isinstance(d, dict):
        return d
    try:
        official = await _official_recent_closes(ticker)
        d = _rebuild_price_sensitive(d, official)
    except Exception as exc:
        d["price_integrity"] = {"status": "unavailable", "error": type(exc).__name__}
    d["version"] = VERSION
    try:
        server._CACHE[ticker] = (time.time(), d)
    except Exception:
        pass
    return d


server.build_stock = build_stock_v550


@server.app.middleware("http")
async def v550_runtime_metadata(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({
            "status": "ok", "version": VERSION,
            "mode": "official-close-integrity+revenue-bars+market-anchored-valuation+data-recovery",
            "price_integrity_guard": True,
            "price_source": "TWSE STOCK_DAY",
            "valuation_guardrail": True,
            "revenue_yoy_bars": True,
            "pwa": True,
            "official_fallback": True,
            "data_recovery": True,
        }, headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    response.headers["X-AI-Stock-Version"] = VERSION
    return response


app = server.app
