# V5.19.5 — TPEx official financial integrity

- Parse TPEx detail-feed `Season`/`Quarter` fields as the fiscal quarter.
- Probe current TWSE/TPEx EPS and general-industry statements first, then limit secondary schemas to the detected market.
- Keep slow company-specific MOPS and IR sources as fallbacks instead of blocking every cold request.
- Cache verified official period snapshots by ticker/year/quarter for later EPS derivation.
- Remove resolved `OfficialFinancial` timeout noise after an official period is verified.
- Treat a TWSE no-row result as normal routing—not an error—after TPEx price history succeeds.
- Preserve the existing mobile layout and stale-report protection introduced in V5.19.4.

Validation matrix: 2330, 2454, 2317, 3008, 3661, 3665, 6488, 6223.
