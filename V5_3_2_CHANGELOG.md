# V5.3.2 — Evidence Quality & Conflict Engine

- Official/direct quarter EPS is classified as `fact`; arithmetic quarter/TTM EPS remains `derived_fact`.
- Analyst target prices and forward EPS are classified as `estimate`.
- Conflict detection now requires the same metric + period + definition + unit and independent factual sources.
- Estimates/model outputs are excluded from source-conflict penalties.
- Same-source estimate changes are tracked as `estimate_revisions`.
- Evidence Matrix now separates Estimate, true conflicts, and estimate revisions.
- Evidence schema upgraded to 1.1.
