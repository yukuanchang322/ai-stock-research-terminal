"""V5.10.0 Integrated Fix

Goals
- One visible runtime version: /health is authoritative; no static legacy version badge.
- Preserve the stable V5.9.9 core pipeline (price/name/revenue/T86/margin/technical).
- Financial recovery is isolated and additive: it can never erase already-good core data.
- If financial data is still unavailable, expose the actual official-source failure reason in source_status.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

import run_v599, run_v597, run_v596, run_v594, server

VERSION = "5.10.0"
server.app.version = VERSION


def _present(v: Any) -> bool:
    return v not in (None, "", {}, [])


def _financial_present(d: dict) -> bool:
    f = d.get("financial")
    return isinstance(f, dict) and any(f.get(k) is not None for k in ("ytd_eps", "quarter_eps", "ttm_eps", "revenue", "gross_profit", "net_income"))


def _snapshot_from_diag(diag: dict) -> dict | None:
    """Pick the best official snapshot already observed by the isolated diagnostic probe."""
    candidates: list[dict] = []
    for x in diag.get("company_mops") or []:
        if isinstance(x, dict): candidates.append(x)
    for group in ("openapi", "mops_csv"):
        for item in diag.get(group) or []:
            if isinstance(item, dict) and isinstance(item.get("snapshot"), dict):
                snap = dict(item["snapshot"])
                snap.setdefault("source", item.get("source"))
                snap.setdefault("endpoint", item.get("url"))
                candidates.append(snap)
    for x in diag.get("mops_material") or []:
        if isinstance(x, dict): candidates.append(x)
    sel = diag.get("selection")
    if isinstance(sel, dict): candidates.append(sel)
    valid = [x for x in candidates if any(x.get(k) is not None for k in ("ytd_eps", "revenue_ytd", "gross_profit_ytd", "net_income_ytd"))]
    if not valid:
        return None
    valid.sort(key=lambda x: (
        x.get("fiscal_year") or 0,
        x.get("fiscal_quarter") or 0,
        x.get("completeness") or 0,
    ), reverse=True)
    out = dict(valid[0])
    out["official"] = True
    return out


def _diag_reason(diag: dict) -> str:
    # Prefer a concise, actionable reason rather than generic "缺資料".
    if diag.get("company_mops_error"):
        return "MOPS公司財報查詢失敗"
    openapi = diag.get("openapi") or []
    if openapi and all((x.get("http_status") not in (200, None)) for x in openapi if isinstance(x, dict)):
        return "TWSE/MOPS官方端點暫時不可用"
    if openapi and not any(x.get("matched") for x in openapi if isinstance(x, dict)):
        return "官方彙總財報尚未匹配此公司/季度"
    if any(x.get("matched") and not x.get("snapshot") for x in openapi if isinstance(x, dict)):
        return "官方有公司資料但財報欄位解析未完成"
    if diag.get("expected_period"):
        return f"官方尚未取得 {diag.get('expected_period')} 可解析財報"
    return "官方財報來源目前未回傳可解析資料"


def _write_source_status(d: dict, name: str, *, status: str, as_of: str | None, reason: str | None = None):
    rows = d.get("source_status")
    if not isinstance(rows, list):
        rows = []
        d["source_status"] = rows
    row = next((x for x in rows if isinstance(x, dict) and x.get("name") == name), None)
    if row is None:
        row = {"name": name, "dataset": name, "scheduled_update": "官方公告後更新"}
        rows.append(row)
    row["status"] = status
    row["as_of"] = as_of
    if reason:
        row["reason"] = reason
        if not as_of:
            row["as_of"] = f"缺資料｜{reason}"


async def _financial_recovery(ticker: str, d: dict) -> dict:
    if _financial_present(d):
        return d

    pd = d.setdefault("pipeline_diagnostics", {})
    # First: proven production fetcher. Keep this bounded so it cannot hold the whole report indefinitely.
    try:
        snap = await asyncio.wait_for(server.fetch_official_income_statement(ticker), timeout=32)
    except Exception as e:
        snap = None
        pd["financial_v5100_primary"] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:160]}"}

    # If the primary fetcher failed, run the isolated diagnostic probe once and reuse any snapshot it actually observed.
    diag = None
    if not isinstance(snap, dict) or not snap.get("official") or not any(snap.get(k) is not None for k in ("ytd_eps", "revenue_ytd", "gross_profit_ytd", "net_income_ytd")):
        try:
            diag = await asyncio.wait_for(server.diagnose_official_financial_sources(ticker), timeout=38)
            snap = _snapshot_from_diag(diag) or snap
        except Exception as e:
            pd["financial_v5100_diag"] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:160]}"}

    if not isinstance(snap, dict) or not snap.get("official") or not any(snap.get(k) is not None for k in ("ytd_eps", "revenue_ytd", "gross_profit_ytd", "net_income_ytd")):
        reason = _diag_reason(diag or {})
        pd["financial_v5100"] = {"status": "missing", "reason": reason, "diagnostic": diag or {}}
        _write_source_status(d, "財務報表", status="missing", as_of=None, reason=reason)
        return d

    try:
        eps_stack = await asyncio.wait_for(server.build_eps_stack(ticker, [], snap, {}), timeout=12)
    except Exception:
        eps_stack = {"ytd_eps": snap.get("ytd_eps"), "quarter_period": snap.get("period"), "source": snap.get("source")}
    try:
        integrity = server.assess_financial_integrity(snap, eps_stack, date.today())
    except Exception:
        integrity = {}

    fin = {
        "statement_date": snap.get("statement_date") or snap.get("period"),
        "period": snap.get("period"), "source": snap.get("source"), "official": True,
        "partial": False, "ytd_eps": snap.get("ytd_eps"),
        "quarter_eps": eps_stack.get("quarter_eps"), "ttm_eps": eps_stack.get("ttm_eps"),
        "revenue": snap.get("revenue_ytd"), "gross_profit": snap.get("gross_profit_ytd"),
        "operating_income": snap.get("operating_income_ytd"), "net_income": snap.get("net_income_ytd"),
    }
    rev = fin.get("revenue")
    if rev not in (None, 0):
        if fin.get("gross_profit") is not None: fin["gross_margin"] = fin["gross_profit"] / rev * 100
        if fin.get("operating_income") is not None: fin["operating_margin"] = fin["operating_income"] / rev * 100
        if fin.get("net_income") is not None: fin["net_margin"] = fin["net_income"] / rev * 100

    d["official_financial"] = snap
    d["financial"] = fin
    d["eps_stack"] = eps_stack
    d["financial_integrity"] = integrity
    period = str(snap.get("period") or snap.get("statement_date") or "官方可用")
    _write_source_status(d, "財務報表", status="ok", as_of=period)
    pd["financial_v5100"] = {"status": "ok", "period": period, "source": snap.get("source"), "endpoint": snap.get("endpoint")}
    try:
        d["valuation"] = server.model_valuation(d.get("price"), d.get("per") or {}, eps_stack, d.get("research") or {}, integrity)
    except Exception:
        pass
    return d


async def build_stock_v5100(ticker: str, force_refresh: bool = False):
    # V5.9.9 is the stabilization baseline. Never replace its core payload with diagnostics.
    d = await run_v599.build_stock_v599(ticker, force_refresh=force_refresh)

    # Hard guard against core-data regression: if monthly revenue disappeared, retry V5.9.7 and merge ONLY revenue/name.
    rev = d.get("revenue")
    if not (isinstance(rev, dict) and rev.get("series")):
        try:
            stable = await asyncio.wait_for(run_v597.build_stock_v597(ticker, force_refresh=True), timeout=52)
            if isinstance(stable.get("revenue"), dict) and stable["revenue"].get("series"):
                d["revenue"] = stable["revenue"]
            for k in ("name", "company_name"):
                if not _present(d.get(k)) and _present(stable.get(k)): d[k] = stable[k]
        except Exception as e:
            d.setdefault("pipeline_diagnostics", {})["revenue_guard_v5100"] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:150]}"}

    d = await _financial_recovery(ticker, d)
    try: d = run_v596._sync_financial_status(d)
    except Exception: pass
    try:
        d["scores"] = server.scores(d.get("technical") or {}, d.get("revenue") or {}, d.get("flow") or {}, d.get("per") or {}, d.get("financial") or {}, d.get("research") or {})
    except Exception: pass
    try: d = run_v594._reconfidence(d)
    except Exception: pass
    d["version"] = VERSION
    return d


app = server.app
for r in list(app.routes):
    if getattr(r, "path", None) in ("/api/stock/{ticker}", "/", "/health"):
        try: app.routes.remove(r)
        except ValueError: pass

@app.get("/api/stock/{ticker}")
async def api_stock_v5100(ticker: str, refresh: int = 0):
    return await build_stock_v5100(ticker.strip(), force_refresh=bool(refresh))

@app.get("/api/v5100/stock/{ticker}")
async def api_v5100(ticker: str, refresh: int = 0):
    return await build_stock_v5100(ticker.strip(), force_refresh=bool(refresh))

@app.get("/api/v5100/financial-diagnostics/{ticker}")
async def financial_diag_v5100(ticker: str):
    return await server.diagnose_official_financial_sources(ticker.strip())

@app.get("/health")
async def health_v5100():
    return JSONResponse({"status": "ok", "version": VERSION, "mode": "integrated-core-financial-version-integrity"}, headers={"Cache-Control": "no-store"})

@app.get("/", response_class=HTMLResponse)
async def root_v5100():
    text = (server.ROOT / "index.html").read_text(encoding="utf-8")
    # Version is runtime-only. Strip every legacy static version badge before delivery.
    text = re.sub(r'<span[^>]*data-app-version[^>]*>.*?</span>', '', text, flags=re.S | re.I)
    text = re.sub(r'AI Stock Research Terminal V5\.\d+(?:\.\d+)?', f'AI Stock Research Terminal V{VERSION}', text)
    for asset in ("styles.css", "app.js", "recovery.js", "v547_hotfix.js", "v5100_hotfix.js"):
        text = re.sub(rf'{re.escape(asset)}(?:\?v=[^\"\']+)?', f'{asset}?v={VERSION}', text)
    text = re.sub(r'/sw\.js(?:\?v=[^\"\']+)?', f'/sw.js?v={VERSION}', text)
    return HTMLResponse(text, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache", "Expires": "0", "Clear-Site-Data": "\"cache\"",
        "X-App-Version": VERSION,
    })

@app.middleware("http")
async def v5100_runtime(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp
