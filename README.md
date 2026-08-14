# AI Stock Research Terminal V5.2.5

V5.2.5 fixes the systemic financial-freshness problem affecting both ordinary listed companies such as 2330 and KY/foreign issuers such as 3661.

The primary financial source is now a company-specific MOPS IFRS report query for the expected quarter (`server-java/t164sb01`), with consolidated/individual report fallbacks. Aggregate TWSE/TPEx OpenAPI and MOPS CSV remain secondary sources because their batch refresh can temporarily omit companies that have already reported.

Key changes:
- Company-specific MOPS IFRS fetch for every ticker, not just 3661.
- Robust cumulative/YTD column detection for Q1-Q4.
- TSMC official quarterly-results fallback when aggregate MOPS feeds lag.
- Accept verified official single-quarter EPS when YTD EPS is temporarily unavailable.
- Same-quarter fields may merge; cross-quarter fields never merge.
- Diagnostics now expose `company_mops` results.

Deploy by replacing the files in the existing GitHub repository and let Render auto-deploy. Verify `/health` returns version `5.2.5`.
