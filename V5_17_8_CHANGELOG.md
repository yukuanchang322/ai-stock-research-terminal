# V5.17.8

- Hold one stable PDF data revision while official history backfill jobs are
  running, instead of invalidating the PDF after every incremental row batch.
- Invalidate once when the background job completes so the next PDF includes
  the completed official history.
- Preserve the V5.17.7 ReportLab fast renderer, per-ticker lock, and diagnostic
  cache headers.
