# V5.2.13 — EPS Evidence Ledger

- 新增 `eps_stack.evidence_ledger`：逐季記錄官方 EPS 證據、來源 URL、口徑、信心度、缺失原因。
- 單季 EPS 與 TTM 只從 Ledger 中可用的官方證據推導。
- 前端基本面新增 EPS Evidence Ledger，可直接看到哪一季有資料、哪一季缺資料。
- 2330 增加官方新聞穩定 URL 作為 2026 Q1/Q2 證據種子；數值仍從官方頁即時解析，不硬編 EPS。
- `/health` 版本更新為 5.2.13，mode=`cloud-mobile-eps-evidence-ledger`。
