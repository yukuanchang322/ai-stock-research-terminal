# V5.19.0 Research Quality and Runtime Efficiency

- Adds an auditable investor decision brief with scenario weights, probability-weighted value, variant perception, and explicit conclusion-strength gates.
- Caps confidence when the latest financial period is not officially verified or material source conflicts remain.
- Coalesces identical in-flight stock builds to prevent duplicate provider fan-out during cold starts.
- Exposes stock-build metrics in health and per-request diagnostics.
- Updates the professional PDF with conclusion quality, downgrade reasons, and weighted scenario valuation.
- Keeps the single `server:app` runtime and retired service-worker architecture.
