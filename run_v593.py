"""V5.9.3 Frontend Cache Sync.
Extends V5.9.2 data recovery and aligns the backend/API version with the
cache-busted V5.9.3 frontend shell.
"""
from __future__ import annotations

import re
from fastapi.responses import HTMLResponse
import run_v592, server

VERSION = "5.9.3"
server.app.version = VERSION


async def build_stock_v593(ticker: str, force_refresh: bool = False):
    d = await run_v592.build_stock_v592(ticker, force_refresh=force_refresh)
    d["version"] = VERSION
    return d


app = server.app

# Replace inherited production stock/root routes with V5.9.3 routes.
for r in list(app.routes):
    if getattr(r, "path", None) in ("/api/stock/{ticker}", "/"):
        try:
            app.routes.remove(r)
        except ValueError:
            pass


@app.get("/api/stock/{ticker}")
async def api_stock_v593(ticker: str, refresh: int = 0):
    return await build_stock_v593(ticker.strip(), force_refresh=bool(refresh))


@app.get("/api/v593/stock/{ticker}")
async def api_v593(ticker: str, refresh: int = 0):
    return await build_stock_v593(ticker.strip(), force_refresh=bool(refresh))


@app.get("/", response_class=HTMLResponse)
async def root_v593():
    text = (server.ROOT / "index.html").read_text(encoding="utf-8")
    text = re.sub(r"AI Stock Research Terminal V5\.\d+(?:\.\d+)?", f"AI Stock Research Terminal V{VERSION}", text)
    for asset in ("styles.css", "app.js", "recovery.js", "v547_hotfix.js"):
        text = re.sub(rf"{re.escape(asset)}(?:\?v=[^\"']+)?", f"{asset}?v={VERSION}", text)
    text = text.replace("V5.9.2", f"V{VERSION}")
    return HTMLResponse(
        text,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-App-Version": VERSION,
        },
    )
