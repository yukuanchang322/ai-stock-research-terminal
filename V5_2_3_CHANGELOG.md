# V5.2.3 — KY / 外國發行人官方財報 fallback

## 目的
修正 3661 世芯-KY 等外國/KY 發行人已公布新季度財報，但 OpenAPI JSON 仍抓不到或停留舊季度的情況。

## 新增
- MOPS 官方 CSV 直連 fallback：`t187ap14_L.csv`、上市/公發綜合損益表 CSV。
- KY/外國發行人公司 IR 財報頁 fallback；目前內建 3661 世芯-KY IR 財報頁。
- IR PDF 只有在文件明確寫出應有季度（例如 Six Months Ended June 30, 2026）時才採用。
- PDF 解析 Q2/Q3 時，區分「單季」與「YTD」四欄表格，核心財報使用 current YTD 欄位。
- 同季度來源仍可互補欄位，但禁止跨季度合併。
- `/health` 更新為 5.2.3 / `cloud-mobile-ky-official-fallback`。

## 資料優先順序
1. TWSE / TPEx OpenAPI
2. MOPS 官方 CSV
3. 公司 IR 審閱/查核財報 PDF（已登錄公司）
4. FinMind 僅作結構化輔助，不能單獨通過最新季度驗證

## 驗證原則
- 官方季度未達 freshness gate：EPS/TTM/反推估值不進核心估值。
- IR PDF 期間不明確：不採用。
- EPS 解析不明確：顯示缺值，不猜值。
