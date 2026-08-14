# V5.2.6 — Official Financial Mapping Fix

- Adds a final official-financial reconciliation step before `/api/stock/{ticker}` is assembled.
- Re-checks official MOPS CSV independently and forces the newest official fiscal period into the main payload.
- Never merges financial fields across fiscal quarters.
- Maps official `ytd_eps`, revenue, gross profit, operating income, and net income into the Fundamentals card and PDF.
- Keeps quarter EPS distinct from YTD EPS; quarter EPS is derived only when a reliable prior YTD value exists.
- Keeps TTM EPS separate and does not annualize a single quarter.
- Adds mapping metadata (`mapping_reconciled`, `mapping_sources`) for diagnostics.
