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

`/say` 預設先顯示私人預覽，確認後才送出；`preview:false` 可直接發送。
操作紀錄保留操作者、頻道、回覆對象、新訊息 ID 和狀態，不另外保存訊息內容或圖片。
若紀錄停在 `sending`，代表 Discord 發送結果尚待核對，不應盲目重送。
