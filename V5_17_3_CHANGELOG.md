# V5.17.3 Stability and Security

- Restrict `/static` to the five public frontend assets; backend source, data files, tests, and generated reports are no longer exposed.
- Disable `/api/cache/clear` unless `CACHE_ADMIN_TOKEN` is configured and require a matching bearer token.
- Restore header-based MOPS historical EPS parsing, company IFRS YTD amount parsing, official quarter/YTD margin separation, and TWSE lending normalization.
- Reconcile the expected official fiscal quarter before accepting an older financial snapshot.
- Allow provisional EPS subtraction only for exact TWSE/TPEx MOPS daily-summary sources; ambiguous sources remain official-only.
- Replace repeated full-report polling with bounded, exponential diagnostic polling and cancel superseded browser requests.
- Keep verbose MCP tool discovery in diagnostics only instead of sending it in every stock report payload.
- Use one V5.17.3 version across the HTML shell, assets, retired service worker message, API, and PDF filename/header.
- Add regression coverage for static-source exposure, protected cache clearing, and lightweight background refresh behavior.
