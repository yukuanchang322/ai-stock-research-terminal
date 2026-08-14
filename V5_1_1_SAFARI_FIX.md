# V5.1.1 Safari Fix

- 修正 iPhone Safari `The string did not match the expected pattern.`。
- 不再把 URL 物件直接交給 `history.replaceState`。
- API 回應先讀文字並檢查 Content-Type，再解析 JSON。
- Render/Proxy 回傳 HTML 錯誤頁時顯示可讀錯誤。
- 股票代號加入格式驗證。
- 分享網址改成 Safari-safe 字串組合。
