# V5.2.7 — Official EPS Engine

- 新增官方歷史季度 EPS 回查。
- Q1：單季 EPS = Q1 YTD EPS。
- Q2/Q3：單季 EPS = 當期 YTD - 前季 YTD。
- Q4：單季 EPS = 全年 YTD - Q3 YTD。
- TTM EPS 只加總四個實際單季 EPS；不足則顯示缺資料。
- 官方當期若缺官方前季，禁止混用 stale FinMind 資料推算。
- 2330 驗收基準：2026 Q2 YTD 49.33、Q1 YTD 22.08 → Q2 單季 27.25。
