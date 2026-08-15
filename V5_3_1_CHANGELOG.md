# V5.3.1 Evidence Engine EPS Takeover

- EPS Evidence Ledger now reads the Official EPS Registry before the legacy missing_official pipeline.
- Added a built-in verified registry seed so GitHub/mobile uploads cannot accidentally omit nested `data/` evidence files.
- File-based `data/official_eps_registry.json` still merges on top and can override/extend built-in records.
- 2330 validation path: 2026 Q2 YTD 49.33 + official quarterly evidence resolves Q2 27.25 and TTM 86.27.
- Evidence Ledger records registry source URLs and marks `registry_verified=true`.
- Health version: 5.3.1; mode: cloud-mobile-evidence-eps-takeover.
