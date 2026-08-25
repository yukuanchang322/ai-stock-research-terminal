# V5.17.6

- Stop forcing a full stock refresh when the PDF button is pressed.
- Show an immediate busy state and a readable error on both PDF buttons.
- Open the generated PDF inline for iPhone preview and sharing.
- Catch HTML renderer failures and generate a valid Traditional Chinese
  ReportLab fallback PDF from the same cached research result.
- Return explicit 400/503 errors instead of an unhandled HTTP 500.
