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
4. 在 **Token** 區塱按 **Reset Token** / **Copy**，把出現的長字串**先貼在記事本**（**不要**貼在公開網路或 GitHub 上）。
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

## 七、專案裡有什麼（概覽）

- **前綴指令**：`!` 前綴的傳統指令（若有實作）。
- **斜線指令 (Slash)**：例如 `daily`、`hourly`、`bj`（21 點）、`balance`、`stock` 底下多個子指令、比賽相關 `tournament_*`、管理用 `give` / `take` / `ban` 等（權限依 Discord 與程式設計而定）。
- **管理員專用**：`publish_bracket`、`give`、`lock`、`adminhelp` 等，請只給可信任的人使用。

完整列表請在 **Discord 打 `/` 在介面中瀏覽**，或搜尋 `bot.py` 內的 `@bot.tree.command`。

技術重點：`discord.py`、**MySQL** 透過 `PyMySQL`，設定自 **`.env` / `load_dotenv()`**。

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
