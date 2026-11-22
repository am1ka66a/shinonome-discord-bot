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

## Railway 設定

`.railway/railway.ts` 僅管理 Bot 服務的設定，變數維持由 Railway 管理；資料庫與磁碟不在這份設定的管理範圍。
變更前執行 `railway config plan`，確認沒有刪除資源或修改資料庫／磁碟，再套用。


## 一致性匯出與還原演練

執行 `python scripts/backup_database.py --output .local-state/backups/date.jsonl.gz`，會使用唯讀一致性交易串流匯出，並產生 SHA-256 清單。檔案保存在 Git 忽略目錄，請以資料庫備份的權限管理。
匯出格式是 gzip 壓縮的 JSONL，每筆 SQL 以 JSON 保存，不會因文字包含分號或換行而截斷。
CI 會將合成測試資料匯出，再還原到全新的 `127.0.0.1/shinonome_restore_test`，核對餘額與文字內容。還原工具會拒絕遠端、正式名稱或已有資料的資料庫。
目前 Railway 工作區方案不提供手動與排程備份；這次上線前已有本機一致性匯出。每日雲端備份尚待使用支援該功能的方案，並未自動升級付費方案。
