# V5.2.15 — Official EPS Registry + Auto Backfill

- 新增 `data/official_eps_registry.json`：公司官方歷史季度 EPS 證據帳本。
- Registry 優先於即時歷史爬取，避免 Render→MOPS 歷史頁的反爬/安全限制。
- 每筆資料保留 ticker、季度、quarter EPS、可選 YTD EPS、公司官方來源、URL、verified。
- 主 EPS Engine 可直接用 Registry 的前季單季值與當期官方 YTD/單季值。
- 2330 已加入 2025Q2-Q4、2026Q1-Q2 官方季度 EPS 證據。
- 3661 已加入 2025Q2-Q4、2026Q1 公司官方財報/新聞 EPS 證據。
- 新增 `/api/diagnostics/eps-registry/{ticker}`。
- 第三方 EPS 永遠不會自動寫入 Registry。
