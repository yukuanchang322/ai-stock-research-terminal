"""V5.10.1 Frontend Asset Fix

Keeps the V5.10.0 data pipeline unchanged while fixing the production shell:
- serve root assets from the mounted /static path (no more 404 JS/CSS),
- remove legacy recovery/v547 scripts that overwrite runtime version/UI,
- expose one authoritative version from /health,
- keep service worker available at /sw.js with no-cache headers.
"""
from __future__ import annotations

import re
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

import run_v5100, server

VERSION = "5.10.1"
server.app.version = VERSION
app = server.app

# Remove inherited endpoints that this entrypoint owns.
for r in list(app.routes):
    if getattr(r, "path", None) in ("/api/stock/{ticker}", "/", "/health", "/sw.js"):
        try:
            app.routes.remove(r)
        except ValueError:
            pass


@app.get("/api/stock/{ticker}")
async def api_stock_v5101(ticker: str, refresh: int = 0):
    d = await run_v5100.build_stock_v5100(ticker.strip(), force_refresh=bool(refresh))
    d["version"] = VERSION
    return d


@app.get("/api/v5101/stock/{ticker}")
async def api_v5101(ticker: str, refresh: int = 0):
    return await api_stock_v5101(ticker, refresh)


@app.get("/health")
async def health_v5101():
    return JSONResponse(
        {"status": "ok", "version": VERSION, "mode": "frontend-asset-fix"},
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/sw.js")
async def service_worker_v5101():
    return FileResponse(
        server.ROOT / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/", response_class=HTMLResponse)
async def root_v5101():
    text = (server.ROOT / "index.html").read_text(encoding="utf-8")

    # One version source only. Remove the static legacy badge from index.html.
    text = re.sub(r'<span[^>]*data-app-version[^>]*>.*?</span>', '', text, flags=re.S | re.I)
    text = re.sub(r'AI Stock Research Terminal V5\.\d+(?:\.\d+)?', f'AI Stock Research Terminal V{VERSION}', text)

    # Assets live behind server.app.mount('/static', StaticFiles(directory=ROOT)).
    text = re.sub(r'href="styles\.css(?:\?v=[^"]+)?"', f'href="/static/styles.css?v={VERSION}"', text)
    text = re.sub(r'href="/static/manifest\.webmanifest(?:\?v=[^"]+)?"', f'href="/static/manifest.webmanifest?v={VERSION}"', text)

    # Remove every historical JS injector. They carry hard-coded V5.4/V5.5 versions
    # and were the cause of both duplicate versions and production 404s.
    text = re.sub(r'\s*<script[^>]+src="(?:recovery|v547_hotfix|v5100_hotfix)\.js[^\"]*"[^>]*></script>', '', text, flags=re.I)
    text = re.sub(r'\s*<script[^>]+src="app\.js[^\"]*"[^>]*></script>', '', text, flags=re.I)
    text = re.sub(r'\s*<script>if\(\'serviceWorker\'.*?</script>', '', text, flags=re.S | re.I)

    scripts = (
        f'\n  <script src="/static/app.js?v={VERSION}"></script>'
        f'\n  <script src="/static/v5101_hotfix.js?v={VERSION}"></script>'
        f'\n  <script>if(\'serviceWorker\' in navigator){{navigator.serviceWorker.register(\'/sw.js?v={VERSION}\',{{updateViaCache:\'none\'}}).then(r=>r.update()).catch(()=>{{}});}}</script>\n'
    )
    text = text.replace("</body>", scripts + "</body>")

    return HTMLResponse(
        text,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-App-Version": VERSION,
        },
    )


@app.middleware("http")
async def v5101_runtime_header(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp
