# V5.2.4 — 3661 實際官方財報診斷版

- 新增 `/api/diagnostics/financial/{ticker}`。
- TWSE/TPEx OpenAPI：記錄 HTTP status、資料筆數、3661 是否命中、官方季度與解析欄位。
- MOPS CSV：記錄 HTTP status、編碼、表頭、資料筆數、ticker 命中與解析結果。
- Company IR：記錄頁面是否顯示應有年度/季度、PDF link 數量、候選 PDF HTTP/PDF 文字/期間驗證/解析結果。
- 診斷結果列出最終 `fetch_official_income_statement()` 選擇來源，便於比較「上游有資料」與「選擇器沒選到」兩種故障。
- 財報未通過 freshness gate 時，手機基本面直接提供診斷連結。
- 不會因診斷而放寬 EPS 驗證；未知資料仍不得進入估值。
