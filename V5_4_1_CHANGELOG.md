# V5.4.1 — TWStock MCP Cross-Validation

- TWStock MCP upgraded from shadow mode to live secondary provider.
- Default `TWSTOCK_MCP_ENABLED=1`.
- Runtime MCP tool discovery via `tools/list`.
- Schema-aware automatic argument construction for stock tools.
- Attempts quote, institutional, margin, monthly revenue, valuation and financial/EPS tools.
- MCP output is normalized into Evidence Records.
- MCP never silently overwrites TWSE/MOPS/company official data.
- Same-period comparable discrepancies feed Evidence Conflict.
- Provider failures are non-blocking; official/FinMind pipeline continues.
- New `/api/diagnostics/mcp/{ticker}` endpoint and UI status panel.
