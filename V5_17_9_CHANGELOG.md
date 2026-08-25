# V5.17.9

- Create the PDF history-cycle revision when official background tasks are
  scheduled, before their coroutines transition from queued to running.
- Preserve that revision across queued, running, incremental writes, and
  provider completion.
- Clear the cycle only after the last provider task exits, causing exactly one
  final PDF invalidation.
