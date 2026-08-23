"""V5.7.4 Stable Full-Report Recovery.

Rolls the research core back to the last same-day pipeline that was verified to return
complete reports (V5.5.0), while keeping the current runtime/version and Render fixes.
The V5.5.0 core already includes official TWSE STOCK_DAY price validation and
recalculates valuation/scores after price correction.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

import run_v550
import server

VERSION = "5.7.4"
server.app.version = VERSION
_base = run_v550.build_stock_v550


def _normalize(d):
    if not isinstance(d, dict):
        return d
    d["version"] = VERSION
    d["pipeline_state"] = "stable_recovery"
    d["report_partial"] = False
    d["status_text"] = "穩定完整研究管線"
    # Never display literal null strings when a legacy UI renders score values.
    s = d.get("scores") if isinstance(d.get("scores"), dict) else {}
    for k in ("綜合", "基本面", "籌碼面", "技術面", "估值"):
        if k not in s:
            s[k] = None
    d["scores"] = s
    if not isinstance(d.get("source_status"), list):
        d["source_status"] = []
    return d


async def build_stock_v574(ticker: str, force_refresh: bool = False):
    d = await _base(ticker, force_refresh=force_refresh)
    return _normalize(d)


server.build_stock = build_stock_v574


@server.app.middleware("http")
async def runtime(request: Request, call_next):
    if request.url.path == "/health":
        return JSONResponse({
            "status": "ok",
            "version": VERSION,
            "mode": "stable-full-report-recovery",
            "core_baseline": "V5.5.0",
            "official_close_guard": True,
            "partial_background_pipeline": False,
        }, headers={"Cache-Control": "no-store"})
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp


app = server.app
