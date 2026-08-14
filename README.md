# AI Stock Research Terminal V5

台股手機雲端研究平台。輸入股票代號後，整合結構化市場資料、公開網路法人研究引用、公司事件雷達、透明估值與投資人 PDF。

## V5 核心升級

- 公開網路法人研究聚合：搜尋公開可取得的法人/券商研究引用，辨識券商、評等、目標價、Forward EPS、日期、發布者與來源連結。
- 法人共識引擎：目標價中位數、平均、最高、最低、Buy/Neutral/Sell 計數、同機構目標價修正與 EPS 修正。
- 去重與可信度：同標題去重；辨識到券商、目標價、評等與 EPS 時提高資料可信度。
- 公司事件雷達：法說、財報、營收、展望、重大訊息、接單、產能與客戶相關公開引用。
- 模型估值與法人共識分流：Bear/Base/Bull 模型合理價不與法人喊價混為一談。
- PDF V5：法人研究與公司事件會進入投資人 PDF，並保留資料來源與更新時間。
- 手機/PWA：iPhone Safari 可加入主畫面；市場 API 與 PDF 不做離線快取，避免舊資料誤標最新。

## 資料來源

1. FinMind API：股價、PER/PBR、三大法人、融資融券、月營收、財務報表。
2. 公開網路研究：Google News RSS 搜尋公開新聞/研究引用。僅保存標題、摘要、數值、發布者、日期與來源連結，不重製付費研究全文。
3. `data/research_reports.json`：仍支援自行匯入研究 metadata，會與公開網路研究合併。

> 公開網路研究不是完整券商資料庫，可能有缺漏、延遲、媒體轉述或解析失敗。系統會保留來源與可信度，不應把缺失資料補成假共識。

## Render 部署

現有 Render Web Service 不需要重建。把 V5 所有檔案覆蓋/上傳到 GitHub repo 根目錄並 Commit，Render 若已啟用 Auto-Deploy 會自動重部署；否則在 Render 選：

`Manual Deploy → Deploy latest commit`

Build Command:

```bash
pip install -r requirements.txt
```

Start Command:

```bash
uvicorn server:app --host 0.0.0.0 --port $PORT
```

Health Check:

`/health` 應回傳 `version: 5.0.0`。

## 環境變數

- `FINMIND_TOKEN`：選填；建議設定以提高 API 額度。
- `CACHE_TTL_SECONDS`：預設 600 秒。

## 重要揭露

本系統為研究與資訊整理工具，不構成個人化投資建議、招攬或收益保證。公開網路法人資訊可能是媒體轉述而非原始研究全文，應以來源連結、發布日期與公司正式公告交叉驗證。
