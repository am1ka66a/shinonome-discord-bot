import discord
from discord import app_commands
from discord.ext import commands
import random
import pymysql
import aiohttp
import os
import asyncio
import datetime
import time
import typing
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# ⚙️ 系統設定與全局變數
# ==========================================
ALLOWED_HOST_IDS = [531308526262550528, 600177596088582185]  # ⚠️ 填入你的 Discord ID
SIDE_BET_RATIO = 0.5                     # 側注上限 (主注的 50%)
IS_EVENT_ACTIVE = True                   # 賭場狀態
MAX_LEVEL = 100
EXP_COOLDOWN_SECONDS = 45
STOCK_CACHE_SECONDS = 20
RED_PACKET_MIN_SECONDS = 10
red_packet_seq = 0
stock_cache = {"day_all": {"ts": 0.0, "data": []}}

def is_host():
    def predicate(ctx): return ctx.author.id in ALLOWED_HOST_IDS
    return commands.check(predicate)

# ==========================================
# 🗄️ 1. 資料庫系統 (MySQL)
# ==========================================
def get_db_connection():
    mysql_url = os.getenv('MYSQL_URL') or os.getenv('DATABASE_URL')
    if mysql_url:
        parsed = urlparse(mysql_url)
        if parsed.scheme.startswith('mysql'):
            return pymysql.connect(
                host=parsed.hostname,
                port=parsed.port or 3306,
                user=parsed.username,
                password=parsed.password,
                database=(parsed.path or '/').lstrip('/'),
                charset='utf8mb4'
            )

    return pymysql.connect(
        host=os.getenv('MYSQLHOST') or os.getenv('DB_HOST'),
        port=int(os.getenv('MYSQLPORT') or os.getenv('DB_PORT', 3306)),
        user=os.getenv('MYSQLUSER') or os.getenv('DB_USER'),
        password=os.getenv('MYSQLPASSWORD') or os.getenv('DB_PASS'),
        database=os.getenv('MYSQLDATABASE') or os.getenv('DB_NAME'),
        charset='utf8mb4'
    )

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (user_id VARCHAR(255) PRIMARY KEY, balance BIGINT, rescue_count INT DEFAULT 0,
                  total_games INT DEFAULT 0, wins INT DEFAULT 0, total_profit BIGINT DEFAULT 0,
                  last_work TIMESTAMP NULL, last_beg TIMESTAMP NULL, last_rescue TIMESTAMP NULL,
                  exp BIGINT DEFAULT 0, level INT DEFAULT 1,
                  last_hourly_claim TIMESTAMP NULL, hourly_bank INT DEFAULT 0)''')
    # 確保現有表也有新欄位 (Migration)
    try: c.execute("ALTER TABLE users ADD COLUMN last_work TIMESTAMP NULL")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN last_beg TIMESTAMP NULL")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN last_rescue TIMESTAMP NULL")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN exp BIGINT DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN level INT DEFAULT 1")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN last_hourly_claim TIMESTAMP NULL")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN hourly_bank INT DEFAULT 0")
    except: pass

    c.execute('''CREATE TABLE IF NOT EXISTS activity_stats 
                 (user_id VARCHAR(255) PRIMARY KEY, msg_count INT DEFAULT 0, 
                  last_msg_reward TIMESTAMP NULL, last_vc_reward TIMESTAMP NULL,
                  last_exp_reward TIMESTAMP NULL)''')
    try: c.execute("ALTER TABLE activity_stats ADD COLUMN last_exp_reward TIMESTAMP NULL")
    except: pass

    c.execute('''CREATE TABLE IF NOT EXISTS blacklist (user_id VARCHAR(255) PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_claims (user_id VARCHAR(255) PRIMARY KEY, last_claim DATE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS logs (id INT AUTO_INCREMENT PRIMARY KEY, user_id VARCHAR(255), amount BIGINT, reason VARCHAR(255), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def log_transaction(user_id, amount, reason):
    if amount == 0: return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO logs (user_id, amount, reason) VALUES (%s, %s, %s)", (str(user_id), amount, reason))
    conn.commit()
    conn.close()

def is_blacklisted(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM blacklist WHERE user_id=%s", (str(user_id),))
    res = c.fetchone()
    conn.close()
    return res is not None

def get_user_stats(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance, total_games, wins, total_profit FROM users WHERE user_id=%s", (str(user_id),))
    res = c.fetchone()
    conn.close()
    return res

def try_deduct_balance(user_id, amount, reason):
    if amount <= 0:
        return True
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
        (amount, str(user_id), amount)
    )
    ok = c.rowcount > 0
    conn.commit()
    conn.close()
    if ok:
        log_transaction(user_id, -amount, reason)
    return ok

def update_game_result(user_id, balance_delta, profit_delta, is_win, is_push=False):
    conn = get_db_connection()
    c = conn.cursor()
    win_int = 1 if is_win else 0
    if is_push:
        c.execute("UPDATE users SET balance=balance+%s, total_profit=total_profit+%s WHERE user_id=%s",
                  (balance_delta, profit_delta, str(user_id)))
    else:
        c.execute("UPDATE users SET balance=balance+%s, total_profit=total_profit+%s, total_games=total_games+1, wins=wins+%s WHERE user_id=%s",
                  (balance_delta, profit_delta, win_int, str(user_id)))
    conn.commit()
    conn.close()
    if balance_delta != 0:
        log_transaction(user_id, balance_delta, "21點遊戲結算")

def exp_for_next_level(level):
    lv = max(1, min(MAX_LEVEL, level))
    return 60 + lv * 25 + int((lv ** 1.6) * 8)

def calc_level_from_exp(exp):
    level = 1
    remaining = max(0, int(exp))
    while level < MAX_LEVEL:
        need = exp_for_next_level(level)
        if remaining < need:
            break
        remaining -= need
        level += 1
    return level, remaining, (0 if level >= MAX_LEVEL else exp_for_next_level(level))

def get_level_stats(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT exp, level FROM users WHERE user_id=%s", (str(user_id),))
    row = c.fetchone()
    conn.close()
    return row

def add_user_exp(user_id, amount):
    if amount <= 0:
        return None
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT exp, level FROM users WHERE user_id=%s", (str(user_id),))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    old_exp, old_level = int(row[0] or 0), int(row[1] or 1)
    new_exp = old_exp + int(amount)
    new_level, _, _ = calc_level_from_exp(new_exp)
    if new_level != old_level:
        c.execute("UPDATE users SET exp=%s, level=%s WHERE user_id=%s", (new_exp, new_level, str(user_id)))
    else:
        c.execute("UPDATE users SET exp=%s WHERE user_id=%s", (new_exp, str(user_id)))
    conn.commit()
    conn.close()
    return old_level, new_level, new_exp

def refresh_hourly_bank(user_id):
    now = datetime.datetime.now()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT level, last_hourly_claim, hourly_bank FROM users WHERE user_id=%s", (str(user_id),))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    level = max(1, min(MAX_LEVEL, int(row[0] or 1)))
    last_claim = row[1]
    bank = int(row[2] or 0)
    if last_claim is None:
        c.execute("UPDATE users SET last_hourly_claim=%s WHERE user_id=%s", (now, str(user_id)))
        conn.commit()
        conn.close()
        return {"level": level, "bank": bank, "next_in_seconds": 3600}

    elapsed_hours = int((now - last_claim).total_seconds() // 3600)
    if elapsed_hours > 0:
        bank = min(level, bank + elapsed_hours)
        last_claim = last_claim + datetime.timedelta(hours=elapsed_hours)
        c.execute("UPDATE users SET hourly_bank=%s, last_hourly_claim=%s WHERE user_id=%s", (bank, last_claim, str(user_id)))
        conn.commit()
    next_in_seconds = max(0, 3600 - int((now - last_claim).total_seconds()))
    conn.close()
    return {"level": level, "bank": bank, "next_in_seconds": next_in_seconds}

def payout_hourly_bank(user_id, bank, reward_per_slot):
    if bank <= 0:
        return 0
    payout = int(bank * reward_per_slot)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET balance=balance+%s, hourly_bank=0 WHERE user_id=%s", (payout, str(user_id)))
    conn.commit()
    conn.close()
    log_transaction(user_id, payout, "每小時簽到")
    return payout

def to_float(value, default=0.0):
    try:
        return float(str(value).replace(",", ""))
    except:
        return default

async def fetch_stock_day_all():
    now = time.time()
    cache_obj = stock_cache["day_all"]
    if now - cache_obj["ts"] < STOCK_CACHE_SECONDS and cache_obj["data"]:
        return cache_obj["data"]
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(url, timeout=15) as resp:
            if resp.status != 200:
                raise RuntimeError(f"TWSE API 錯誤: {resp.status}")
            data = await resp.json()
    cache_obj["ts"] = now
    cache_obj["data"] = data
    return data

def _parse_mis_price(item):
    z = str(item.get("z", "")).strip()
    if z and z != "-":
        return to_float(z, 0.0)
    # z 為 "-" 時，用最佳買價欄位補
    b = str(item.get("b", "")).split("_")[0].strip()
    return to_float(b, 0.0)

def _to_mis_quote(item):
    code = str(item.get("c", "")).strip()
    name = str(item.get("n", "")).strip() or code
    price = _parse_mis_price(item)
    prev_close = to_float(item.get("y", "0"), 0.0)
    volume = int(to_float(item.get("v", "0"), 0.0))
    change = price - prev_close if prev_close > 0 and price > 0 else 0.0
    pct = (change / prev_close * 100) if prev_close > 0 and price > 0 else 0.0
    return {
        "code": code,
        "name": name,
        "price": price,
        "prev_close": prev_close,
        "change": change,
        "pct": pct,
        "volume": volume,
        "time": str(item.get("t", "")).strip(),
        "channel": str(item.get("ch", "")).strip(),
    }

async def fetch_mis_quotes(channels):
    if not channels:
        return []
    ex_ch = "|".join(channels)
    ts = int(time.time() * 1000)
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0&_={ts}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://mis.twse.com.tw/stock/index.jsp"}
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        async with session.get(url, timeout=15) as resp:
            if resp.status != 200:
                raise RuntimeError(f"MIS API 錯誤: {resp.status}")
            payload = await resp.json()
    items = payload.get("msgArray") or []
    return [_to_mis_quote(item) for item in items if item]

def build_mis_channels_for_code(code):
    c = str(code).strip().upper()
    return [f"tse_{c}.tw", f"otc_{c}.tw"]

# ==========================================
# 🃏 2. 核心遊戲邏輯 (6副牌)
# ==========================================
def get_deck(num_decks=6):
    suits = ['♥️', '♦️', '♣️', '♠️']
    ranks = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    return [{'rank': r, 'suit': s} for s in suits for r in ranks] * num_decks

def card_to_emoji(card, guild_id=None) -> str:
    return f"**[{card['rank']} {card['suit']}]**"

async def sync_guild_emojis(guild: discord.Guild):
    pass

def card_back_emoji(guild_id=None) -> str:
    return "**[??]**"

async def _send_game(channel, gv: 'BlackjackGame', interaction: discord.Interaction = None, message_obj: discord.Message = None, view=None, 
                     done=False, res="", profit=0, animating=False, extra_msg="") -> discord.Message:
    embed = gv.build_embed(done=done, res=res, profit=profit, animating=animating, extra_msg=extra_msg, guild_id=channel.guild.id if channel.guild else None)
    current_view = view if view is not None else gv

    if interaction:
        if interaction.response.is_done():
            return await interaction.edit_original_response(embed=embed, view=current_view, attachments=[])
        else:
            await interaction.response.edit_message(embed=embed, view=current_view, attachments=[])
            return await interaction.original_response()
    elif message_obj:
        return await message_obj.edit(embed=embed, view=current_view, attachments=[])
    return await channel.send(embed=embed, view=current_view)

def calculate_score(hand):
    score, aces = 0, 0
    values = {'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,'10':10,'J':10,'Q':10,'K':10,'A':11}
    for c in hand:
        score += values[c['rank']]
        if c['rank'] == 'A': aces += 1
    while score > 21 and aces:
        score -= 10
        aces -= 1
    return score

def check_sidebets(player_hand, dealer_up, p_bet, s_bet):
    res_msg, total_p = "", 0
    if p_bet > 0:
        c1, c2 = player_hand[0], player_hand[1]
        if c1['rank'] == c2['rank']:
            if c1['suit'] == c2['suit']: mult, m = 30, "同花對子"
            else: mult, m = 5, "混合對子"
            total_p += p_bet * mult
            res_msg += f"🧧 {m}！+{p_bet*mult} "
        else:
            total_p -= p_bet
            res_msg += f"🧧 對子未中 -{p_bet} "
    if s_bet > 0:
        cards = [player_hand[0], player_hand[1], dealer_up]
        suits = [c['suit'] for c in cards]
        rv = {'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,'10':10,'J':11,'Q':12,'K':13,'A':14}
        v = sorted([rv[c['rank']] for c in cards])
        if v == [2,3,14]: v = [1,2,3]
        is_flush    = len(set(suits)) == 1
        is_straight = (v[2]-v[1] == 1 and v[1]-v[0] == 1)
        is_triplet  = len(set([c['rank'] for c in cards])) == 1
        if is_flush and is_triplet: mult, m = 50, "同花三條"
        elif is_flush and is_straight: mult, m = 25, "同花順"
        elif is_triplet: mult, m = 25, "三條"
        elif is_straight: mult, m = 10, "順子"
        elif is_flush: mult, m = 5, "同花"
        else: mult, m = -1, "未中"
        
        if mult > 0:
            total_p += s_bet * mult
            res_msg += f"🎯 21+3 {m}！+{s_bet*mult} "
        else:
            total_p -= s_bet
            res_msg += f"🎯 21+3 未中 -{s_bet} "
    return total_p, res_msg

# ==========================================
# 🖼️ 3. 遊戲 UI 區塊
# ==========================================
class BetModal(discord.ui.Modal, title='自訂下注金額'):
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.b_input = discord.ui.TextInput(label='主注 (最低 100)', default=str(view.base_bet), required=True)
        self.p_input = discord.ui.TextInput(label='對子旁注', default=str(view.p_bet), required=False)
        self.s_input = discord.ui.TextInput(label='21+3旁注', default=str(view.s_bet), required=False)
        self.add_item(self.b_input)
        self.add_item(self.p_input)
        self.add_item(self.s_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            b = int(self.b_input.value)
            p = int(self.p_input.value or 0)
            s = int(self.s_input.value or 0)
            if b < 100 or p < 0 or s < 0: raise ValueError
        except ValueError:
            return await interaction.response.send_message("請輸入有效正整數 (主注最低 100)", ephemeral=True)
        max_side = int(b * SIDE_BET_RATIO)
        if p + s > max_side:
            return await interaction.response.send_message(f"旁注總和 ({p+s}) 不能超過主注的 {int(SIDE_BET_RATIO*100)}% ({max_side})", ephemeral=True)
        stats = get_user_stats(self.view.user.id)
        if not stats: return await interaction.response.send_message("請先使用 /register 註冊！", ephemeral=True)
        if stats[0] < (b + p + s): return await interaction.response.send_message(f"餘額不足！你目前有 {stats[0]} 東雲幣", ephemeral=True)
        self.view.base_bet = b
        self.view.max_side = max_side
        self.view.p_bet = p
        self.view.s_bet = s
        await interaction.response.edit_message(embed=self.view.build_embed(), view=self.view)

class SetupView(discord.ui.View):
    def __init__(self, user, base_bet, p_bet=0, s_bet=0):
        super().__init__(timeout=90)
        self.user, self.base_bet = user, base_bet
        self.p_bet, self.s_bet = p_bet, s_bet
        self.max_side = int(base_bet * SIDE_BET_RATIO)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("這不是你的牌局！", ephemeral=True)
            return False
        now = asyncio.get_running_loop().time()
        if hasattr(self, "last_action") and now - self.last_action < 2.0:
            await interaction.response.send_message("⚠️ 操作太快了！按鈕有 2 秒冷卻時間。", ephemeral=True)
            return False
        self.last_action = now
        return True

    def build_embed(self, err=""):
        stats = get_user_stats(self.user.id)
        embed = discord.Embed(title="🃏 21點 — 下注設定", color=0x2b2d31)
        embed.description = f"{'❌ ' + err + '\n' if err else ''}主注：`{self.base_bet}`\n旁注剩餘額度：**`{self.max_side - (self.p_bet + self.s_bet)}`**\n你的餘額：`{stats[0]}`"
        embed.add_field(name="🧧 對子旁注", value=f"下注金額：`{self.p_bet}`\n**同花對子**: 30倍\n**混合對子**: 5倍", inline=True)
        embed.add_field(name="🎯 21+3旁注", value=f"下注金額：`{self.s_bet}`\n**同花三條**: 50倍\n**同花順**: 25倍\n**三條**: 25倍\n**順子**: 10倍\n**同花**: 5倍", inline=True)
        return embed

    @discord.ui.button(label="開始遊戲 (再來一局)", style=discord.ButtonStyle.success)
    async def start(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        stats = get_user_stats(self.user.id)
        if not stats: return await inter.followup.send("請先使用 /register 註冊！", ephemeral=True)
        total_cost = self.base_bet + self.p_bet + self.s_bet
        if not try_deduct_balance(self.user.id, total_cost, "21點開局扣款"):
            return await inter.followup.send("餘額不足", ephemeral=True)
        self.stop()
        gv = BlackjackGame(self.user, self.base_bet, self.p_bet, self.s_bet, upfront_cost=total_cost)
        await _send_game(inter.channel, gv, interaction=inter)
        msg = await inter.original_response()
        asyncio.create_task(gv.check_auto_bj(msg))

    @discord.ui.button(label="自訂下注金額", style=discord.ButtonStyle.primary)
    async def custom_bet(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.send_modal(BetModal(self))

class BlackjackGame(discord.ui.View):
    def __init__(self, user, bet, p_bet, s_bet, upfront_cost=0):
        super().__init__(timeout=90)
        self.user, self.bet, self.p_bet, self.s_bet = user, bet, p_bet, s_bet
        self.total_deducted = upfront_cost
        self.hand_bets = [bet]
        self.deck = get_deck()
        random.shuffle(self.deck)
        self.hands = [[self.deck.pop(), self.deck.pop()]]
        self.d_hand = [self.deck.pop(), self.deck.pop()]
        self.current_hand = 0
        self.hand_results = [None]
        self.side_p, self.side_m = check_sidebets(self.hands[0], self.d_hand[0], p_bet, s_bet)
        self.update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("這不是你的牌局！", ephemeral=True)
            return False
        now = asyncio.get_running_loop().time()
        if hasattr(self, "last_action") and now - self.last_action < 1.0:
            await interaction.response.send_message("⚠️ 操作太快了！請慢慢點擊。", ephemeral=True)
            return False
        self.last_action = now
        return True

    async def _edit(self, message=None, extra_msg="", interaction: discord.Interaction = None, done=False, res="", profit=0, animating=False):
        try:
            if interaction:
                await _send_game(interaction.channel, self, interaction=interaction, done=done, res=res, profit=profit, animating=animating, extra_msg=extra_msg)
            elif message:
                await _send_game(message.channel, self, message_obj=message, done=done, res=res, profit=profit, animating=animating, extra_msg=extra_msg)
        except Exception as e: print(f"❌ 渲染錯誤: {e}")

    @property
    def p_hand(self): return self.hands[self.current_hand]

    def update_buttons(self):
        values = {'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,'10':10,'J':10,'Q':10,'K':10,'A':11}
        can_split = len(self.hands) == 1 and len(self.p_hand) == 2 and values[self.p_hand[0]['rank']] == values[self.p_hand[1]['rank']]
        can_double = len(self.p_hand) == 2
        to_remove = []
        for c in self.children:
            if c.label == "分牌":
                if not can_split: to_remove.append(c)
            elif c.label == "雙倍":
                if not can_double: to_remove.append(c)
            elif c.label == "投降":
                c.disabled = len(self.p_hand) > 2 or len(self.hands) > 1
            elif c.label == "要牌":
                c.disabled = calculate_score(self.p_hand) > 21
        for c in to_remove: self.remove_item(c)

    def build_embed(self, done=False, res="", profit=0, animating=False, extra_msg="", guild_id=None):
        stats = get_user_stats(self.user.id)
        if stats: bal, total, wins, t_prof = stats
        else: bal, total, wins, t_prof = 0, 0, 0, 0
        wr = (wins/total*100) if total>0 else 0
        embed = discord.Embed(title="🃏 21點大賽", color=0x2b2d31)
        main_ui = f"💰 餘額：{bal} | 🏆 勝場：{wins} | 🎲 總局數：{total} | 📈 勝率：{wr:.1f}% | 💸 總盈虧：{t_prof}\n"
        if extra_msg: main_ui += f"**{extra_msg}**\n"
        for i, hand in enumerate(self.hands):
            indicator = "👉 " if i == self.current_hand and not done else ""
            title_text = f"{indicator}👤 {self.user.display_name} 的手牌"
            if len(self.hands) > 1: title_text += f" (第 {i+1} 手)"
            p_cards = ' '.join([card_to_emoji(c, guild_id) for c in hand])
            main_ui += f"### {title_text}\n### {p_cards} (點數: **{calculate_score(hand)}**)\n"
        if done or animating:
            d_cards = ' '.join([card_to_emoji(c, guild_id) for c in self.d_hand])
            main_ui += f"### 🤖 莊家手牌\n### {d_cards} (點數: **{calculate_score(self.d_hand)}**)\n"
            if done:
                total_profit = profit + self.side_p
                res_line = f"### 🏆 {res}\n{self.side_m}\n"
                if total_profit > 0: res_line += f"📈 總盈虧：`+{total_profit}` | 💰 餘額：`{bal}`\n"
                elif total_profit < 0: res_line += f"📉 總盈虧：`{total_profit}` | 💰 餘額：`{bal}`\n"
                else: res_line += f"➖ 無輸贏 | 💰 餘額：`{bal}`\n"
                main_ui += res_line
        else:
            main_ui += f"### 🤖 莊家手牌\n### {card_to_emoji(self.d_hand[0], guild_id)} {card_back_emoji(guild_id)} (點數: **❓**)\n"
        embed.description = main_ui
        return embed

    async def check_auto_bj(self, message):
        if len(self.p_hand) == 2 and calculate_score(self.p_hand) == 21:
            await asyncio.sleep(1.5)
            try: await self.advance_hand(message_obj=message)
            except: pass

    async def end(self, res, prof, win=False, is_push=False, message_obj=None, interaction=None):
        if getattr(self, '_game_over', False): return
        self._game_over = True
        
        total_p = prof + getattr(self, 'side_p', 0)
        settlement_credit = self.total_deducted + total_p
        update_game_result(self.user.id, settlement_credit, total_p, win, is_push)
        
        for c in self.children: c.disabled = True
        stats = get_user_stats(self.user.id)
        nv  = NewGameView(self.user, self.bet, self.p_bet, self.s_bet, stats[0] if stats else 0)
        await _send_game(message_obj.channel if message_obj else interaction.channel, self, 
                         interaction=interaction, message_obj=message_obj, view=nv, 
                         done=True, res=res, profit=prof)

    async def advance_hand(self, message_obj=None, interaction=None):
        if getattr(self, '_game_over', False): return
        if self.current_hand < len(self.hands) - 1:
            self.current_hand += 1
            self.update_buttons()
            await self._edit(message=message_obj, interaction=interaction, extra_msg=f"👉 換第 {self.current_hand+1} 手牌")
            if len(self.p_hand) == 2 and calculate_score(self.p_hand) == 21:
                await asyncio.sleep(1.5)
                await self.advance_hand(message_obj=message_obj, interaction=interaction)
        else:
            await self.resolve_dealer(message_obj=message_obj, interaction=interaction)

    async def resolve_dealer(self, message_obj=None, interaction=None):
        if getattr(self, '_game_over', False): return
        need_dealer = any(hand is None for hand in self.hand_results)
        for c in self.children: c.disabled = True
        await self._edit(message=message_obj, interaction=interaction, animating=True)
        if need_dealer:
            await asyncio.sleep(1.2)
            while calculate_score(self.d_hand) < 17 and len(self.d_hand) < 5:
                self.d_hand.append(self.deck.pop())
                await self._edit(message=message_obj, interaction=None, animating=True)
                await asyncio.sleep(1.2)
        total_prof, final_res_texts = 0, []
        ds = calculate_score(self.d_hand)
        dealer_bj = len(self.d_hand) == 2 and ds == 21
        dealer_5_card = len(self.d_hand) == 5 and ds <= 21
        for i, hand in enumerate(self.hands):
            if self.hand_results[i] is not None:
                r, p, w = self.hand_results[i]
                final_res_texts.append(f"第 {i+1} 手: {r}" if len(self.hands)>1 else r)
                total_prof += p
                continue
            ps = calculate_score(hand)
            player_bj, player_5_card = (len(hand) == 2 and ps == 21), (len(hand) == 5 and ps <= 21)
            if player_5_card and dealer_5_card: final_res_texts.append("🤝 雙方皆過五關！平手")
            elif player_5_card: final_res_texts.append("🐉 你過五關啦！爽贏 2.5 倍！"); total_prof += int(self.hand_bets[i] * 2.5)
            elif dealer_5_card: final_res_texts.append("🐉 老子過五關啦！你這低能兒～"); total_prof -= self.hand_bets[i]
            elif player_bj and dealer_bj: final_res_texts.append("🤝 雙方皆為 BlackJack！平手")
            elif player_bj: final_res_texts.append("🌟 BlackJack！1.5倍賠率！"); total_prof += int(self.hand_bets[i] * 1.5)
            elif dealer_bj: final_res_texts.append("💀 莊家 BlackJack！你輸啦～雜魚～"); total_prof -= self.hand_bets[i]
            elif ds > 21 or ps > ds: final_res_texts.append("🎉 這次算你贏啦，腦殘！"); total_prof += self.hand_bets[i]
            elif ps < ds: final_res_texts.append("💀 你輸啦～雜魚～"); total_prof -= self.hand_bets[i]
            else: final_res_texts.append("🤝 就這點技術阿腦殘？")
        final_msg = "\n".join(final_res_texts)
        total_combined = total_prof + getattr(self, 'side_p', 0)
        await self.end(final_msg, total_prof, total_combined > 0, total_combined == 0, message_obj=message_obj, interaction=interaction)

    @discord.ui.button(label="要牌", style=discord.ButtonStyle.success)
    async def hit(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        self.p_hand.append(self.deck.pop())
        self.update_buttons() 
        ps = calculate_score(self.p_hand)
        if ps > 21 or len(self.p_hand) == 5:
            if ps > 21: self.hand_results[self.current_hand] = ("爆牌輸了", -self.hand_bets[self.current_hand], False)
            await self.advance_hand(interaction=inter, message_obj=inter.message)
        else: await self._edit(interaction=inter)

    @discord.ui.button(label="停牌", style=discord.ButtonStyle.danger)
    async def stand(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer(); await self.advance_hand(interaction=inter, message_obj=inter.message)

    @discord.ui.button(label="投降", style=discord.ButtonStyle.secondary)
    async def surrender(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer(); self.hand_results[self.current_hand] = ("這樣就投降了嗎，雜魚～", -(self.hand_bets[self.current_hand]//2), False)
        await self.advance_hand(interaction=inter, message_obj=inter.message)

    @discord.ui.button(label="雙倍", style=discord.ButtonStyle.primary)
    async def double_down(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        extra_cost = self.hand_bets[self.current_hand]
        if not try_deduct_balance(self.user.id, extra_cost, "21點雙倍加注"):
            return await inter.followup.send("餘額不足", ephemeral=True)
        self.total_deducted += extra_cost
        self.hand_bets[self.current_hand] *= 2
        self.p_hand.append(self.deck.pop())
        if calculate_score(self.p_hand) > 21: self.hand_results[self.current_hand] = ("你爆牌囉～小丑～", -self.hand_bets[self.current_hand], False)
        await self.advance_hand(interaction=inter, message_obj=inter.message)

    @discord.ui.button(label="分牌", style=discord.ButtonStyle.primary)
    async def split(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        if not try_deduct_balance(self.user.id, self.bet, "21點分牌加注"):
            return await inter.followup.send("餘額不足", ephemeral=True)
        self.total_deducted += self.bet
        self.is_split, c1, c2 = True, self.hands[0][0], self.hands[0][1]
        self.hands, self.hand_results, self.hand_bets = [[c1, self.deck.pop()], [c2, self.deck.pop()]], [None, None], [self.bet, self.bet]
        self.update_buttons(); await self._edit(interaction=inter, extra_msg="✌️ 你選擇了分牌！")
        if calculate_score(self.p_hand) == 21: await asyncio.sleep(1.5); await self.advance_hand(interaction=None, message_obj=inter.message)

class ConfirmAllInView(discord.ui.View):
    def __init__(self, user, parent_msg):
        super().__init__(timeout=30)
        self.user, self.parent_msg = user, parent_msg
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id: return False
        return True
    @discord.ui.button(label="確定 All In！", style=discord.ButtonStyle.danger)
    async def confirm(self, inter, btn):
        stats = get_user_stats(self.user.id)
        if not stats or stats[0] < 100: return await inter.response.send_message("去乞討吧雜魚", ephemeral=True)
        self.stop(); await inter.response.edit_message(content="🔥 All In 已確認！正在為你開牌...", view=None)
        try: await self.parent_msg.delete()
        except: pass
        gv = BlackjackGame(self.user, stats[0], 0, 0)
        msg = await _send_game(inter.channel, gv)
        asyncio.create_task(gv.check_auto_bj(msg))

class NewGameView(discord.ui.View):
    def __init__(self, user, last_bet, last_p_bet, last_s_bet, current_bal):
        super().__init__(timeout=90)
        self.user, self.last_bet, self.last_p_bet, self.last_s_bet, self.current_bal = user, last_bet, last_p_bet, last_s_bet, current_bal
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id: return False
        return True
    @discord.ui.button(label="再來一局", style=discord.ButtonStyle.success)
    async def again(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        total_cost = self.last_bet + self.last_p_bet + self.last_s_bet
        if not try_deduct_balance(self.user.id, total_cost, "21點開局扣款"):
            return await inter.followup.send("餘額不足", ephemeral=True)
        self.stop(); gv = BlackjackGame(self.user, self.last_bet, self.last_p_bet, self.last_s_bet, upfront_cost=total_cost)
        await _send_game(inter.channel, gv, interaction=inter)
        msg = await inter.original_response(); asyncio.create_task(gv.check_auto_bj(msg))
    @discord.ui.button(label="雙倍再局 (Double)", style=discord.ButtonStyle.primary)
    async def double_again(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        new_bet = self.last_bet * 2
        total_cost = new_bet + self.last_p_bet + self.last_s_bet
        if not try_deduct_balance(self.user.id, total_cost, "21點開局扣款"):
            return await inter.followup.send("餘額不足", ephemeral=True)
        self.stop(); gv = BlackjackGame(self.user, new_bet, self.last_p_bet, self.last_s_bet, upfront_cost=total_cost)
        await _send_game(inter.channel, gv, interaction=inter)
        msg = await inter.original_response(); asyncio.create_task(gv.check_auto_bj(msg))
    @discord.ui.button(label="修改下注", style=discord.ButtonStyle.secondary)
    async def modify_bet(self, inter, btn):
        self.stop(); await inter.response.defer()
        try: await inter.message.delete()
        except: pass
        setup = SetupView(self.user, self.last_bet, self.last_p_bet, self.last_s_bet)
        await inter.channel.send(embed=setup.build_embed(), view=setup)
    @discord.ui.button(label="All In (全押)", style=discord.ButtonStyle.danger)
    async def all_in(self, inter, btn):
        cv = ConfirmAllInView(self.user, inter.message)
        await inter.response.send_message("⚠️ 警告：要全押嗎雜魚？", view=cv, ephemeral=True)

def build_random_splits(total_amount, count):
    remaining = total_amount
    amounts = []
    for i in range(count - 1):
        max_pick = remaining - (count - i - 1)
        pick = random.randint(1, max_pick)
        amounts.append(pick)
        remaining -= pick
    amounts.append(remaining)
    random.shuffle(amounts)
    return amounts

class RedPacketView(discord.ui.View):
    def __init__(self, creator_id, total_amount, count):
        super().__init__(timeout=120)
        global red_packet_seq
        red_packet_seq += 1
        self.packet_id = red_packet_seq
        self.creator_id = creator_id
        self.total_amount = total_amount
        self.count = count
        self.left_amount = total_amount
        self.left_count = count
        self.claimed_users = set()

    def summary_text(self):
        claimed = self.count - self.left_count
        return (
            f"🧧 紅包編號 #{self.packet_id}\n"
            f"總金額：`{self.total_amount}` | 份數：`{self.count}`\n"
            f"已搶：`{claimed}` 人 | 剩餘金額：`{self.left_amount}`"
        )

    @discord.ui.button(label="搶紅包", style=discord.ButtonStyle.success)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.bot:
            return await interaction.response.send_message("機器人不能搶紅包", ephemeral=True)
        if interaction.user.id in self.claimed_users:
            return await interaction.response.send_message("你已經搶過這包了", ephemeral=True)
        if self.left_count <= 0 or self.left_amount <= 0:
            return await interaction.response.send_message("紅包已搶完", ephemeral=True)

        if self.left_count == 1:
            amount = self.left_amount
        else:
            max_pick = self.left_amount - (self.left_count - 1)
            amount = random.randint(1, max_pick)
        self.left_amount -= amount
        self.left_count -= 1
        self.claimed_users.add(interaction.user.id)

        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "INSERT INTO users (user_id, balance) VALUES (%s, %s) ON DUPLICATE KEY UPDATE balance=balance+%s",
            (str(interaction.user.id), amount, amount)
        )
        conn.commit()
        conn.close()
        log_transaction(interaction.user.id, amount, f"搶紅包 #{self.packet_id}")

        if self.left_count <= 0 or self.left_amount <= 0:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content=self.summary_text() + "\n✅ 紅包已被搶完！", view=self)
            return
        await interaction.response.edit_message(content=self.summary_text(), view=self)
        await interaction.followup.send(f"🎉 你搶到 `{amount}` 東雲幣！", ephemeral=True)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if hasattr(self, "message") and self.message:
                await self.message.edit(content=self.summary_text() + "\n⌛ 紅包已逾時關閉。", view=self)
        except:
            pass

# --- 4. 指令系統 ---
intents = discord.Intents.default(); intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
stock_group = app_commands.Group(name="stock", description="台股查詢")
bot.tree.add_command(stock_group)

@bot.event
async def on_ready():
    try:
        init_db()
        print("✅ 資料庫初始化完成")
    except Exception as e:
        print(f"❌ init_db 失敗: {e}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Slash 指令同步完成: {len(synced)}")
    except Exception as e:
        print(f"❌ Slash 同步失敗: {e}")
    # 在每個伺服器做 guild sync，讓新指令幾乎即時可用
    for guild in bot.guilds:
        try:
            gsynced = await bot.tree.sync(guild=guild)
            print(f"✅ Guild 同步完成 {guild.id}: {len(gsynced)}")
        except Exception as e:
            print(f"❌ Guild 同步失敗 {guild.id}: {e}")
    bot.loop.create_task(vc_reward_task())
    print(f"{bot.user} 啟動！")

async def vc_reward_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(600)
        now, conn = datetime.datetime.now(), get_db_connection(); c = conn.cursor()
        awarded_users = set()
        for guild in bot.guilds:
            for vc in guild.voice_channels:
                for member in vc.members:
                    if member.bot or member.voice.self_deaf or member.voice.deaf: continue
                    user_id = str(member.id)
                    if user_id in awarded_users: continue
                    c.execute("SELECT last_vc_reward FROM activity_stats WHERE user_id=%s", (user_id,))
                    row = c.fetchone()
                    if not row or row[0] is None or (now - row[0]).total_seconds() >= 1800:
                        c.execute("INSERT INTO users (user_id, balance) VALUES (%s, 500) ON DUPLICATE KEY UPDATE balance=balance+500", (user_id,))
                        c.execute("INSERT INTO activity_stats (user_id, last_vc_reward) VALUES (%s, %s) ON DUPLICATE KEY UPDATE last_vc_reward=%s", (user_id, now, now))
                        log_transaction(user_id, 500, "語音通話獎勵 (10min)")
                        awarded_users.add(user_id)
        conn.commit(); conn.close()

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    try:
        if not message.guild:
            return
        user_id, now = str(message.author.id), datetime.datetime.now()
        conn = get_db_connection(); c = conn.cursor()
        c.execute("INSERT INTO activity_stats (user_id, msg_count) VALUES (%s, 1) ON DUPLICATE KEY UPDATE msg_count=msg_count+1", (user_id,))
        c.execute("SELECT msg_count, last_msg_reward, last_exp_reward FROM activity_stats WHERE user_id=%s", (user_id,))
        row = c.fetchone()
        if row and (row[2] is None or (now - row[2]).total_seconds() >= EXP_COOLDOWN_SECONDS):
            exp_gain = random.randint(12, 20)
            lv_info = add_user_exp(user_id, exp_gain)
            c.execute("UPDATE activity_stats SET last_exp_reward=%s WHERE user_id=%s", (now, user_id))
            if lv_info and lv_info[1] > lv_info[0]:
                try:
                    await message.channel.send(f"🎉 {message.author.mention} 升到 **Lv.{lv_info[1]}**！")
                except:
                    pass
        if row and row[0] >= 10:
            if row[1] is None or (now - row[1]).total_seconds() >= 1800:
                c.execute("INSERT INTO users (user_id, balance) VALUES (%s, 500) ON DUPLICATE KEY UPDATE balance=balance+500", (user_id,))
                c.execute("UPDATE activity_stats SET msg_count=0, last_msg_reward=%s WHERE user_id=%s", (now, user_id))
                log_transaction(user_id, 500, "聊天活躍獎勵 (10句)")
        conn.commit(); conn.close()
    except Exception as e:
        print(f"❌ on_message 錯誤: {e}")
    finally:
        await bot.process_commands(message)

# --- Slash ---
@bot.tree.command(name="register", description="獲得 50,000 啟動資金")
async def register(interaction: discord.Interaction):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("INSERT IGNORE INTO users (user_id, balance) VALUES (%s, 50000)", (str(interaction.user.id),))
    if c.rowcount == 0: await interaction.response.send_message("你註冊過了!", ephemeral=True)
    else: log_transaction(interaction.user.id, 50000, "註冊獎勵"); await interaction.response.send_message("註冊成功，獲得 50,000 東雲幣！")
    conn.commit(); conn.close()

@bot.tree.command(name="daily", description="每日簽到領取 100,000 東雲幣")
async def daily(interaction: discord.Interaction):
    stats = get_user_stats(interaction.user.id)
    if not stats: return await interaction.response.send_message("未註冊", ephemeral=True)
    lv_row = get_level_stats(interaction.user.id)
    level_num = int(lv_row[1] or 1) if lv_row else 1
    daily_reward = 100000 + level_num * 1000
    # 設定台灣時間 (UTC+8)
    tz = datetime.timezone(datetime.timedelta(hours=8))
    now_tw = datetime.datetime.now(tz)
    today_tw = now_tw.date()
    
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT last_claim FROM daily_claims WHERE user_id=%s", (str(interaction.user.id),))
    row = c.fetchone()
    
    if row and row[0] == today_tw:
        tomorrow_tw = today_tw + datetime.timedelta(days=1)
        next_claim_dt = datetime.datetime.combine(tomorrow_tw, datetime.time.min, tzinfo=tz)
        ts = int(next_claim_dt.timestamp())
        conn.close()
        return await interaction.response.send_message(f"⚠️ 你今天已經簽到過囉！下次簽到時間：<t:{ts}:F> (<t:{ts}:R>)", ephemeral=True)
        
    c.execute("INSERT INTO daily_claims (user_id, last_claim) VALUES (%s, %s) ON DUPLICATE KEY UPDATE last_claim=%s", (str(interaction.user.id), today_tw, today_tw))
    c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (daily_reward, str(interaction.user.id)))
    conn.commit(); conn.close(); log_transaction(interaction.user.id, daily_reward, "每日簽到")
    new_bal = get_user_stats(interaction.user.id)[0]
    
    # 計算下一次領取時間
    tomorrow_tw = today_tw + datetime.timedelta(days=1)
    next_claim_dt = datetime.datetime.combine(tomorrow_tw, datetime.time.min, tzinfo=tz)
    ts = int(next_claim_dt.timestamp())
    await interaction.response.send_message(
        f"🎉 簽到成功！獲得 **{daily_reward}** 東雲幣！（Lv.{level_num} 加成）目前餘額：`{new_bal}`\n"
        f"下次領取時間：<t:{ts}:f> (<t:{ts}:R>)"
    )

@bot.tree.command(name="hourly", description="每小時簽到（可依等級累積）")
async def hourly(interaction: discord.Interaction):
    stats = get_user_stats(interaction.user.id)
    if not stats:
        return await interaction.response.send_message("未註冊", ephemeral=True)
    bank_info = refresh_hourly_bank(interaction.user.id)
    if not bank_info:
        return await interaction.response.send_message("資料初始化失敗", ephemeral=True)
    level_num = bank_info["level"]
    bank = bank_info["bank"]
    reward_per_slot = 1000 + level_num * 100
    if bank <= 0:
        sec = bank_info["next_in_seconds"]
        mins = max(1, int(sec // 60))
        return await interaction.response.send_message(
            f"⏳ 目前尚無可領時段。下次可累積約 `{mins}` 分鐘後。\n"
            f"你目前 Lv.{level_num}，最多可累積 `{level_num}` 小時。",
            ephemeral=True
        )
    payout = payout_hourly_bank(interaction.user.id, bank, reward_per_slot)
    new_bal = get_user_stats(interaction.user.id)[0]
    await interaction.response.send_message(
        f"🕒 已領取每小時簽到！\n"
        f"累積時段：`{bank}` 小時（上限 `{level_num}`）\n"
        f"每小時獎勵：`{reward_per_slot}`\n"
        f"本次共獲得：`{payout}` 東雲幣\n"
        f"目前餘額：`{new_bal}`"
    )

@bot.tree.command(name="beg", description="街頭乞討")
async def beg(interaction: discord.Interaction):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT balance, last_beg FROM users WHERE user_id=%s", (str(interaction.user.id),))
    row = c.fetchone()
    if not row: return await interaction.response.send_message("未註冊", ephemeral=True)
    now = datetime.datetime.now()
    if row[1] and (now - row[1]).total_seconds() < 120: return await interaction.response.send_message("太快了", ephemeral=True)
    earn = random.randint(100, 600)
    if random.random() < 0.3: await interaction.response.send_message("沒人鳥你 乞丐"); c.execute("UPDATE users SET last_beg=%s WHERE user_id=%s", (now, str(interaction.user.id)))
    else: c.execute("UPDATE users SET balance=balance+%s, last_beg=%s WHERE user_id=%s", (earn, now, str(interaction.user.id))); log_transaction(interaction.user.id, earn, "乞討所得"); await interaction.response.send_message(f"你被施捨了！獲得 {earn} 元")
    conn.commit(); conn.close()

@bot.tree.command(name="rescue", description="[極致救濟] 餘額為 0 元時可領 1,000 (每人限領 10 次)")
async def rescue(interaction: discord.Interaction):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT balance, last_rescue, rescue_count FROM users WHERE user_id=%s", (str(interaction.user.id),))
    row = c.fetchone()
    if not row: return await interaction.response.send_message("尚未註冊", ephemeral=True)
    if row[0] > 0: return await interaction.response.send_message(f"💰 你還有一點尊嚴（餘額: {row[0]}），請自力更生！完全歸零時再來領。", ephemeral=True)
    if row[2] >= 10: return await interaction.response.send_message("🚫 抱歉，你的救濟次數已達 10 次上限。這輩子不能再領了，去跟朋友借吧！", ephemeral=True)
    
    now = datetime.datetime.now()
    if row[1] and (now - row[1]).total_seconds() < 3600:
        rem = 3600 - (now - row[1]).total_seconds()
        return await interaction.response.send_message(f"🕒 救助站正在休息中！請再等 `{int(rem//60)}` 分鐘。", ephemeral=True)
        
    c.execute("UPDATE users SET balance=balance+1000, last_rescue=%s, rescue_count=rescue_count+1 WHERE user_id=%s", (now, str(interaction.user.id)))
    conn.commit(); conn.close(); log_transaction(interaction.user.id, 1000, "終極破產救濟")
    await interaction.response.send_message(f"🚑 貧窮救濟金已發放！獲得 **1,000** 東雲幣。這是你第 `{row[2]+1}/10` 次領取。")

@bot.tree.command(name="bj", description="開始 21 點")
@app_commands.describe(bet="注額")
async def bj(interaction: discord.Interaction, bet: int = 1000):
    if not IS_EVENT_ACTIVE: return await interaction.response.send_message("打烊", ephemeral=True)
    if bet < 100: return await interaction.response.send_message("低消 100", ephemeral=True)
    if get_user_stats(interaction.user.id) is None: return await interaction.response.send_message("未註冊", ephemeral=True)
    sv = SetupView(interaction.user, bet); await interaction.response.send_message(embed=sv.build_embed(), view=sv)

@bot.tree.command(name="balance", description="查詢個人的戰績與餘額")
async def balance(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user; stats = get_user_stats(target.id)
    if not stats: return await interaction.response.send_message("未註冊", ephemeral=True)
    bal, total, wins, t_prof = stats
    wr = (wins/total*100) if total > 0 else 0
    msg = f"📊 **{target.mention} 的統計資料**\n"
    msg += f"💰 目前餘額：`{bal}`\n"
    msg += f"🎲 總遊玩局數：`{total}` 局\n"
    msg += f"🏆 勝利場次：`{wins}` 場\n"
    msg += f"📈 勝率：`{wr:.1f}%`\n"
    msg += f"💸 歷史總盈虧：`{t_prof}`"
    await interaction.response.send_message(msg)

@bot.tree.command(name="level", description="查詢等級與經驗值")
async def level(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    lv_row = get_level_stats(target.id)
    if not lv_row:
        return await interaction.response.send_message("未註冊", ephemeral=True)
    exp = int(lv_row[0] or 0)
    level_num = int(lv_row[1] or 1)
    calc_lv, cur_progress, next_need = calc_level_from_exp(exp)
    level_num = max(level_num, calc_lv)
    if level_num >= MAX_LEVEL:
        text = f"🏅 {target.mention} 目前 **Lv.{level_num}**（已滿級）\n✨ 總 EXP：`{exp}`"
    else:
        text = (
            f"🏅 {target.mention} 目前 **Lv.{level_num}**\n"
            f"✨ 總 EXP：`{exp}`\n"
            f"📈 升級進度：`{cur_progress}/{next_need}`"
        )
    await interaction.response.send_message(text)

@bot.tree.command(name="transfer", description="轉帳給其他玩家")
@app_commands.describe(member="要轉帳給誰", amount="轉帳金額")
async def transfer(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        return await interaction.response.send_message("金額必須大於 0", ephemeral=True)
    if member.bot:
        return await interaction.response.send_message("不能轉帳給機器人", ephemeral=True)
    if member.id == interaction.user.id:
        return await interaction.response.send_message("不能轉帳給自己", ephemeral=True)
    if get_user_stats(interaction.user.id) is None:
        return await interaction.response.send_message("你還沒註冊，請先使用 /register", ephemeral=True)
    if get_user_stats(member.id) is None:
        return await interaction.response.send_message("對方尚未註冊", ephemeral=True)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
        (amount, str(interaction.user.id), amount)
    )
    if c.rowcount == 0:
        conn.close()
        return await interaction.response.send_message("餘額不足，無法轉帳", ephemeral=True)

    c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, str(member.id)))
    conn.commit()
    conn.close()

    log_transaction(interaction.user.id, -amount, f"轉帳給 {member.id}")
    log_transaction(member.id, amount, f"收到 {interaction.user.id} 的轉帳")
    await interaction.response.send_message(f"✅ 已轉帳 **{amount}** 給 {member.mention}")

@bot.tree.command(name="redpacket", description="[管理員] 發送可搶紅包")
@app_commands.describe(total_amount="紅包總金額", count="份數", seconds="有效秒數(最少10秒)")
async def redpacket(interaction: discord.Interaction, total_amount: int, count: int, seconds: int = 60):
    if interaction.user.id not in ALLOWED_HOST_IDS:
        return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
    if total_amount < count or total_amount <= 0:
        return await interaction.response.send_message("總金額需大於 0，且至少要能每包 1 元。", ephemeral=True)
    if count < 1 or count > 100:
        return await interaction.response.send_message("份數需介於 1 到 100。", ephemeral=True)
    timeout_seconds = max(RED_PACKET_MIN_SECONDS, seconds)
    view = RedPacketView(interaction.user.id, total_amount, count)
    view.timeout = timeout_seconds
    await interaction.response.send_message(
        f"{interaction.user.mention} 發了一個紅包！\n{view.summary_text()}\n"
        f"⏰ 有效時間：`{timeout_seconds}` 秒",
        view=view
    )
    try:
        view.message = await interaction.original_response()
    except:
        pass

@stock_group.command(name="quote", description="查詢台股個股資訊")
@app_commands.describe(symbol="股票代號或名稱")
async def stock_quote(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(thinking=True)
    try:
        rows = await fetch_stock_day_all()
    except Exception as e:
        return await interaction.followup.send(f"台股資料暫時無法取得：{e}", ephemeral=True)

    key = symbol.strip().upper()
    picked = None
    for row in rows:
        code = str(row.get("Code", "")).upper()
        name = str(row.get("Name", ""))
        if code == key or name == symbol.strip():
            picked = row
            break
    if picked is None:
        for row in rows:
            code = str(row.get("Code", "")).upper()
            name = str(row.get("Name", ""))
            if key in code or symbol.strip() in name:
                picked = row
                break
    if picked is None:
        return await interaction.followup.send("找不到該股票代號/名稱", ephemeral=True)

    code = str(picked.get("Code", "")).strip()
    try:
        quotes = await fetch_mis_quotes(build_mis_channels_for_code(code))
    except Exception as e:
        return await interaction.followup.send(f"即時資料暫時無法取得：{e}", ephemeral=True)
    real = None
    for q in quotes:
        if q["code"] == code and q["price"] > 0:
            real = q
            break
    if real is None and quotes:
        real = quotes[0]
    if not real or real["price"] <= 0:
        return await interaction.followup.send("目前查無即時成交資訊", ephemeral=True)

    open_price = to_float(picked.get("OpeningPrice", "0"))
    high_price = to_float(picked.get("HighestPrice", "0"))
    low_price = to_float(picked.get("LowestPrice", "0"))
    ref_price = real["prev_close"]
    color = 0x2ecc71 if real["change"] > 0 else (0xe74c3c if real["change"] < 0 else 0x95a5a6)
    embed = discord.Embed(title=f"📈 {picked.get('Name')} ({picked.get('Code')})", color=color)
    embed.add_field(name="即時價", value=f"`{real['price']:.2f}`")
    embed.add_field(name="漲跌", value=f"`{real['change']:+.2f}` ({real['pct']:+.2f}%)")
    embed.add_field(name="成交量", value=f"`{real['volume']:,}`")
    embed.add_field(name="開盤", value=f"`{open_price:.2f}`")
    embed.add_field(name="最高 / 最低", value=f"`{high_price:.2f}` / `{low_price:.2f}`")
    embed.add_field(name="昨收(參考)", value=f"`{ref_price:.2f}`")
    embed.set_footer(text=f"即時來源: TWSE MIS | 時間: {real['time'] or 'N/A'}")
    await interaction.followup.send(embed=embed)

@stock_group.command(name="movers", description="台股漲跌排行")
@app_commands.describe(top_n="排行筆數(1-10)")
async def stock_movers(interaction: discord.Interaction, top_n: int = 5):
    await interaction.response.defer(thinking=True)
    top_n = max(1, min(10, top_n))
    try:
        rows = await fetch_stock_day_all()
    except Exception as e:
        return await interaction.followup.send(f"台股資料暫時無法取得：{e}", ephemeral=True)

    # 先挑成交值前段，避免一次抓全市場即時資料造成過重負載
    candidates = []
    for row in rows:
        trade_value = to_float(row.get("TradeValue", "0"))
        if trade_value > 0:
            candidates.append((trade_value, str(row.get("Code", "")).strip(), str(row.get("Name", "")).strip()))
    candidates = sorted(candidates, key=lambda x: x[0], reverse=True)[:60]
    channels = []
    code_name_map = {}
    for _, code, name in candidates:
        if not code:
            continue
        code_name_map[code] = name
        channels.extend(build_mis_channels_for_code(code))
    try:
        quotes = await fetch_mis_quotes(channels)
    except Exception as e:
        return await interaction.followup.send(f"即時資料暫時無法取得：{e}", ephemeral=True)

    best_by_code = {}
    for q in quotes:
        code = q["code"]
        if not code:
            continue
        prev = best_by_code.get(code)
        if prev is None or q["price"] > prev["price"]:
            best_by_code[code] = q

    scored = []
    for code, q in best_by_code.items():
        if q["price"] <= 0 or q["prev_close"] <= 0:
            continue
        q["name"] = code_name_map.get(code, q["name"] or code)
        scored.append((q["pct"], q))
    if not scored:
        return await interaction.followup.send("目前無法計算排行", ephemeral=True)

    gainers = sorted(scored, key=lambda x: x[0], reverse=True)[:top_n]
    losers = sorted(scored, key=lambda x: x[0])[:top_n]
    up_text = "\n".join([f"{i+1}. {r['code']} {r['name']} `{p:+.2f}%`" for i, (p, r) in enumerate(gainers)])
    down_text = "\n".join([f"{i+1}. {r['code']} {r['name']} `{p:+.2f}%`" for i, (p, r) in enumerate(losers)])
    embed = discord.Embed(title="📊 台股漲跌排行", color=0x2b2d31)
    embed.add_field(name="漲幅前段", value=up_text or "無資料", inline=False)
    embed.add_field(name="跌幅前段", value=down_text or "無資料", inline=False)
    embed.set_footer(text="即時來源: TWSE MIS（熱門成交值樣本）")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="say", description="[管理員] 指定機器人對特定頻道發送內容")
@app_commands.describe(text="你要機器人說什麼？", channel="指定發送到哪個頻道？(選填)")
@app_commands.default_permissions(manage_messages=True)
async def say_slash(interaction: discord.Interaction, text: str, channel: discord.TextChannel = None):
    if interaction.user.id not in ALLOWED_HOST_IDS:
        return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
    target_channel = channel or interaction.channel
    await target_channel.send(text)
    await interaction.response.send_message(f"✅ 訊息已發送到 {target_channel.mention}！", ephemeral=True)

@bot.tree.command(name="record", description="最後 10 筆紀錄")
async def record_cmd(interaction: discord.Interaction):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT amount, reason, created_at FROM logs WHERE user_id=%s ORDER BY created_at DESC LIMIT 10", (str(interaction.user.id),))
    rows = c.fetchall(); conn.close()
    if not rows: return await interaction.response.send_message("無紀錄", ephemeral=True)
    msg = "\n".join([f"[{r[2].strftime('%H:%M')}] {r[1]}: `{r[0]}`" for r in rows]); await interaction.response.send_message(msg)

@bot.tree.command(name="leaderboard", description="前 10 名")
async def leaderboard(interaction: discord.Interaction):
    conn = get_db_connection(); c = conn.cursor(); c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 10"); data = c.fetchall(); conn.close()
    msg = "\n".join([f"{i+1}. <@{uid}>: {bal}" for i, (uid, bal) in enumerate(data)]); await interaction.response.send_message(embed=discord.Embed(title="🏆 排行榜", description=msg))

# Admin
@bot.command()
@is_host()
async def give(ctx, member: discord.Member, amount: int):
    conn = get_db_connection(); c = conn.cursor(); c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, str(member.id))); conn.commit(); conn.close(); log_transaction(member.id, amount, "管理員發放"); await ctx.send(f"老闆發錢啦！已發放 **{amount}** 東雲幣給 {member.mention}！")

@bot.command()
@is_host()
async def ban(ctx, member: discord.Member):
    conn = get_db_connection(); c = conn.cursor(); c.execute("INSERT IGNORE INTO blacklist (user_id) VALUES (%s)", (str(member.id),)); conn.commit(); conn.close(); await ctx.send("黑名單")

@bot.command()
@is_host()
async def take(ctx, member: discord.Member, amount: int):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("UPDATE users SET balance=GREATEST(0, balance-%s) WHERE user_id=%s", (amount, str(member.id)))
    conn.commit(); conn.close(); log_transaction(member.id, -amount, "管理員扣除"); await ctx.send(f"💸 已從 {member.mention} 帳戶扣除 **{amount}**！")

@bot.command()
@is_host()
async def unban(ctx, member: discord.Member):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("DELETE FROM blacklist WHERE user_id=%s", (str(member.id),))
    conn.commit(); conn.close(); await ctx.send(f"✅ {member.mention} 已解鎖。")

@bot.command()
@is_host()
async def resetall_zero(ctx):
    conn = get_db_connection(); c = conn.cursor(); c.execute("UPDATE users SET balance=0")
    conn.commit(); conn.close(); await ctx.send("💥 經濟大崩潰：全伺服器帳戶餘額已清零！")

@bot.command()
@is_host()
async def resetall_default(ctx):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("UPDATE users SET balance=50000, rescue_count=0, total_games=0, wins=0, total_profit=0")
    conn.commit(); conn.close(); await ctx.send("🔄 已經為所有人重新發放 50,000 啟動資金，並重置所有統計數據。")

@bot.command()
@is_host()
async def adminhelp(ctx):
    help_text = """**👑 賭場管理員密令清單**
`!give @玩家 <數量>` - 老闆發錢
`!take @玩家 <數量>` - 扣除資金
`!ban @玩家` - 設為黑名單
`!unban @玩家` - 解除黑名單
`!lock` - 暫停/開放賭場營業
`!resetall_zero` - [危險] 全服餘額清零
`!resetall_default` - [重置] 全服重置為 50,000
`/say text:內容 channel:#頻道` - 代位發聲(斜線指令)"""
    await ctx.send(help_text)

@bot.command()
@is_host()
async def lock(ctx):
    global IS_EVENT_ACTIVE; IS_EVENT_ACTIVE = not IS_EVENT_ACTIVE; await ctx.send(f"狀態: {IS_EVENT_ACTIVE}")

bot.run(os.getenv('DISCORD_TOKEN'))