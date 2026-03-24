import discord
from discord.ext import commands
import random
import pymysql
import os
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# ⚙️ 系統設定與全局變數
# ==========================================
ALLOWED_HOST_IDS = [531308526262550528]  # ⚠️ 填入你的 Discord ID
SIDE_BET_RATIO = 0.5                     # 側注上限 (主注的 50%)
IS_EVENT_ACTIVE = True                   # 賭場狀態

def is_host():
    def predicate(ctx): return ctx.author.id in ALLOWED_HOST_IDS
    return commands.check(predicate)

# ==========================================
# 🗄️ 1. 資料庫系統 (MySQL)
# ==========================================
def get_db_connection():
    return pymysql.connect(
        host=os.getenv('DB_HOST'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASS'),
        database=os.getenv('DB_NAME'),
        charset='utf8mb4'
    )

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (user_id VARCHAR(255) PRIMARY KEY, balance BIGINT, rescue_count INT DEFAULT 0,
                  total_games INT DEFAULT 0, wins INT DEFAULT 0, total_profit BIGINT DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS blacklist (user_id VARCHAR(255) PRIMARY KEY)''')
    conn.commit()
    conn.close()

def get_user_stats(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance, total_games, wins, total_profit FROM users WHERE user_id=%s", (str(user_id),))
    res = c.fetchone()
    conn.close()
    return res

def update_game_result(user_id, profit, is_win):
    conn = get_db_connection()
    c = conn.cursor()
    win_int = 1 if is_win else 0
    c.execute("UPDATE users SET balance=balance+%s, total_profit=total_profit+%s, total_games=total_games+1, wins=wins+%s WHERE user_id=%s",
              (profit, profit, win_int, str(user_id)))
    conn.commit()
    conn.close()

# ==========================================
# 🃏 2. 核心遊戲邏輯 (6副牌)
# ==========================================
def get_deck(num_decks=6):
    suits = ['♥️', '♦️', '♣️', '♠️']
    ranks = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    return [{'rank': r, 'suit': s} for s in suits for r in ranks] * num_decks

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

# --- 側注判定 ---
def check_sidebets(player_hand, dealer_up, p_bet, s_bet):
    res_msg, total_p = "", 0
    # 對子
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
    # 21+3
    if s_bet > 0:
        cards = [player_hand[0], player_hand[1], dealer_up]
        suits = [c['suit'] for c in cards]
        rv = {'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,'10':10,'J':11,'Q':12,'K':13,'A':14}
        v = sorted([rv[c['rank']] for c in cards])
        if v == [2,3,14]: v = [1,2,3]
        f, s, t = len(set(suits))==1, (v[2]-v[1]==1 and v[1]-v[0]==1), len(set([c['rank'] for c in cards]))==1
        if f and t: mult, m = 50, "同花三條"
        elif f and s: mult, m = 25, "同花順"
        elif t: mult, m = 25, "三條"
        elif s: mult, m = 10, "順子"
        elif f: mult, m = 5, "同花"
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
class SetupView(discord.ui.View):
    def __init__(self, user, base_bet):
        super().__init__(timeout=90)
        self.user, self.base_bet = user, base_bet
        self.p_bet, self.s_bet = 0, 0
        self.max_side = int(base_bet * SIDE_BET_RATIO)

    def build_embed(self, err=""):
        stats = get_user_stats(self.user.id)
        embed = discord.Embed(title="🃏 21點 — 旁注設定", color=0x2b2d31)
        embed.description = f"{'❌ ' + err + '\n' if err else ''}主注：`{self.base_bet}`\n旁注剩餘額度：**`{self.max_side - (self.p_bet + self.s_bet)}`**"
        embed.add_field(name="🧧 對子", value=f"`{self.p_bet}`", inline=True)
        embed.add_field(name="🎯 21+3", value=f"`{self.s_bet}`", inline=True)
        return embed

    @discord.ui.button(label="對子 +200", style=discord.ButtonStyle.primary)
    async def add_p(self, inter, btn):
        if inter.user.id != self.user.id: return
        if self.p_bet + self.s_bet + 200 > self.max_side:
            return await inter.response.edit_message(embed=self.build_embed("超過旁注上限"))
        self.p_bet += 200
        await inter.response.edit_message(embed=self.build_embed())

    @discord.ui.button(label="21+3 +200", style=discord.ButtonStyle.primary)
    async def add_s(self, inter, btn):
        if inter.user.id != self.user.id: return
        if self.p_bet + self.s_bet + 200 > self.max_side:
            return await inter.response.edit_message(embed=self.build_embed("超過旁注上限"))
        self.s_bet += 200
        await inter.response.edit_message(embed=self.build_embed())

    @discord.ui.button(label="開始遊戲", style=discord.ButtonStyle.success)
    async def start(self, inter, btn):
        if inter.user.id != self.user.id: return
        if get_user_stats(self.user.id)[0] < (self.base_bet + self.p_bet + self.s_bet):
            return await inter.response.send_message("餘額不足", ephemeral=True)
        self.stop(); await inter.message.delete()
        gv = BlackjackGame(self.user, self.base_bet, self.p_bet, self.s_bet)
        await inter.channel.send(embed=gv.build_embed(), view=gv)

class BlackjackGame(discord.ui.View):
    def __init__(self, user, bet, p_bet, s_bet):
        super().__init__(timeout=90)
        self.user, self.bet, self.p_bet, self.s_bet = user, bet, p_bet, s_bet
        self.deck = get_deck()
        random.shuffle(self.deck)
        self.p_hand = [self.deck.pop(), self.deck.pop()]
        self.d_hand = [self.deck.pop(), self.deck.pop()]
        # 結算旁注
        self.side_p, self.side_m = check_sidebets(self.p_hand, self.d_hand[0], p_bet, s_bet)
        if self.side_p != 0: update_game_result(user.id, self.side_p, self.side_p > 0)

    def build_embed(self, done=False, res="", profit=0):
        stats = get_user_stats(self.user.id)
        bal, total, wins, t_prof = stats
        wr = (wins/total*100) if total>0 else 0
        embed = discord.Embed(title="🃏 21點大賽", color=0x2b2d31)
        embed.description = f"餘額：{bal} | 勝率：{wr:.1f}% | 總盈虧：{t_prof}"
        p_cards = ' '.join([f"[{c['rank']}{c['suit']}]" for c in self.p_hand])
        embed.add_field(name="👤 你的手牌", value=f"{p_cards}\n點數：{calculate_score(self.p_hand)}", inline=False)
        if done:
            d_cards = ' '.join([f"[{c['rank']}{c['suit']}]" for c in self.d_hand])
            embed.add_field(name="🤖 莊家手牌", value=f"{d_cards}\n點數：{calculate_score(self.d_hand)}", inline=False)
            embed.add_field(name="🏆 結果", value=f"{res}\n{self.side_m}", inline=False)
        else:
            embed.add_field(name="🤖 莊家手牌", value=f"[{self.d_hand[0]['rank']}{self.d_hand[0]['suit']}] [❓]", inline=False)
        return embed

    async def end(self, inter, res, prof, win=False):
        for c in self.children: c.disabled = True
        update_game_result(self.user.id, prof, win)
        nv = NewGameView(self.user, self.bet, get_user_stats(self.user.id)[0])
        await inter.response.edit_message(embed=self.build_embed(True, res, prof), view=nv)

    @discord.ui.button(label="要牌", style=discord.ButtonStyle.success)
    async def hit(self, inter, btn):
        if inter.user.id != self.user.id: return
        self.p_hand.append(self.deck.pop())
        self.children[2].disabled = True # 抽牌後不能投降
        ps = calculate_score(self.p_hand)
        if ps > 21: await self.end(inter, "爆牌輸了", -self.bet)
        elif len(self.p_hand) == 5: # 過五關
            await self.end(inter, "🐉 過五關！2.5倍獎勵", int(self.bet*1.5), True)
        else: await inter.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="停牌", style=discord.ButtonStyle.danger)
    async def stand(self, inter, btn):
        if inter.user.id != self.user.id: return
        while calculate_score(self.d_hand) < 17: self.d_hand.append(self.deck.pop())
        ps, ds = calculate_score(self.p_hand), calculate_score(self.d_hand)
        if ds > 21 or ps > ds: await self.end(inter, "你贏了！", self.bet, True)
        elif ps < ds: await self.end(inter, "你輸了", -self.bet)
        else: await self.end(inter, "平手", 0)

    @discord.ui.button(label="投降", style=discord.ButtonStyle.secondary)
    async def surrender(self, inter, btn):
        if inter.user.id != self.user.id: return
        await self.end(inter, "投降輸一半", -(self.bet//2))

class NewGameView(discord.ui.View):
    def __init__(self, user, last_bet, bal):
        super().__init__(timeout=90)
        self.user, self.last_bet, self.bal = user, last_bet, bal
    @discord.ui.button(label="再一局", style=discord.ButtonStyle.success)
    async def again(self, inter, btn):
        if inter.user.id != self.user.id: return
        self.stop(); await inter.message.delete()
        setup = SetupView(self.user, self.last_bet)
        await inter.channel.send(embed=setup.build_embed(), view=setup)

# ==========================================
# 🤖 4. 指令系統
# ==========================================
intents = discord.Intents.default(); intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready(): init_db(); print(f"✅ {bot.user} 啟動")

@bot.command(aliases=['註冊'])
async def register(ctx):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("INSERT IGNORE INTO users (user_id, balance) VALUES (%s, 50000)", (str(ctx.author.id),))
    conn.commit(); conn.close()
    await ctx.send(f"🎉 {ctx.author.mention} 註冊成功，獲得 50,000 幣！")

@bot.command()
async def bj(ctx, bet: int = 1000):
    if not IS_EVENT_ACTIVE: return await ctx.send("打烊了")
    if bet < 100: return await ctx.send("低消 100")
    if get_user_stats(ctx.author.id) is None: return await ctx.send("請先註冊")
    sv = SetupView(ctx.author, bet)
    await ctx.send(embed=sv.build_embed(), view=sv)

@bot.command(aliases=['lb'])
async def leaderboard(ctx):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 10")
    data = c.fetchall(); conn.close()
    msg = "\n".join([f"{i+1}. <@{uid}>: {bal}" for i, (uid, bal) in enumerate(data)])
    await ctx.send(embed=discord.Embed(title="🏆 排行榜", description=msg))

# --- 管理員指令 ---
@bot.command()
@is_host()
async def lock(ctx):
    global IS_EVENT_ACTIVE
    IS_EVENT_ACTIVE = not IS_EVENT_ACTIVE
    await ctx.send(f"賭場狀態：{'營業中' if IS_EVENT_ACTIVE else '已打烊'}")

bot.run(os.getenv('DISCORD_TOKEN'))