"""V5.10.2 Cache Breaker + Runtime Unification

Purpose:
- keep the V5.10.0/5.10.1 data pipeline unchanged,
- forcibly retire stale Safari/PWA service workers that still serve V5.9.3/V5.9.7 HTML,
- serve one authoritative V5.10.2 shell and health version,
- keep a legacy /app.js bridge so cached old HTML can self-heal and reload the current shell.
"""
from __future__ import annotations

import re
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response

import run_v5100, server

VERSION = "5.10.2"
server.app.version = VERSION
app = server.app

# This entrypoint owns all shell/runtime endpoints. Remove inherited copies first.
for r in list(app.routes):
    if getattr(r, "path", None) in (
        "/api/stock/{ticker}", "/", "/health", "/sw.js", "/app.js"
    ):
        try:
            app.routes.remove(r)
        except ValueError:
            pass


@app.get("/api/stock/{ticker}")
async def api_stock_v5102(ticker: str, refresh: int = 0):
    d = await run_v5100.build_stock_v5100(ticker.strip(), force_refresh=bool(refresh))
    d["version"] = VERSION
    return JSONResponse(
        d,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "X-AI-Stock-Version": VERSION,
        },
    )


@app.get("/api/v5102/stock/{ticker}")
async def api_v5102(ticker: str, refresh: int = 0):
    return await api_stock_v5102(ticker, refresh)


@app.get("/health")
async def health_v5102():
    return JSONResponse(
        {"status": "ok", "version": VERSION, "mode": "cache-breaker-runtime-unification"},
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-AI-Stock-Version": VERSION,
        },
    )


@app.get("/sw.js")
async def service_worker_v5102():
    return FileResponse(
        server.ROOT / "sw.js",
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Service-Worker-Allowed": "/",
            "X-AI-Stock-Version": VERSION,
        },
    )


@app.get("/app.js")
async def legacy_app_bridge_v5102():
    """Old cached V5.9.x HTML still requests /app.js.

    Do not run the old app. Unregister every legacy service worker/cache once,
    then navigate to a cache-busted URL. The new shell uses /static/app.js,
    so this route is only a recovery bridge for stale pages.
    """
    js = f"""
(async()=>{{
  const V='{VERSION}', K='ai-stock-cache-break-'+V;
  try {{
    if (!sessionStorage.getItem(K)) {{
      sessionStorage.setItem(K,'1');
      if ('serviceWorker' in navigator) {{
        const regs=await navigator.serviceWorker.getRegistrations();
        await Promise.all(regs.map(r=>r.unregister().catch(()=>false)));
      }}
      if ('caches' in window) {{
        const keys=await caches.keys();
        await Promise.all(keys.map(k=>caches.delete(k).catch(()=>false)));
      }}
    }}
  }} catch(e) {{}}
  const u=new URL(location.href);
  u.searchParams.set('v',V);
  u.searchParams.set('cb',Date.now().toString());
  location.replace(u.toString());
}})();
"""
    return Response(
        js,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-AI-Stock-Version": VERSION,
        },
    )


@app.get("/", response_class=HTMLResponse)
async def root_v5102():
    text = (server.ROOT / "index.html").read_text(encoding="utf-8")

    # Remove all hard-coded/legacy version badges and replace visible/document version.
    text = re.sub(r'<span[^>]*data-app-version[^>]*>.*?</span>', '', text, flags=re.S | re.I)
    text = re.sub(r'AI Stock Research Terminal V5\.\d+(?:\.\d+)?', f'AI Stock Research Terminal V{VERSION}', text)

    # Always point the current shell at /static assets, which are mounted by server.py.
    text = re.sub(r'href="styles\.css(?:\?v=[^"]+)?"', f'href="/static/styles.css?v={VERSION}"', text)
    text = re.sub(r'href="/static/manifest\.webmanifest(?:\?v=[^"]+)?"', f'href="/static/manifest.webmanifest?v={VERSION}"', text)

    # Strip every historical injector and any old app.js registration from source index.html.
    text = re.sub(r'\s*<script[^>]+src="(?:recovery|v547_hotfix|v5100_hotfix|v5101_hotfix|v5102_hotfix)\.js[^\"]*"[^>]*></script>', '', text, flags=re.I)
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
            "X-AI-Stock-Version": VERSION,
        },
    )


@app.middleware("http")
async def v5102_runtime_header(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-AI-Stock-Version"] = VERSION
    return resp
