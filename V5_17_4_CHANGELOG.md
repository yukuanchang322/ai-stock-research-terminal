# V5.17.4

## Root cause

The background official-financial job rebuilt standalone-quarter margins by
fetching the preceding cumulative quarter. On a Render free instance that
extra official-source request could consume the full 30-second timeout even
after the current official statement was already available.

## Fix

- Publish current official cumulative (YTD/FY) margins immediately.
- Prefer official direct standalone-quarter margins when the source provides them.
- Skip preceding-quarter network resolution in the background warm path.
- Keep the existing explicit warning when only cumulative margins are available;
  cumulative values are never relabelled as standalone-quarter values.

## Validation

- Unit coverage proves the background fast path performs no predecessor fetch.
- Unit coverage proves direct official quarter margins take precedence without a
  predecessor fetch.
- Existing full-suite, syntax, asset-version and diff checks remain required
  before the PR is opened.
