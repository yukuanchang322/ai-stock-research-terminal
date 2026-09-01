# V5.19.3 — stale-while-revalidate cold-start recovery

- Preserve the last complete stock report when official history or financial
  revisions advance. Revision gates keep stale reports out of the normal fresh
  path, while the timeout path can still return the last good report explicitly.
- Coalesce forced and normal retries into one in-flight build per ticker.
- Treat TWStock MCP as an optional cross-check that cannot evict the core report.
- Persist up to six successful reports on the device. During Render cold start,
  show the matching ticker's prior report with an updating notice and retry the
  API automatically; never leave another ticker's report visible.
- Align server, HTML assets, and retired service-worker marker to V5.19.3.
