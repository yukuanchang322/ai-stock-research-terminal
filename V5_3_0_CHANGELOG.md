# V5.3.0 Multi-Source Evidence Engine

- Canonical Evidence Record schema across market, fundamental, positioning, technical and research inputs.
- Fact / Derived Fact / Analysis separation with source, period, confidence and provenance.
- Numeric conflict detection; disagreeing sources are surfaced instead of silently averaged.
- New `/api/evidence/{ticker}` and `/api/diagnostics/providers/{ticker}` endpoints.
- Provider registry inspired by TWStock MCP patterns: TWSE, TPEx, company IR, EPS Registry, optional TWStock MCP shadow adapter, FinMind fallback and public web.
- TWStock MCP probe is disabled by default and never blocks production.
- Mobile Evidence Matrix shows coverage, verified-source count and conflict count.
