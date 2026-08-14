# V5.2.4a Hotfix — KY official Q2 fallback

This hotfix adds an official MOPS material-information fallback for board-approved quarterly financial reports.

## Why
For foreign/KY issuers such as 3661, the company may have approved and disclosed a new quarter while the structured EPS/XBRL feed still lags.

## New source order
1. TWSE/TPEx structured OpenAPI
2. MOPS official CSV
3. **TWSE/MOPS board-approved financial-report material disclosure**
4. Company IR reviewed/audited PDF
5. FinMind only as stale fallback

The material-disclosure parser only accepts explicit quarterly financial-report approvals and extracts YTD revenue, gross profit, operating income, net income and basic EPS from the official disclosure text. No inferred values are fabricated.
