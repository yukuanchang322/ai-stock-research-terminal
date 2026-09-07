# V5.19.4 — Mobile snapshot quality repair

## Root cause

V5.19.3 preserved a same-ticker browser snapshot whenever the report contained a price. During a first Render build, a price-only or partially populated report could therefore replace a more complete local report and remain visible after scrolling away from the update notice. The server already exposed `background_revision_pending`, but the browser retry loop only handled `stale` and HTTP 503 warming responses.

## Changes

- Version browser snapshots with schema v2 and remove legacy v1 partial snapshots.
- Score revenue, official financials, technical history and institutional flow before persisting a report.
- Never replace a complete or higher-quality snapshot with a lower-quality response.
- Retry reports marked `background_revision_pending` until the official background build finishes.
- Show snapshot time and completeness in a fixed mobile disclosure above the action dock.

## Validation

- 139 Python regression tests and JavaScript syntax checks passed.
- 2330, 2454, 3661 and 3665 returned official 2026 Q2 financials, 24 revenue months and 252 technical rows.
- iPhone 390×844 retained the 2330 report during a cold restart, showed a fixed non-overlapping status, then replaced it with official data automatically.
