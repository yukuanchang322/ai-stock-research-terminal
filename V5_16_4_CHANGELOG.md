# V5.16.4 — Idempotent evidence rendering

- Give the EPS ledger and Evidence matrix a single owned DOM region.
- Replace that region on every report render instead of appending new panels.
- Rebuild mobile disclosure toggles after replacement, preventing duplicates
  during background history recovery, forced refreshes, and ticker changes.
- Bump static asset versions so iPhone Safari receives the corrected client.
