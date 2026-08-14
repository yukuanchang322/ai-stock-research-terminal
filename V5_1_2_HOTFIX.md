# V5.1.2 Hotfix

修正 Render 後端 500：`build_stock()` 在 `lp` 尚未賦值前就傳入 `expectation_gap_analysis(...)`，導致 `UnboundLocalError`。

本版將 `lp = tech.get("last")` 提前到估值與預期差分析之前，並把 health / FastAPI 版本更新為 5.1.2。
