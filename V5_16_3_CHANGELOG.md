# V5.16.3 — Canonical runtime cleanup

- Removed 49 retired `run_v*.py` wrapper runtimes; production continues to use only `server.py`.
- Removed obsolete browser hotfix/recovery scripts, historical release ZIPs, tracked bytecode, and an unused 6.8 MB font.
- Added ignore rules and architecture tests so retired runtime artifacts cannot silently return.
- Preserved production data, the official EPS registry, tests, report assets, and all release notes.
- This cleanup reduces deployment contents and old-runtime ambiguity; stock-query latency remains governed mainly by upstream provider response times.
