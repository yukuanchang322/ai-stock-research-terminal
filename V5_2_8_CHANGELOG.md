# V5.2.8 — Official EPS Resolver

## Root cause fixed
MOPS company IFRS endpoint `t164sb01` expects ROC year in `SYEAR`. V5.2.7 sent Gregorian years (e.g. 2026), so historical Q1/Q3 lookups silently failed. Current Q2 YTD could be sourced from aggregate CSV, but predecessor YTD was missing, leaving standalone EPS blank.

## Changes
- Convert Gregorian year to ROC year for company-specific MOPS IFRS lookups.
- Add per-period EPS lookup diagnostics.
- Expose `/api/diagnostics/eps/{ticker}`.
- Preserve official-only derivation rule: no mixing stale FinMind predecessor with an official current quarter.
- TTM remains actual-quarter-only; no annualization guess.
