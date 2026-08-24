# V5.16.1

- Add an isolated Yahoo Finance chart adapter compatible with yfinance ticker symbols (`.TW` / `.TWO`) for OHLC fallback only.
- Keep TWSE, TPEx, and MOPS authoritative; official same-date rows replace third-party rows without deleting history.
- Preserve the last successful price history for six hours when all upstream price providers fail transiently.
- Expose FinMind, Yahoo price fallback, and TWStock MCP roles in health and provider diagnostics.
- Keep Taiwan institutional and margin data on official/FinMind sources; Yahoo holder data is not treated as daily three-institution flow.
