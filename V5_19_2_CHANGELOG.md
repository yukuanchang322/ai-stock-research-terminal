# V5.19.2 — idempotent official-history cache

- Treat an identical official-history snapshot as a no-op: its revision time is
  preserved and the completed stock report cache is not evicted.
- Persist market-history progress only when the unique row count increases,
  preventing a partial count such as 56 from invalidating the report on every
  remaining lookback date.
- Keep cache invalidation for genuine row additions or value corrections, so
  newer official data still replaces an older report.
- Add regression coverage for identical and materially changed history rows;
  align the public runtime and shell version to V5.19.2.
