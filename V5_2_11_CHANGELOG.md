# V5.2.11 Multi-source EPS Resolver

- Production EPS path no longer depends on MOPS historical HTML endpoints that return security blocks on cloud hosts.
- Priority: official structured TWSE/MOPS -> company official IR/reviewed report -> official cumulative EPS difference -> leave blank.
- TSMC adapter follows the official quarterly-results page to the Earnings Release PDF and extracts actual single-quarter EPS dynamically.
- Alchip/mapped issuers reuse reviewed/audited company IR PDF parsing for historical periods.
- Third-party FinMind history is never mixed with an official current quarter to derive official EPS.
- EPS cards now show provenance labels: official direct, official cumulative-difference, or insufficient data.
- TTM is shown only when four actual quarter EPS values are available.
- Blocked MOPS raw diagnostics remain available for troubleshooting only.
