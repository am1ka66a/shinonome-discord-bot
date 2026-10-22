# Windows 本機運行

專案已建立 Python 3.11 虛擬環境 `.venv`，套件依 `requirements.txt` 安裝。
請從這份最新專案運行，不要啟動另一份舊版 Cursor 專案。

## 設定

1. 將 `.env.example` 複製為 `.env`。已有 `.env` 時不要覆蓋。
2. 只在電腦上的 `.env` 填寫 Discord Bot Token 與 MySQL 設定，不要貼到聊天或 GitHub。
3. 若沿用 Railway 資料庫，填 MySQL 的公開 TCP Proxy 主機與對外連接埠。
   `*.railway.internal` 只能在 Railway 內網使用，本機不能用。
4. 沿用相同機器人及資料庫前，先停止 Railway 的 **Bot 服務**，保留 **MySQL 服務**。
   請勿讓兩份 Bot 同時運行；新版會在啟動時處理中斷遊戲的結算與退款。

使用獨立測試 Bot 時，需搭配獨立資料庫，不能只換 Token 然後共用正式資料庫。

## 檢查與啟動

在專案目錄開啟命令提示字元：

```cmd
run-local.cmd --check
run-local.cmd --check-db
run-local.cmd
```

`--check` 不登入 Discord、不連接資料庫；`--check-db` 只登入 MySQL 執行 `SELECT 1`，
不初始化資料表。實際啟動後才會登入 Discord、同步 Slash 指令及初始化資料庫。

也可以直接雙擊 `run-local.cmd`。請保持命令視窗開啟，電腦需持續開機、連網且不休眠。
由 Codex 在背景啟動的 Bot 要套用修改時，可雙擊 `restart-local.cmd`，先停止背景程序，
再於命令視窗啟動最新版。
若已手動開啟 Bot 命令視窗，先在舊視窗按 Ctrl+C，再重新啟動；舊版本不會自動載入修改。
按 Ctrl+C 停止。再次開啟同一專案時，啟動工具會阻擋重複本機程序。

若使用原資料庫，既有餘額與紀錄會保留；改用新資料庫則是另一套資料。

若由 Codex 在背景啟動，日誌放在 `logs/local.stderr.log`，程序資訊放在
`.local-state/process.json`。雙擊 `stop-local.cmd` 可停止該背景程序；工具會先核對
程序路徑與啟動時間，避免誤停其他程式。這是立即停止，進行中遊戲由下次啟動的
持久化恢復流程處理。自行開啟命令視窗執行時，仍用 Ctrl+C 停止。
新版啟動工具亦會將日誌寫入 `logs/local-runtime.log`，手動雙擊啟動時也能追查錯誤。

本機運行期間，請勿手動重新部署 Railway Bot；回到雲端前必須先停本機。
2026-10-22 本機切換時，已暫時斷開 Railway Bot 的 GitHub Source 連結，避免後續
推送自動啟動第二份 Bot。恢復雲端時，先停止本機，再到該服務 Settings →
Connect Repo，重新連結 `am1ka66a/shinonome-discord-bot` 的 `main` 分支並部署。
MySQL 服務及其資料持續保留在 Railway。
資料庫讀取／寫入預設 30 秒逾時，可用 `MYSQL_READ_TIMEOUT`／`MYSQL_WRITE_TIMEOUT`
調整。初始化舊資料表時使用獨立連線，避免連線池誤將重複欄位錯誤當成斷線重試。

本機連至 Railway 美國區資料庫仍有跨區網路延遲。連線池會在最近 15 秒沒有
成功查詢時才額外 ping（`MYSQL_HEALTHCHECK_IDLE_SECONDS` 可調整），並預設不再
每 50 次 SQL 強制重建連線。這不會快取餘額或遊戲結算資料。
這台電腦的 `.env` 已設定 `MYSQL_POOL_MINCACHED=4`，啟動時先建立四條連線。

2026-10-22 發現電腦 DNS 同時無法解析 Discord 及資料庫主機，已在 `.env` 啟用
`LOCAL_DNS_OVER_HTTPS=true`。此選項只對本機啟動程序生效，透過 Cloudflare
`https://1.1.1.1/dns-query` 解析 Discord 官方網域及設定的公開 MySQL 主機，
保留標準 HTTPS 憑證驗證；不會修改 Windows DNS、hosts 或資料庫位置。
關閉此選項即可恢復使用系統 DNS。資料庫初始化遭遇暫時連線錯誤時會自動重試。

## 在另一台電腦建立環境

先安裝 Python 3.11，再在專案目錄執行：

```cmd
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

啟動時會檢查 Python 與固定套件版本，避免誤用電腦上另一份 Python。
