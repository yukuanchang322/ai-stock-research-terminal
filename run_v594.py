"""V5.9.4 Data Integrity.
Fixes institutional T86 recovery with nearest-trading-day fallback and direct net-field aliases,
synchronizes freshness labels with actual payload availability, exposes the production version
consistently, and patches mobile overflow without disturbing recovered price/revenue/margin data.
"""
from __future__ import annotations

from datetime import date, timedelta
import re
import asyncio
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

import run_v593, run_v590, server

VERSION = "5.9.4"
server.app.version = VERSION

num = run_v590.num
pick = run_v590.pick
get_json = run_v590.get_json
rows_from_rwd = run_v590.rows_from_rwd
merge_cf = run_v590.merge_cf


def _code(r):
    return str(pick(r, "證券代號", "股票代號", "Code", "stock_id") or "").strip()


def _institutional_row(r, px):
    # Prefer actual buy/sell columns so the UI can show buy amount / sell amount.
    aliases = {
        "foreign": (
            ("外陸資買進股數(不含外資自營商)", "外陸資買進股數(不含自營商)", "外資及陸資買進股數"),
            ("外陸資賣出股數(不含外資自營商)", "外陸資賣出股數(不含自營商)", "外資及陸資賣出股數"),
            ("外陸資買賣超股數(不含外資自營商)", "外陸資買賣超股數(不含自營商)", "外資及陸資買賣超股數"),
        ),
        "trust": (("投信買進股數",), ("投信賣出股數",), ("投信買賣超股數",)),
        "dealer": (("自營商買進股數",), ("自營商賣出股數",), ("自營商買賣超股數",)),
    }
    out = {}
    for who, (bks, sks, nks) in aliases.items():
        b = num(pick(r, *bks)); s = num(pick(r, *sks)); n = num(pick(r, *nks))
        if who == "dealer" and (b is None or s is None):
            b1 = num(pick(r, "自營商買進股數(自行買賣)")); b2 = num(pick(r, "自營商買進股數(避險)"))
            s1 = num(pick(r, "自營商賣出股數(自行買賣)")); s2 = num(pick(r, "自營商賣出股數(避險)"))
            if b1 is not None or b2 is not None: b = (b1 or 0) + (b2 or 0)
            if s1 is not None or s2 is not None: s = (s1 or 0) + (s2 or 0)
        if n is None and b is not None and s is not None: n = b - s
        if n is None: continue
        out[who] = {"1": {
            "buy": b * px if b is not None and px else None,
            "sell": s * px if s is not None and px else None,
            "net": n * px if px else n,
            "shares_net": n,
            "days": 1,
        }}
    return out


async def _t86_nearest(ticker: str, d: dict):
    try:
        end = date.fromisoformat(str(d.get("price_as_of") or date.today().isoformat())[:10])
    except Exception:
        end = date.today()
    px = num(d.get("price"))
    attempts = []
    for back in range(0, 9):
        dt = end - timedelta(days=back)
        if dt.weekday() >= 5: continue
        q = dt.strftime("%Y%m%d")
        try:
            j = await get_json("https://www.twse.com.tw/rwd/zh/fund/T86", {
                "date": q, "selectType": "ALLBUT0999", "response": "json"
            }, timeout=8)
            rs = rows_from_rwd(j)
            attempts.append({"date": dt.isoformat(), "rows": len(rs), "stat": (j or {}).get("stat") if isinstance(j, dict) else None})
            r = next((x for x in rs if _code(x) == ticker), None)
            if not r: continue
            inst = _institutional_row(r, px)
            if not inst: continue
            return {
                "institutional": inst,
                "institutional_rows": 1,
                "institutional_source": "TWSE T86 official",
                "last_date": dt.isoformat(),
            }, attempts
        except Exception as e:
            attempts.append({"date": dt.isoformat(), "error": type(e).__name__})
    return None, attempts


def _sync_source_status(d: dict):
    rows = d.get("source_status") if isinstance(d.get("source_status"), list) else []
    cf = d.get("cashflow") if isinstance(d.get("cashflow"), dict) else {}
    fin_ok = bool(d.get("official_financial") or d.get("financial") or d.get("eps_stack"))
    fin_asof = (d.get("financial_integrity") or {}).get("latest_period") or (d.get("official_financial") or {}).get("period") or d.get("financial_period")
    seen = set()
    for row in rows:
        name = row.get("name"); seen.add(name)
        if name == "財務報表":
            row.update({"status": "ok" if fin_ok else "missing", "as_of": fin_asof or row.get("as_of"), "dataset": row.get("dataset") or "TWSE/MOPS official financial"})
        elif name == "三大法人":
            row.update({"status": "ok" if cf.get("institutional") else "missing", "as_of": cf.get("last_date"), "dataset": cf.get("institutional_source") or "TWSE T86"})
        elif name == "融資融券":
            ok = cf.get("margin_balance") is not None or cf.get("short_balance") is not None
            row.update({"status": "ok" if ok else "missing", "as_of": cf.get("margin_last_date") or cf.get("margin_short_as_of"), "dataset": cf.get("margin_short_source") or "TWSE MI_MARGN"})
    if "財務報表" not in seen:
        rows.append({"name":"財務報表","dataset":"TWSE/MOPS official financial","as_of":fin_asof,"status":"ok" if fin_ok else "missing"})
    d["source_status"] = rows
    return d


