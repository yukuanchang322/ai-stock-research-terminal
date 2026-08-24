# V5.16.2 — Financial fallback integrity

- Official financial values remain the highest-priority source and replace provisional values on every new query.
- Missing standalone EPS may be provisionally derived from official current YTD plus a structured prior-period backup; the UI labels it with `△` and keeps it outside core valuation.
- TTM EPS may use a clearly marked mixed backup only when four consecutive quarters are available.
- Gross, operating, and net margins now pass unit/period sanity checks before display, scoring, or narrative use. Implausible values are blocked instead of shown.
- The iPhone financial cards explain official, official-derived, provisional, and missing states.
