# V5.19.1 — bounded stock-report requests

- Add a 22-second default request budget (clamped to 8–26 seconds) to
  `/api/stock/{ticker}`, keeping the edge proxy from turning a slow provider
  cycle into a 502.
- Preserve the shared shielded build task after a request timeout so a retry
  can coalesce onto the same provider fan-out instead of starting another one.
- Serve an explicitly labelled last-good report when a refresh times out, or a
  `503` `warming` response with `Retry-After: 5` when no report exists yet.
- Add regression tests for both timeout paths and align the public version to
  V5.19.1. No runtime wrapper module is introduced.
