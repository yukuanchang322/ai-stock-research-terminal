"""V5.9.2 Version Sync Fix.
Keeps V5.9.1 data recovery, forces a single production version, and serves the
HTML shell with cache-busted core assets so Safari/PWA cannot remain on an old UI.
"""
from __future__ import annotations

import re
from fastapi.responses import HTMLResponse
import run_v591, server

VERSION = "5.9.2"
server.app.version = VERSION


async def build_stock_v592(ticker: str, force_refresh: bool = False):
    d = await run_v591.build_stock_v591(ticker, force_refresh=force_refresh)
    d["version"] = VERSION
    return d


app = server.app

# Replace inherited production stock endpoint so the UI always receives V5.9.2.
for r in list(app.routes):
    if getattr(r, "path", None) in ("/api/stock/{ticker}", "/"):
        try:
            app.routes.remove(r)
        except ValueError:
            pass


@app.get("/api/stock/{ticker}")
async def api_stock_v592(ticker: str, refresh: int = 0):
    return await build_stock_v592(ticker.strip(), force_refresh=bool(refresh))


@app.get("/api/v592/stock/{ticker}")
async def api_v592(ticker: str, refresh: int = 0):
    return await build_stock_v592(ticker.strip(), force_refresh=bool(refresh))


@app.get("/", response_class=HTMLResponse)
async def root_v592():
    """Serve a no-cache HTML shell and normalize legacy embedded version strings."""
    text = (server.ROOT / "index.html").read_text(encoding="utf-8")
    text = re.sub(r"AI Stock Research Terminal V5\.\d+(?:\.\d+)?", f"AI Stock Research Terminal V{VERSION}", text)
    text = re.sub(r"styles\.css(?:\?v=[^\"']+)?", f"styles.css?v={VERSION}", text)
    text = re.sub(r"app\.js(?:\?v=[^\"']+)?", f"app.js?v={VERSION}", text)
    text = re.sub(r"recovery\.js(?:\?v=[^\"']+)?", f"recovery.js?v={VERSION}", text)
    text = re.sub(r"v547_hotfix\.js(?:\?v=[^\"']+)?", f"v547_hotfix.js?v={VERSION}", text)
    # Normalize visible legacy release copy without touching model/data version labels.
    text = text.replace("V5.4.7 延續", f"V{VERSION} 延續")
    return HTMLResponse(
        text,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-App-Version": VERSION,
        },
    )
