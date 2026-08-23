# V5.4.0 — Research Pipeline Re-architecture

- Provider layer：參考 TWStockMCPServer，把行情、基本面、籌碼、重大訊息視為可替換資料 Provider。
- Evidence first：參考 stock-analysis，研究結論必須下游於 Evidence。
- Fact / Derived Fact / Estimate 分層；缺值不視為 0。
- Data Boundary Grade：只有會改變核心結論的缺失/衝突才列為 material boundary。
- 新增研究結論、成立條件、失效條件。
- 報告流程：Question → Evidence → Validation → Analysis → View → Conditions → Valuation → PDF。
