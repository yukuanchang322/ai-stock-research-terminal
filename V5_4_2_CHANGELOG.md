# V5.4.2 — Data API / Price Fallback Hotfix

Root cause clarified from Render logs:
- `/api/stock/2330` route existed, but returned HTTP 404 because `price` was null.
- The UI therefore showed a misleading "stock not found / provider unavailable" error.

Fixes:
1. FinMind daily price remains primary.
2. If unavailable, official TWSE `STOCK_DAY` monthly API backfills ~13 months of OHLC.
3. If both historical feeds fail, TWStock MCP `close` can rescue the latest price without pretending technical history exists.
4. Transient provider failure now returns HTTP 503 with diagnostic errors instead of 404.
5. Price Source status shows the actual provider used.
6. Fixed PWA icon paths from `/static/icons/...` to `/static/...`.
7. PDF filenames updated to V5.4.2.
