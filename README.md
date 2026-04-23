# Discord 多功能機器人（經濟、21 點、台股、比賽）

本專案是一個在 Discord 上運行的 **Python** 機器人：虛擬幣、每日／每小時簽到、21 點、台股查詢、自訂比賽報名與雙淘賽制等。資料存在 **MySQL**。

> 目標讀者：**從沒用過也沒關係**，照順序做即可。

---

## 零、你需要先有什麼

| 東西 | 做什麼用 |
|------|----------|
| 電腦可裝 [Python 3.10+](https://www.python.org/downloads/) | 執行機器人程式 |
| 一個 [MySQL](https://dev.mysql.com/downloads/) 或任何相容的雲端資料庫 | 存玩家、餘額、比賽資料 |
| Discord 帳號 + 一個你管理／測試用的伺服器 | 放機器人與用指令 |
| 免費的 [Git](https://git-scm.com/)（若要推上 GitHub） | 版本控制、上傳 GitHub |

---

## 一、在 Discord 開一個「機器人」並拿令牌（Token）

1. 用瀏覽器打開 <https://discord.com/developers/applications> 並登入。
2. 點 **New Application**，取個名字 → **Create**。
3. 左欄點 **Bot** → **Add Bot**（若已經有就略過）。
4. 在 **Token** 區按 **Reset Token** / **Copy**，把出現的長字串**先貼在記事本**（**不要**貼在公開網路或 GitHub 上）。
5. 同一頁建議關掉 **Public Bot**（若你只想自己伺服器用），並打開 **MESSAGE CONTENT INTENT**（機器人需要讀取訊息內容做活躍獎勵等）。
6. 左欄點 **OAuth2** → **URL Generator**：
   - **SCOPES** 勾 `bot`（必要時可再加 `applications.commands` 通常已內建於現代邀請流程）。
   - **BOT PERMISSIONS** 至少依你伺服器需要勾選，例如讀/發訊息、使用 Slash 指令、管理訊息（若用 `/say`）等。
7. 最下方會產生 **Generated URL**，用該網址在瀏覽器打開，選你的測試伺服器，把機器人加進去。

> **令牌 = 密碼**：任何人拿到都能操控你的 Bot，**絕對不要**寫在程式裡上傳 GitHub。只用下面的 `.env` 放。

---

## 二、下載專案（或複製到電腦）

- **若用 Git**（在專案資料夾裡）其實你已有程式時可略過；第一次從空資料夾 clone 的話：  
  `git clone <你的倉庫網址>`  
- **或** 在 GitHub 按 **Code** → **Download ZIP**，解壓到一個固定路徑。

---

## 三、建虛擬環境與安裝套件

在 **專案根目錄**（有 `bot.py` 和 `requirements.txt` 的那層）開 **終端機**（Windows 可開 PowerShell 或「在此資料夾開啟於終端機」）。

**Windows / macOS / Linux 通用寫法：**

```bash
python -m venv venv
```

啟用虛擬環境：

- **Windows (PowerShell):** `.\venv\Scripts\Activate.ps1`
- **Windows (cmd):** `venv\Scripts\activate.bat`
- **macOS / Linux:** `source venv/bin/activate`

然後安裝依賴：

```bash
pip install -r requirements.txt
```

看到沒有紅字錯誤即完成。

---

## 四、設定環境變數 `.env`（本機專用，不要上傳 GitHub）

1. 在專案裡把 **`.env.example`** 複製一份，**改名成** `.env`（注意前面有點）。
2. 用記事本或 VS Code / Cursor 打開 `.env`，照裡面註解填：

   - `DISCORD_TOKEN`：貼上你在「步驟一」複製的令牌。
   - 資料庫：二選一  
     - **A)** 一條 `MYSQL_URL=mysql://...`；或  
     - **B)** 用 `MYSQLHOST`, `MYSQLPORT`, `MYSQLUSER`, `MYSQLPASSWORD`, `MYSQLDATABASE` 分開寫。  

3. 確認 MySQL 已啟動，且**已建立空資料庫**（庫名與 .env 一致）。機器人第一次啟動會自動 `CREATE TABLE` 建表。

> 專案裡的 **`.gitignore` 已包含 `.env`**，所以正常情況下 `git` 不會把密碼推上去。若沒有 `.gitignore`，萬一勿把 `.env` 加入上傳。

---

## 五、在程式裡把「遊戲主辦者」設成你（可選但建議）

打開 `bot.py` 最上方，找到 `ALLOWED_HOST_IDS`：

```python
ALLOWED_HOST_IDS = [531308526262550528, ...]  # 放 Discord 使用者 ID
```

- 刪成只剩 **你自己的 Discord 數字 ID**（在 Discord 開**開發者模式**後，在自己頭像右鍵可複製 ID），再存檔。  
- 沒有放對 ID 的話，很多「遊戲內主辦用」的按鈕／流程不會幫你判定為主辦人。

---

## 六、啟動機器人

在已啟用虛擬環境的終端機、且目前路徑在專案根目錄：

```bash
python bot.py
```

- 如果成功，終端機會出現像「資料庫初始化完成」、「Slash 指令同步完成」、以及 Bot 在線的訊息。
- 到你的 Discord 伺服器，在頻道輸入 `/` 應能看見以 `/` 開頭的指令（如 `/daily`、`/bj` 等）。若一開始沒出現，等約幾分鐘或重開機器人一次，或確認 Bot 有 **applications.commands** 權限與在線。

---

## 七、專案裡有什麼

本專案主程式是單一檔案 **`bot.py`**（未拆成多個 `.py` 模塊，但可從下表對照**程式在檔內的職責**）。技術上：`discord.py` + **MySQL**（`PyMySQL`）+ **`.env`**（`python-dotenv`），對外幾乎全是 **斜線指令 (Slash)**。完整指令列表在 Discord 輸入 `/` 瀏覽，或在原始碼搜尋 `@bot.tree.command` 與 `@stock_group.command`。

### 1. 系統設定與權限

| 內容 | 做什麼 |
|------|--------|
| `ALLOWED_HOST_IDS` | 指定誰是「**遊戲主辦**」（21 點內主辦按鈕、部分賽事／管理行為）。 |
| `IS_EVENT_ACTIVE`、側注比例等常數 | 控制賭桌是否接客、**旁注上限**等全局規則。 |
| `is_host` / `is_slash_host` | 在指令或按鈕內**檢查**是否為主辦，不符合就擋。 |

### 2. 資料庫層

| 內容 | 做什麼 |
|------|--------|
| `get_db_connection` | 從 `MYSQL_URL` / `DATABASE_URL` 或分開的 `MYSQLHOST`… 等讀設定，**連上 MySQL**。 |
| `init_db` | 啟動時**建表、補欄位**（`users`、`activity_stats`、`blacklist`、`daily_claims`、`logs`、`stock_watchlist`、比賽相關表等），缺表則建、舊表則**漸進補欄**（`ALTER`）。 |
| 各資料表 | 存玩家餘額/戰績、**聊天與語音活躍**、**黑名單**、**每日簽到**、**交易紀錄**、**自選股**、**比賽報名／賽程／比分**等。 |

### 3. 經濟與帳本

| 內容 | 做什麼 |
|------|--------|
| `ensure_user_exists`、`get_user_stats`、`try_deduct_balance` | 新玩家**預設餘額**、查戰績、下注時**扣款**（防透支）。 |
| `update_game_result` | 21 點結算後**更新**餘額、胜場、累計盈虧。 |
| `log_transaction` + `logs` 表 | 幾乎所有**金額變動**寫一筆，方便查帳。 |
| `is_blacklisted` | 被黑名單者是否拒絕服務。 |
| 對應指令 | `/daily`、`/hourly`、`/beg`、`/rescue`、`/transfer`、`/redpacket`、轉帳與**排行榜**等。 |

### 4. 等級、經驗、每小時加給

| 內容 | 做什麼 |
|------|--------|
| `exp_for_next_level`、`calc_level_from_exp`、`add_user_exp` | **升級曲線**、依總經驗**換算等級**、發放經驗。 |
| `get_level_stats` | 查等級。 |
| `refresh_hourly_bank`、`payout_hourly_bank` | **每小時可領的「累積槽」**依等級有上限、依時間**慢慢堆**，`/hourly` 一次領出。 |
| 與 `on_message` 連動 | 符合冷卻時在頻道聊天會**小量加經驗**（`EXP_COOLDOWN_SECONDS`）。 |

### 5. 活躍與被動獎勵

| 內容 | 做什麼 |
|------|--------|
| `on_message` | 在伺服器文字頻道計算**訊息數**；夠多句可觸發**活躍獎勵**；並在冷卻外發放**經驗**。需 **Message Content Intent**。 |
| `vc_reward_task` | 背景迴圈：在**語音**且非全靜音等條件下，間隔夠久可發**語音掛網獎勵**到餘額。 |

### 6. 台股查詢模組

| 內容 | 做什麼 |
|------|--------|
| `fetch_stock_day_all` | 呼叫**證交所 OpenAPI** 全上市股票日內盤匯整（有**短快取**）。 |
| `fetch_mis_quotes`、MIS 相輔助函式 | 向 **TWSE MIS** 取**即時報價**（分輪詢、避免單次網址過長）。 |
| `get_realtime_rank_data` | 綜合成交量與即時漲跌，產**排行用**候選＋報價。 |
| `STOCK_API_INSECURE_SSL` | 在 `.env` 可關閉 SSL 驗證（**僅在特定環境需要時**使用，有安全權衡）。 |
| 對應指令 | 斜線群組 **`/stock`**：`quote`、`list`、`movers`、`gainers`、`losers`、`topvolume`、`market`、**自選股** `watch_*` / `watchquote`、`txchart` 等。 |
| `StockPagerView` | 股票清單等介面的**分頁按鈕** UI。 |

### 7. 21 點與遊戲介面

| 內容 | 做什麼 |
|------|--------|
| `get_deck`、`calculate_score` | 使用 **6 副牌**（可調）、算點數（含 A 軟硬）。 |
| `check_sidebets` | **對子旁注**、**21+3 旁注**（同花、順、三條等與盤面賠率）。 |
| `SetupView`、`BetModal` | 開局前**主注、旁注**輸入與確認。 |
| `BlackjackGame` 等 View | 遊戲中**要牌、停牌、分牌、All-in** 等按鈕邏輯；結算寫入經濟。 |
| `ConfirmAllInView`、`NewGameView` | 全下確認、再開一局等流程。 |
| 對應指令 | `/bj` 進入 21 點；`IS_EVENT_ACTIVE` 關閉時會「打烊」。 |

### 8. 紅包

| 內容 | 做什麼 |
|------|--------|
| `build_random_splits` | 將總金額**隨機拆**成多份。 |
| `RedPacketView` | 一則可搶紅包訊息上的**搶奪按鈕**與時效。 |
| 對應指令 | `/redpacket`（從自己餘額扣、發多人搶包）。 |

### 9. 比賽（錦標賽）模組

| 內容 | 做什麼 |
|------|--------|
| 報名與欄位 | 玩家**遊戲內 ID**、**卡組名**、**卡組圖網址**、Discord 對應等。 |
| `get_tournament_window` 等 | **報名起迄時間**的讀寫與顯示。 |
| `publish_bracket`、`_advance_winner`、`_clear_downstream…` 等 | **單淘汰 BO3 賽程**建立、**晉級**、輪空自動推進、**重開比賽**回滾後續。 |
| 比分流程 | 選手 `/tournament_submit_score` 提交、雙方 `/tournament_confirm_score` **同意才成立**；管理員可**直接裁定、指定晉級、重開場次**。 |
| 其他 | `/tournament_list`、`/tournament_bracket` 查名單與戰況等。 |

### 10. 查詢與社群

| 內容 | 做什麼 |
|------|--------|
| `/balance`、`/level`、`/record` | 個人**餘額／戰績**、**等級**、**金流紀錄**（分頁）。 |
| `/leaderboard`、`/lvleaderboard` | 餘額前段班、**等級榜**。 |
| `/say` | 管理員在指定頻道**代機器人發話**（需 `manage_messages` 等權限設計）。 |

### 11. 管理、黑名單、賭場開關

| 內容 | 做什麼 |
|------|--------|
| `/give`、`/take` | 發幣、扣幣。 |
| `/ban`、`/unban` | 黑名單。 |
| `/resetall_zero`、`/resetall_default` | 全服餘額**歸零**或**重設成預設額**（高風險操作）。 |
| `/lock` | 切換**賭場**是否接客。 |
| `/adminhelp` | 看管理相關指令說明。 |
| 比賽用 | `/tournament_remove`、`/clear_tournament_players`、各種 `tournament_admin_*` 等。 |

**提醒**：有「管理」或**全體金錢**影響的指令，只應讓**信任的管理員**使用，並在 Discord 後台**權限**分級。

### `bot.py` 行數區段對照（方便在原始碼裡跳轉）

下表以目前 **`bot.py` 全檔約 2516 行**為基準。之後你若增刪程式，**行數會跟著變動**，實務上以檔內的 `# ---`、`# =====` 註解與 `def` / `class` 名稱為準；此表僅作**大致導航**用。

| 行數 (約) | 區段內容 |
|-----------|----------|
| 1–17 | `import`、`.env` 載入 |
| 19–35 | 系統常數（主辦 ID、賭場開關、等）、`is_host` |
| 37–212 | 資料庫連線、`init_db`、使用者／帳本／黑名單、21 點結算用的 `update_game_result` 等 |
| 214–296 | 等級與經驗、每小時可領「累積槽」`refresh_hourly_bank` / `payout_hourly_bank` |
| 298–555 | 台股（證交所 OpenAPI、MIS 即時、漲跌排行取樣）與**比賽晉級／重開**等純函式（賽制邏輯在這一帶） |
| 556–1119 | 21 點牌組與點數、旁注、以及 **UI**：`SetupView` / `BlackjackGame`、全下確認、**紅包** `RedPacketView`、**台股** `StockPagerView` 等 |
| 1121–1196 | 建立 `Bot` 實例、`on_ready`（建表＋同步 Slash）、**背景語音獎勵** `vc_reward_task`、**訊息活躍** `on_message` |
| 1197–1400 | 經濟向斜線：`/daily`～`/redpacket`（含轉帳、21 點入口 `/bj` 等，至紅包為止） |
| 1401–1734 | 斜線群組 **`/stock`** 全部子指令與**自動完成** `stock_symbol_autocomplete` |
| 1736–1793 | `/say`、`/record`、**餘額榜** `/leaderboard`、**等級榜** `/lvleaderboard` |
| 1795–2392 | 比賽一條龍：報名、報名窗、名單、公布賽程、交比分、雙方確認、以及各種**賽事管理**斜線，至 `tournament_admin_reopen_match` 為止 |
| 2394–2514 | `is_slash_host` 與**主辦向管理**：`give` / `take` / `ban` / `unban`、全服重置、清空比賽報名、`/lock`、`/adminhelp` 等 |
| 2516 | 啟動：`bot.run(os.getenv("DISCORD_TOKEN"))` |

---

## 八、把專案推上 GitHub（一步一步）

### 1. 在瀏覽器建立空倉庫

- 登入 <https://github.com> → **New repository**  
- 取倉庫名稱 → **不要**勾「Initialize with README」（若你本機已經有檔案）→ **Create repository**。

### 2. 在本機專案目錄執行（若已經是 git 倉庫，可從 `git add` 那段開始）

```bash
git init
git add .
git status
```

確認 **`git status` 裡沒有出現 `.env`**（不應被加入）。若出現了，先檢查 `.gitignore` 是否有 `.env` 再重來。

```bash
git commit -m "Initial commit: Discord bot"
git branch -M main
git remote add origin https://github.com/你的帳號/你的倉庫名稱.git
git push -u origin main
```

- 若 GitHub 改用 **main** 以外的預設分支，照網站提示即可。  
- 第一次 `push` 可能會要登入 GitHub（瀏覽器或 **Personal Access Token**）。

之後有改檔就：

```bash
git add .
git commit -m "說明你改了什麼"
git push
```

---

## 常見問題

| 狀況 | 可檢查 |
|------|--------|
| 連不上資料庫 | 防火牆、主機/埠、帳密、庫名是否正確，MySQL 有沒有真的在跑。 |
| Slash 指令沒出現 | Bot 有無邀請到伺服器、權限、等同步完成或重啟。 |
| Token 外洩 | 立刻到開發者後台 **Reset Token**，改 `.env`，舊的當作作廢。 |

---

## 授權

本 README 隨專案提供；原程式之授權若有需要請由專案擁有者自行補上 `LICENSE` 檔。

若你是第一次從 **clone → 安裝 → 填 .env → 執行 → push**，建議在「步驟六」先確認本機能跑，再執行「步驟八」上傳，比較不會帶上不能用的狀態。
