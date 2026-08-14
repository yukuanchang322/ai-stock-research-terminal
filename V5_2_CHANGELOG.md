# V5.2 Data Integrity Changelog

## Priority hierarchy
1. TWSE/MOPS/TPEx official public data
2. Structured market APIs (FinMind)
3. Public web/news excerpts

## EPS definitions
- Quarter EPS: single fiscal quarter only.
- YTD EPS: cumulative fiscal-year EPS through the latest quarter.
- TTM EPS: sum of the latest four reliably derived quarterly EPS values.
- Forward EPS: annual analyst forecast with an explicit forecast year.

The system will not substitute one definition for another without labeling it. In particular, single-quarter EPS × 4 is prohibited as a formal forward estimate.

## Freshness
When the official financial period is newer than the structured API period, the structured financial API is labeled STALE and cannot overwrite official values.
