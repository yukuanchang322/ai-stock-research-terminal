"""V5.4.5 deployment entrypoint.
Keeps the V5.4.4 provider-isolation backend intact while exposing the deployed
application version consistently during the Data Recovery release.
"""
from fastapi.responses import JSONResponse
import server

app = server.app
app.version = "5.4.5"

async def health_v545():
    return {
        "status": "ok",
        "version": "5.4.5",
        "mode": "cloud-mobile-data-recovery",
        "finmind_token": bool(server.FINMIND_TOKEN),
        "cache_ttl_seconds": server.CACHE_TTL,
        "pwa": True,
        "data_recovery": True,
    }

# Replace the existing /health endpoint without touching the proven V5.4.4
# provider-isolation implementation in server.py.
for route in app.routes:
    if getattr(route, "path", None) == "/health" and "GET" in (getattr(route, "methods", set()) or set()):
        route.endpoint = health_v545
        break