def _reconfidence(d: dict):
    try:
        comp = run_v590.completeness(d)
        d["data_completeness"] = comp
        evscore = None
        try:
            ev = server.build_evidence_graph(d.get("ticker"), d.get("technical") or {}, d.get("revenue") or {}, d.get("flow") or {}, d.get("per") or {}, d.get("financial") or {}, d.get("eps_stack") or {}, d.get("research") or {}, d.get("company_events") or {}, d.get("financial_integrity") or {}, {})
            d["evidence"] = ev
            evscore = ((ev.get("summary") or {}).get("evidence_score"))
        except Exception:
            pass
        overall = round(float(evscore) * .6 + comp * .4) if evscore is not None else comp
        d["confidence"] = {"overall": overall, "data_completeness": comp, "evidence_score": evscore}
    except Exception:
        pass
    return d


async def build_stock_v594(ticker: str, force_refresh: bool = False):
    d = await run_v593.build_stock_v593(ticker, force_refresh=force_refresh)
    d["version"] = VERSION
    cf = d.get("cashflow") if isinstance(d.get("cashflow"), dict) else {}
    if not cf.get("institutional"):
        try:
            extra, attempts = await asyncio.wait_for(_t86_nearest(ticker, d), timeout=20)
            if extra: d = merge_cf(d, extra)
            d.setdefault("pipeline_diagnostics", {})["t86_nearest_attempts"] = attempts
            d["pipeline_diagnostics"]["institutional_recovered_v594"] = bool(extra)
        except Exception as e:
            d.setdefault("pipeline_diagnostics", {})["institutional_v594_error"] = f"{type(e).__name__}: {str(e)[:160]}"
    try:
        d["scores"] = server.scores(d.get("technical") or {}, d.get("revenue") or {}, d.get("flow") or {}, d.get("per") or {}, d.get("financial") or {}, d.get("research") or {})
    except Exception:
        pass
    d = _sync_source_status(d)
    d = _reconfidence(d)
    d["version"] = VERSION
    return d


app = server.app

# Replace inherited stock/root endpoints with V5.9.4.
for r in list(app.routes):
    if getattr(r, "path", None) in ("/api/stock/{ticker}", "/"):
        try: app.routes.remove(r)
        except ValueError: pass

@app.get("/api/stock/{ticker}")
async def api_stock_v594(ticker: str, refresh: int = 0):
    return await build_stock_v594(ticker.strip(), force_refresh=bool(refresh))

@app.get("/api/v594/stock/{ticker}")
async def api_v594(ticker: str, refresh: int = 0):
    return await build_stock_v594(ticker.strip(), force_refresh=bool(refresh))

@app.get("/", response_class=HTMLResponse)
async def root_v594():
    text = (server.ROOT / "index.html").read_text(encoding="utf-8")
    text = re.sub(r"AI Stock Research Terminal V5\.\d+(?:\.\d+)?", f"AI Stock Research Terminal V{VERSION}", text)
    for asset in ("styles.css", "app.js", "recovery.js", "v547_hotfix.js"):
        text = re.sub(rf"{re.escape(asset)}(?:\?v=[^\"']+)?", f"{asset}?v={VERSION}", text)
    text = re.sub(r'<span data-app-version>.*?</span>\s*<span class="status-sep">•</span>', '', text)
    patch = '''<style id="v594-mobile-fix">@media(max-width:900px){html,body{max-width:100%;overflow-x:hidden}.report,main,.hero,.search-card{width:100%;max-width:100%;min-width:0}.freshness-strip,.kpi-grid{width:100%;min-width:0;grid-template-columns:repeat(2,minmax(0,1fr));overflow:hidden}.fresh,.kpi{min-width:0;max-width:100%;overflow:hidden;padding:12px 10px}.fresh b,.kpi b{overflow-wrap:anywhere}.section,.report-head,.summary-grid,.panel{min-width:0}.cloud-status{flex-wrap:wrap}}</style>'''
    text = text.replace("</head>", patch + "</head>")
    return HTMLResponse(text, headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0","Pragma":"no-cache","Expires":"0","X-App-Version":VERSION})

# Last-added middleware is outermost: normalize /health even though older versions installed middleware.
@app.middleware("http")
async def v594_runtime(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({"status":"ok","version":VERSION,"mode":"data-integrity","institutional":"TWSE T86 nearest trading day","financial_status_sync":True,"mobile_overflow_fix":True}, headers={"Cache-Control":"no-store"})
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp
