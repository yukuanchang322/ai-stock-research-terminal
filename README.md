# AI Stock Research Terminal V4 Cloud Mobile

V4 把 V3 升級為手機優先、可雲端部署的台股研究平台。

## V4 主要功能

- iPhone / Android / iPad / Mac / PC responsive UI
- PWA：Safari / 支援瀏覽器可加入主畫面
- 手機底部固定：查股 / 強制更新 / PDF / 分享
- URL 可帶 `?ticker=2330`，方便分享同一檔研究頁
- 股票 API 與 PDF 不做 Service Worker 離線快取，避免把舊資料誤當最新
- 10 分鐘 server cache；「強制更新」可跳過快取
- PDF 產生預設再次 refresh 最新可取得資料
- FinMind 結構化資料 + 合法匯入法人研究資料
- Render Blueprint、Railway/Procfile、Dockerfile 三種部署方式
- `/health` 健康檢查

## 本機啟動

macOS 可雙擊 `run.command`，或：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```

瀏覽 `http://127.0.0.1:8000`。

## 環境變數

複製 `.env.example` 後設定：

- `FINMIND_TOKEN`：建議設定，以取得較穩定 API 額度
- `CACHE_TTL_SECONDS`：預設 600 秒

注意：本程式不會把 `.env` 放進版本控制。

## Render 部署

專案已附 `render.yaml`。將此資料夾放入 Git repository 後，在 Render 建立 Blueprint / Web Service，設定 `FINMIND_TOKEN` 即可。健康檢查路徑為 `/health`。

## Railway 部署

專案已附 `Procfile` 和 `Dockerfile`。可從 GitHub repository 部署，並在 Railway Variables 設定 `FINMIND_TOKEN`。平台會提供公開 HTTPS 網址。

## Docker

```bash
docker build -t ai-stock-v4 .
docker run --rm -p 8000:8000 -e FINMIND_TOKEN=YOUR_TOKEN ai-stock-v4
```

## iPhone 安裝方式

1. 用 Safari 開啟部署後的 HTTPS 網址。
2. 點 Safari 分享按鈕。
3. 選「加入主畫面」。
4. 之後從主畫面開啟，即為 standalone PWA 體驗。

PWA 的程式外殼可以被瀏覽器快取，但 `/api/` 股票資料、強制更新與 PDF 都會向伺服器請求，不會用 Service Worker 的舊市場資料代替最新資料。

## 法人研究資料

`data/research_reports.json` 僅供合法授權或使用者自行取得、允許使用的法人研究 metadata / 摘要。不要放入未取得授權的全文。

CSV 欄位範例見 `data/research_reports_template.csv`。

## 資料可信度原則

- 缺資料顯示缺失，不用 AI 猜值補齊。
- 模型合理價與法人目標價分開。
- 每份研究顯示各資料集 `as_of` 日期。
- PDF 保存產生時間、資料新鮮度與估值基礎。
- 研究內容不是個人化投資建議或收益保證。
