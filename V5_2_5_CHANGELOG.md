# V5.2.5 — 通用官方財報修正版

- 修正 2330、3661 都可能拿不到最新財報的系統性問題。
- 新增公司別 MOPS IFRS 報表直查，避免只依賴批次型彙總 OpenAPI。
- 自動嘗試合併(C)、個體/個別(B/A)報表。
- 解析本期累計/YTD 欄位，避免把單季與累計 EPS 混用。
- 台積電增加官方季度結果 fallback。
- `/api/diagnostics/financial/{ticker}` 新增 `company_mops` 診斷。
