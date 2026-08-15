# V5.2.10 — MOPS Endpoint + EPS Parser 修正版

- 歷史財報不再只依賴已回 404 的 `server-java/t164sb01`。
- 依序嘗試 `/mops/web/t164sb01`、`/mops/web/ajax_t164sb01`，舊 `server-java` 僅保留診斷。
- EPS HTML parser 改為 `lxml`，完全移除 `pandas.read_html/html5lib` 的必要性。
- 歷史 MOPS 詳細頁失敗時，增加官方董事會通過財報重大訊息 fallback。
- 2330 再增加官方 TSMC quarterly/news EPS fallback，數字仍從官方頁動態解析，不寫死。
- `/api/diagnostics/eps-raw/{ticker}` 會顯示每個 endpoint/method 的 HTTP status 與 lxml EPS parser 結果。
