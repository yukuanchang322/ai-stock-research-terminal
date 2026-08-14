# V5.2.1 Financial Freshness Gate

- Scans all official listed/OTC MOPS income-statement industry schemas instead of stopping at general industry.
- Chooses the newest official fiscal period across endpoint hits.
- Adds a conservative Taiwan filing-calendar freshness gate.
- If official latest period is not verified, the UI shows STALE and never says “latest verified”.
- YTD/TTM/price÷PER inferred EPS cannot enter core valuation until the official period gate passes.
- Explicit-year analyst Forward EPS remains independently usable, with lower confidence if accounting freshness is unverified.
- Market PER/PBR may still display, but is clearly labeled as independent from official financial-statement verification.
