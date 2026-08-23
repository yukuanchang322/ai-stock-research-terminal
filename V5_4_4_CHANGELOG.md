# V5.4.4 — Provider Hard Isolation

- Any upstream 502/HTML/CSS/font response is blocked from reaching the UI.
- Added generic `_safe_http_json()` wrapper.
- FinMind calls now require valid JSON payloads.
- API errors are compacted and scrubbed of HTML/CSS/base64 leakage.
- `/api/stock/{ticker}` always returns clean JSON on failure.
- Provider health now checks price, institutional, margin, revenue, TWSE fallback and TWStock MCP separately.
- Frontend added `safeJsonFetch()` and `cleanUiError()`.
- app.js/styles.css cache-bust moved to v5.4.4.
