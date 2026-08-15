# V5.2.14 Historical EPS Backfill

- Adds a company-official historical EPS evidence backfill layer.
- Expands TSMC official quarterly evidence registry for 2024 Q4 through 2026 Q2; EPS values remain parsed live from official pages and are not hard-coded.
- Uses multiple official-language URLs as resilient evidence candidates.
- Reuses Alchip company IR reviewed/audited PDF resolver for historical quarters.
- Adds `historical_backfill` resolved/missing period diagnostics to `eps_stack`.
- Keeps the strict rule that third-party EPS cannot be used to derive an official quarter.
