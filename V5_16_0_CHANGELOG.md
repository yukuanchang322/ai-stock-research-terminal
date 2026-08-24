# V5.16.0

- Enable FinMind's anonymous fallback quota when no token is configured on Render.
- Fetch official company identity and OHLC discovery concurrently with a bounded timeout.
- Normalize FinMind `twse` / `tpex` market codes to the UI's `上市` / `上櫃` labels.
- Merge official latest institutional and margin rows into fallback history by date instead of deleting the 5-day and 20-day series.
- Keep official same-day values authoritative while preserving older fallback observations.

Validation: 6488, 3665, and 2330 each returned 252 price sessions, 24 monthly-revenue observations, 83 institutional dates, and 60 margin dates in live-provider testing on 2026-08-24.
