# V5.2.9 — EPS 原始資料追蹤版

- 新增 `/api/diagnostics/eps-raw/{ticker}?year=YYYY&quarter=Q`。
- 顯示 MOPS 歷史財報查詢實際參數：`CO_ID / SYEAR / SSEASON / REPORT_ID`。
- 顯示 HTTP status、最終 URL、content-type、bytes、encoding。
- 顯示解碼後 HTML 小型 preview、是否命中「查無資料」。
- 顯示 `pandas.read_html` 讀到的 table count。
- 顯示 EPS 候選列、每個候選數字與其 column header。
- 顯示 parser 最後選中的 YTD column、YTD EPS、report_id。
- `/api/diagnostics/eps/{ticker}` 新增最近 5 個季度 raw trace 連結。
- 不更動主畫面既有財報、營收、毛利率、營益率與 PER/PBR 邏輯。
