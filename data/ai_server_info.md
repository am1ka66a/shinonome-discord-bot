# 伺服器資料（範本）

正式的伺服器資料放在 Railway 的 AI_CHAT_SERVER_INFO 變數，設定後就不會讀這個檔案。
這份範本只列出機器人本身的玩法，請不要把真實的伺服器或成員資訊寫在這裡（這個儲存庫是公開的）。

## 經濟與遊戲
- 貨幣叫「東雲幣」。新帳號有 50,000 起始資金。
- 指令都是斜線指令；只有在有人問怎麼玩時，才告訴他可以用 `/help` 看完整說明。

日常與經濟：
- `/daily` 每日簽到、`/hourly` 每小時簽到（依等級累積）、`/beg` 乞討、`/rescue` 破產救濟（餘額 0 時）
- `/transfer` 轉帳、`/redpacket` 發紅包、`/balance` 餘額與戰績、`/record` 帳務紀錄
- `/cooldowns` 查冷卻、`/profile` 玩家名片、`/rank` 全站名次、`/compare` 比較兩位玩家

賭場與等級：
- `/bj` 二十一點、`/coinflip` 拋硬幣（50% 勝率）
- `/russian_roulette duel` 1v1 俄羅斯輪盤、`/russian_roulette royale` 3～6 人淘汰賽
- `/lottery buy` 買日彩券、`/lottery status` 看彩池
- `/duel` E 卡決鬥
- `/level` 等級、`/leaderboard` 餘額榜、`/lvleaderboard` 等級榜、`/casino_stats` 經濟統計

通緝與警察：
- `/role_choose` 選警察、搶匪或平民
- 搶匪可以 `/rob` 搶劫（30 分鐘冷卻，成功會累積通緝星），`/wanted_buyout` 花錢消除通緝
- 警察可以 `/cop_hunt` 追捕通緝犯，抓到就會入獄，入獄要 `/bail` 繳錢出獄
- 平民可以買 `/good_citizen` 良民證防搶，`/break_citizen` 可以摧毀別人的良民證
- `/wanted_status` 看自己的通緝狀態、`/wanted_list` 看通緝名單

其他：
- `/kill` Minecraft 風格隨機死法、`/bicycle` 偷奈音的腳踏車、`/8ball` 魔法八號球、`/choose` 隨機選一項
