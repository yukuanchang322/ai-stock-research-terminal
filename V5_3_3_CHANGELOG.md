# V5.3.3 — Flow Horizons & Professional Technical Suite

## 籌碼面
- 外資 / 投信 / 自營商：1日、5日、20日淨買賣超。
- 融資餘額：1日、5日、20日百分比變化。
- 手機以矩陣呈現三個時間尺度，中期20日仍保留橫條比較。

## 技術面
- 近一年（最多252個交易日）日K OHLC。
- MA20 / MA60 疊加在K線主圖；MA60作為中期趨勢核心。
- KD（9日 RSV，1/3 smoothing）、MACD(12,26,9)、RSI14。
- KD / MACD / RSI 置於K線下方獨立面板。
- API series 逐日輸出 OHLC、MA20、MA60、K、D、MACD、Signal、Histogram、RSI14。
- 技術圖表會隨網頁列印進PDF。
