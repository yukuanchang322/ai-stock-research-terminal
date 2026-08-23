# V5.4.3 — 502 Provider Isolation + Cache Bust

- Any single external provider failure no longer aborts the whole stock report.
- Public research, company events, and TWStock MCP are isolated independently.
- API returns clean JSON for 5xx errors; raw HTML/CSS/font responses are never surfaced to the UI.
- FinMind helper rejects unexpected HTML/CSS payloads.
- Added `/api/diagnostics/provider-health/{ticker}`.
- Service Worker cache version bumped to V5.4.3.
- `app.js` and `styles.css` use `?v=5.4.3` cache-busting.
- Frontend uses `cache: no-store` for runtime fetches where applicable.
