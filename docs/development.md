# 開發與部署版本

請用 Python 3.11 建立虛擬環境，並完整安裝 `requirements.txt`（discord.py 2.3.2）。
不要使用另一個專案共用的 Python 套件環境作為上線驗證結果。

執行 `python -m unittest discover -s scripts -p 'test_*.py' -v`，再執行
`python scripts/smoke_check.py` 及 `python scripts/audit_db_scopes.py`。
GitHub Actions 會使用 Python 3.11、固定套件版本及一次性的 MySQL 8 測試資料庫。
MySQL 測試只會在明確啟用且資料庫名稱為 `shinonome_test`、主機為本機時執行。

## 中斷遊戲的處理

21 點、E 卡、紅包的扣款會與遊戲存檔同一筆交易提交。
已確定的結算先保存付款意圖，再將款項、流水與完成狀態一起提交。
同一場再次結算不會重複付款；資料庫暫時失敗時每 20 秒重試已保存的意圖。

部署重啟後，未完成的牌局不恢復舊按鈕，而是退回已記錄的剩餘注金；
紅包只退未領的部分。正常紅包逾時也會退回未領金額。
這個回收程序適用於 Railway 目前的單一 Bot 實例設定，請維持 1 replica。
此機制不能推算啟用存檔前已中斷的歷史遊戲。

功能開關和營業狀態保存在資料庫。黑名單預設限制遊戲、經濟操作及被動獎勵，
查詢與客服聯絡保留；已扣款遊戲仍可結算或退款。

`/say` 使用 `text` 直接發送，支援圖片與指定訊息回覆。
操作紀錄保留操作者、頻道、回覆對象、新訊息 ID 和狀態，不另外保存訊息內容或圖片。
若紀錄停在 `sending`，代表 Discord 發送結果尚待核對，不應盲目重送。


## 正式服務維護

- 每個資料庫只允許一個 Bot：啟動時取得 MySQL 專屬連線鎖，每 5 秒檢查；失去執行權即停止服務。請持續使用 1 replica。
- 執行權檢查逾時或失敗時，先停止服務並釋放連線，再以異常狀態退出，觸發 Railway 的 `ON_FAILURE` 重啟（目前最多 10 次）。重新啟動必須重新取得鎖才可服務；不會偷偷重連舊鎖。正常人工停止不回報此錯誤。
- 啟動時鎖仍被占用或資料庫暫時斷線，會保持離線，按 5、10、20、30 秒（之後每 30 秒）在程式內重試，不消耗平台重啟次數；首次等待記一次 WARNING，後續細節為 DEBUG，恢復時記 INFO。帳密、權限等非暫時性錯誤仍會中止啟動。
- 執行鎖專用連線的 `SESSION wait_timeout` 為 60 秒，正常每 5 秒檢查；網路中斷留下的閒置連線可由 MySQL 自動清除。此設定不改全域值、連線池或遊戲交易；仍有健康機器人持鎖時持續等待，絕不搶鎖或強制中斷其他連線。停止等待時也會回收尚在背景建立的連線。
- 資料庫採 `bot_schema_migrations` 版本紀錄。已完成的結構升級不會在每次重啟重跑；非預期錯誤會阻止啟動，不會略過。
- 歷史 `casino_logs` 保留，啟動不會清空或重建它。既有歷史缺漏需另外核對，不能用現有短期 logs 覆蓋。
- `/resetall_zero` 與 `/resetall_default` 先顯示影響範圍，再由原管理者於 60 秒內確認。必須先 `/lock` 關閉賭場，並完成現有遊戲、紅包及待結算款項。
- 重置影響全部伺服器共用的帳戶。每位玩家的調整前餘額和戰績存於 `bot_admin_snapshots`，操作人與結果存於 `bot_admin_operations`，差額寫入兩份流水；同一操作編號重試不會再次修改。
- 維護關閉期間暫停新遊戲注金及新紅包，既有款項可正常結算／退款。重置後賭場保持關閉，需管理者確認後再 `/lock` 開啟。
- Discord 日誌預設只送 WARNING 以上，每 5 秒合併發送，最多保留 200 種待送訊息；重複訊息合併次數。完整 INFO 記錄保留於 Railway 主控台。可用 `LOG_DISCORD_LEVEL` 調整。
- 重置資料快照不是資料庫外部備份。正式資料仍需另行設定 Railway 備份並定期演練還原；還原前必須停機、核對操作後新增的交易，避免覆蓋新資料。

## 遊戲與查帳模組

- `bot_modules/games/blackjack.py` 與 `games/roulette.py`：純規則、賠率、狀態及序列化；不連線 Discord 或資料庫。
- `bot_modules/ui/blackjack.py` 與 `ui/roulette.py`：訊息與遊戲畫面。指令模組負責互動、鎖定、持久化及付款，既有結算服務不變。
- `bot_modules/admin_audit_repo.py`：有筆數上限的只讀查帳查詢；`commands/admin_audit.py` 負責白名單、私人面板與翻頁。
- 新增規則請先在純規則層驗證；付款、退款、失敗重試仍需另外檢查交易與控制流程。管理員查帳使用方式見 [admin-audit.md](admin-audit.md)。

## 交易與運行可靠性

- 21 點的金額、戰績、EXP 與完成狀態使用同一筆交易；EXP 保存於既有結算意圖中，背景恢復和重試不會重複發放。黑名單玩家可完成已扣款結算，停止發放新的 EXP；退款不發 EXP。
- 搶劫成功與失敗的雙方餘額、流水、冷卻及相關狀態在同一交易中提交，指令回覆不另外補寫流水。不能據此推算或補回舊版本未成功保存的歷史紀錄。
- 聊天 EXP 與資料庫冷卻時間共同提交，重啟後仍遵守 45 秒冷卻；原有每 30 分鐘聊天／語音獎勵金額不變。交易內共用游標更新 EXP，避免另開連線造成連線池互等。
- 執行權檢查使用獨立的單一工作執行緒，一般資料庫工作的執行緒壅塞不會阻擋心跳。背景工作意外停止時會以 5 至 30 秒的間隔自動恢復；取消與正常關閉不會重新啟動。
- INFO／DEBUG 主控台日誌輸出到 stdout，WARNING／ERROR 輸出到 stderr，Discord 用戶端沿用此設定，避免一般連線訊息被平台標成錯誤。不存在的 `!` 文字指令不產生錯誤紀錄；Slash 指令失敗會提供參考編號，不向玩家顯示內部錯誤內容。
- 需要外部成員查詢的指令先回應 Discord；輸入錯誤的選填 ID 會回報錯誤。紅包公開領取名單最多顯示前 20 位，完整入帳仍保存在資料庫並可透過 `/record` 查閱。

## 套件安全檢查

PyMySQL 固定為 1.2.4、cryptography 為 50.0.2、python-dotenv 為 1.2.4。Discord 保留 2.3.2，以既有互動介面與測試驗證相容性。
參考 [PyMySQL 修補紀錄](https://github.com/PyMySQL/PyMySQL/blob/main/CHANGELOG.md)。CI 每次推送／PR 使用固定版本 pip-audit 檢查整份 requirements 與解析後相依套件，有已知漏洞或檢查不完整即失敗；此掃描不等於完整的程式安全證明。

## Railway 設定

`.railway/railway.ts` 僅管理 Bot 服務的設定，變數維持由 Railway 管理；資料庫與磁碟不在這份設定的管理範圍。
變更前執行 `railway config plan`，確認沒有刪除資源或修改資料庫／磁碟，再套用。


## 一致性匯出與還原演練

執行 `python scripts/backup_database.py --output .local-state/backups/date.jsonl.gz`，會使用唯讀一致性交易串流匯出，並產生 SHA-256 清單。檔案保存在 Git 忽略目錄，請以資料庫備份的權限管理。
匯出格式是 gzip 壓縮的 JSONL，每筆 SQL 以 JSON 保存，不會因文字包含分號或換行而截斷。
CI 會將合成測試資料匯出，再還原到全新的 `127.0.0.1/shinonome_restore_test`，核對餘額與文字內容。還原工具會拒絕遠端、正式名稱或已有資料的資料庫。
目前 Railway 工作區方案不提供手動與排程備份；這次上線前已有本機一致性匯出。每日雲端備份尚待使用支援該功能的方案，並未自動升級付費方案。

## 測試 AI 指令

`/ask` 預設停用。啟用時只同步到指定的 Discord 伺服器，且僅指定帳號能在指定文字頻道使用。回覆只有提問者看得見。

1. 為 Shinonome 建立專用的 OpenClaw Gateway。執行 `openclaw models auth login --provider openai --method siwc`，在瀏覽器授權使用 ChatGPT 方案額度，並將預設代理的模型設為該登入可用的模型。Gateway 使用該登入時，無需配置 `OPENAI_API_KEY`。依照 [OpenClaw 的登入文件](https://docs.openclaw.ai/providers/openai/setup) 操作。
2. 啟用 `gateway.http.endpoints.chatCompletions.enabled`，使用 Gateway Token 驗證，並將預設代理的工具設定為最小權限。可設 `tools.profile: "minimal"`，再以 `tools.deny: ["session_status", "gateway"]` 停用此設定檔的工具。此 Gateway 專供文字問答，請避免接入其他帳號或工具。Gateway Token 具有操作員權限，請只允許本機或私有網路連線。參考 [端點文件](https://docs.openclaw.ai/gateway/openai-http-api) 與 [工具權限文件](https://docs.openclaw.ai/gateway/config-tools/tool-policy)。
3. 在 Bot 的執行環境填寫 `.env.example` 中的 `SHINONOME_AI_*` 變數。本機 Gateway 可用 `http://127.0.0.1:18789`。若兩個服務在同一個 Railway 環境，請填入 Gateway 的私有網域及連接埠，例如 `http://openclaw.railway.internal:8080`。填好後設定 `SHINONOME_AI_ENABLED=true`，重啟 Bot，再於測試頻道使用 `/ask`。

將 `SHINONOME_AI_ENABLED` 設為 `false` 並重啟後，Bot 會在伺服器指令同步時移除 `/ask`。
