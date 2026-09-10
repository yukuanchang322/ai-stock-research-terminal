# V5.19.6 — Render Core-Data Budget Repair

## Root cause

A verified TPEx EPS summary could arrive quickly, but the production request
continued waiting for every detailed MOPS schema, CSV/PDF reconciliation,
official-history work, and public research.  On Render's free single worker,
the 22-second API budget expired and clients received a stale empty report.

## Repair

- Return the current official TWSE/TPEx EPS summary as soon as it is verified.
- Preserve completed official probes when a sibling detail endpoint stalls.
- Limit primary FinMind, official-market, and supplement calls to the core
  response budget.
- Keep historical EPS resolution and slow reconciliation out of the initial
  core response; missing single-quarter/TTM values stay explicitly missing.
- Reduce non-core T86 and public-research waits so they cannot hide price,
  revenue, or verified financial data.

## Integrity policy

Official YTD EPS is shown only when its fiscal period is current and verified.
The system does not invent standalone-quarter EPS, TTM EPS, margin history, or
institutional history while the corresponding official evidence is incomplete.
