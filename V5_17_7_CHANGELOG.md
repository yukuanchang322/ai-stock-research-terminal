# V5.17.7

- Prefer the reliable embedded-font ReportLab renderer on Render instead of
  spending time on a known-failing WeasyPrint attempt.
- Cache generated PDFs per ticker for up to the stock cache TTL.
- Invalidate cached PDFs when official history, official financial data, or the
  optional MCP revision changes.
- Serialize concurrent PDF requests per ticker so repeated taps do not trigger
  duplicate report builds.
- Expose `X-PDF-Cache: HIT|MISS` and the selected renderer for production
  diagnostics.
