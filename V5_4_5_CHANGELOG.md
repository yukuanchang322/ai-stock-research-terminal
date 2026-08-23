# V5.4.5 — Data Recovery

- Adds `recovery.js` as a transparent client-side recovery layer for `/api/stock/{ticker}`.
- Live API remains authoritative. Only successful, structurally valid research payloads are saved on-device.
- Primary storage uses IndexedDB; localStorage is used as a fallback.
- Recovery activates only when the live request fails, returns non-JSON/invalid content, or produces an unusable research payload.
- Recovered reports are explicitly marked `Data Recovery · 本機備份` with original report time and backup time.
- Recovered responses include `X-AI-Stock-Recovery: 1` and a `recovery` metadata object.
- Recovery payloads are never re-saved as fresh data.
- Service Worker cache bumped to `ai-stock-v5.4.5` and includes `recovery.js` in the shell.
- `/api/*` responses remain excluded from Service Worker caching so stale market data cannot silently masquerade as live data.
- App assets use v5.4.5 cache-busting and the UI explains that restored data is not real-time.

## Validation policy

1. A valid live report must contain `ticker`, non-null `price`, and `generated_at`.
2. Successful live reports replace the device recovery copy.
3. Failed live reports do not overwrite the last known good copy.
4. If no recovery copy exists, the original API/network error is preserved.
5. Recovery is device-local and is removed when the user clears browser/site storage.
