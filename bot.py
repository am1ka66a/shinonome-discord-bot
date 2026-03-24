import discord
from discord import app_commands
from discord.ext import commands
import random
import pymysql
import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# ⚙️ 系統設定與全局變數
# ==========================================
ALLOWED_HOST_IDS = [531308526262550528]  # ⚠️ 填入你的 Discord ID
SIDE_BET_RATIO = 0.5                     # 側注上限 (主注的 50%)
IS_EVENT_ACTIVE = True                   # 賭場狀態

def is_app_host():
    def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id in ALLOWED_HOST_IDS
    return app_commands.check(predicate)

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
        if stats[0] < (b + p + s):
            return await interaction.response.send_message(f"餘額不足！你目前有 {stats[0]} 幣", ephemeral=True)

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

    def build_embed(self, err=""):
        stats = get_user_stats(self.user.id)
        embed = discord.Embed(title="🃏 21點 — 下注設定", color=0x2b2d31)
        embed.description = f"{'❌ ' + err + '\n' if err else ''}主注：`{self.base_bet}`\n旁注剩餘額度：**`{self.max_side - (self.p_bet + self.s_bet)}`**\n你的餘額：`{stats[0]}`"
        embed.add_field(name="🧧 對子旁注 (賠率: 同花30倍/混合5倍)", value=f"`{self.p_bet}`", inline=False)
        embed.add_field(name="🎯 21+3旁注 (賠率: 順/同花/三條 5~50倍)", value=f"`{self.s_bet}`", inline=False)
        return embed

    @discord.ui.button(label="自訂下注金額", style=discord.ButtonStyle.primary)
    async def custom_bet(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.send_modal(BetModal(self))

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

    def build_embed(self, done=False, res="", profit=0, animating=False):
        stats = get_user_stats(self.user.id)
        bal, total, wins, t_prof = stats
        wr = (wins/total*100) if total>0 else 0
        embed = discord.Embed(title="🃏 21點大賽", color=0x2b2d31)
        embed.description = f"目前餘額：{bal} | 勝率：{wr:.1f}% | 總盈虧：{t_prof}"
        p_cards = ' '.join([f"[{c['rank']}{c['suit']}]" for c in self.p_hand])
        embed.add_field(name="👤 你的手牌", value=f"{p_cards}\n點數：{calculate_score(self.p_hand)}", inline=False)
        if done or animating:
            d_cards = ' '.join([f"[{c['rank']}{c['suit']}]" for c in self.d_hand])
            embed.add_field(name="🤖 莊家手牌", value=f"{d_cards}\n點數：{calculate_score(self.d_hand)}", inline=False)
            if done:
                total_profit = profit + self.side_p
                res_text = f"**{res}**\n{self.side_m}\n"
                if total_profit > 0: res_text += f"\n📈 本局總計：`+{total_profit}` 幣"
                elif total_profit < 0: res_text += f"\n📉 本局總計：`{total_profit}` 幣"
                else: res_text += f"\n➖ 本局無輸贏"
                res_text += f"\n💰 最新餘額：`{bal}` 幣"
                embed.add_field(name="🏆 結果", value=res_text, inline=False)
        else:
            embed.add_field(name="🤖 莊家手牌", value=f"[{self.d_hand[0]['rank']}{self.d_hand[0]['suit']}] [❓]", inline=False)
        return embed

    async def end(self, inter, res, prof, win=False, deferred=False):
        for c in self.children: c.disabled = True
        update_game_result(self.user.id, prof, win)
        nv = NewGameView(self.user, self.bet, self.p_bet, self.s_bet, get_user_stats(self.user.id)[0])
        emb = self.build_embed(True, res, prof)
        if deferred: await inter.message.edit(embed=emb, view=nv)
        else: await inter.response.edit_message(embed=emb, view=nv)

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
        await inter.response.defer()
        for c in self.children: c.disabled = True
        await inter.message.edit(view=self)
        
        await inter.message.edit(embed=self.build_embed(done=False, animating=True))
        await asyncio.sleep(1.2)

        while calculate_score(self.d_hand) < 17:
            self.d_hand.append(self.deck.pop())
            await inter.message.edit(embed=self.build_embed(done=False, animating=True))
            await asyncio.sleep(1.2)

        ps, ds = calculate_score(self.p_hand), calculate_score(self.d_hand)
        if ds > 21 or ps > ds: await self.end(inter, "🎉 你贏了！", self.bet, True, deferred=True)
        elif ps < ds: await self.end(inter, "💀 你輸了", -self.bet, False, deferred=True)
        else: await self.end(inter, "🤝 平手", 0, False, deferred=True)

    @discord.ui.button(label="投降", style=discord.ButtonStyle.secondary)
    async def surrender(self, inter, btn):
        if inter.user.id != self.user.id: return
        await self.end(inter, "投降輸一半", -(self.bet//2))

class ConfirmAllInView(discord.ui.View):
    def __init__(self, user, parent_msg):
        super().__init__(timeout=30)
        self.user = user
        self.parent_msg = parent_msg

    @discord.ui.button(label="確定 All In！", style=discord.ButtonStyle.danger)
    async def confirm(self, inter, btn):
        if inter.user.id != self.user.id: return
        self.stop()
        try: await self.parent_msg.delete()
        except: pass
        stats = get_user_stats(self.user.id)
        if stats[0] < 100:
            return await inter.response.edit_message(content="餘額不足 100，無法遊戲", view=None)
        
        setup = SetupView(self.user, stats[0], 0, 0)
        await inter.channel.send(embed=setup.build_embed(), view=setup)
        await inter.response.edit_message(content="✅ 已進入 All In 模式", view=None)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter, btn):
        if inter.user.id != self.user.id: return
        self.stop()
        await inter.response.edit_message(content="❌ 已取消", view=None)

class NewGameView(discord.ui.View):
    def __init__(self, user, last_bet, last_p_bet, last_s_bet, bal):
        super().__init__(timeout=90)
        self.user, self.last_bet, self.bal = user, last_bet, bal
        self.last_p_bet, self.last_s_bet = last_p_bet, last_s_bet

    @discord.ui.button(label="再一局", style=discord.ButtonStyle.success)
    async def again(self, inter, btn):
        if inter.user.id != self.user.id: return
        self.stop(); await inter.message.delete()
        setup = SetupView(self.user, self.last_bet, self.last_p_bet, self.last_s_bet)
        await inter.channel.send(embed=setup.build_embed(), view=setup)

    @discord.ui.button(label="雙倍再局 (Double)", style=discord.ButtonStyle.primary)
    async def double_again(self, inter, btn):
        if inter.user.id != self.user.id: return
        self.stop(); await inter.message.delete()
        new_bet = self.last_bet * 2
        setup = SetupView(self.user, new_bet, self.last_p_bet, self.last_s_bet)
        await inter.channel.send(embed=setup.build_embed(), view=setup)

    @discord.ui.button(label="All In (全押主注)", style=discord.ButtonStyle.danger)
    async def all_in(self, inter, btn):
        if inter.user.id != self.user.id: return
        cv = ConfirmAllInView(self.user, inter.message)
        await inter.response.send_message("⚠️ 警告：你確定要把所有的財產全部押在主注嗎？輸了就什麼都沒了喔！", view=cv, ephemeral=True)

# ==========================================
# 🤖 4. 指令系統
# ==========================================
intents = discord.Intents.default(); intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()
    print(f"✅ {bot.user} 啟動並已同步斜線指令")

@bot.tree.command(name="register", description="註冊你的帳號並獲得 50,000 啟動資金")
async def register(interaction: discord.Interaction):
    if is_blacklisted(interaction.user.id): return await interaction.response.send_message("🚫 黑名單玩家無法註冊！", ephemeral=True)
    conn = get_db_connection(); c = conn.cursor()
    c.execute("INSERT IGNORE INTO users (user_id, balance) VALUES (%s, 50000)", (str(interaction.user.id),))
    if c.rowcount == 0:
        await interaction.response.send_message(f"⚠️ {interaction.user.mention} 你已經註冊過了！", ephemeral=True)
    else:
        await interaction.response.send_message(f"🎉 {interaction.user.mention} 註冊成功，獲得 50,000 幣！")
    conn.commit(); conn.close()

@bot.tree.command(name="bj", description="開始一場 21點對決")
@app_commands.describe(bet="你想要下注的金額 (預設 1000)")
async def bj(interaction: discord.Interaction, bet: int = 1000):
    if not IS_EVENT_ACTIVE: return await interaction.response.send_message("打烊了", ephemeral=True)
    if is_blacklisted(interaction.user.id): return await interaction.response.send_message("🚫 你已被列入黑名單，無法參與遊戲！", ephemeral=True)
    if bet < 100: return await interaction.response.send_message("低消 100", ephemeral=True)
    if get_user_stats(interaction.user.id) is None: return await interaction.response.send_message("請先使用 /register 獲取啟動資金", ephemeral=True)
    sv = SetupView(interaction.user, bet)
    await interaction.response.send_message(embed=sv.build_embed(), view=sv)

@bot.tree.command(name="balance", description="查詢你或別人的餘額")
@app_commands.describe(member="你想查詢的對象 (選填)")
async def balance(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    stats = get_user_stats(target.id)
    if not stats: return await interaction.response.send_message(f"{target.mention} 尚未註冊！", ephemeral=True)
    embed = discord.Embed(title="💰 帳戶餘額", description=f"{target.mention} 目前擁有 **{stats[0]}** 幣", color=0x2b2d31)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="查看全伺服器最富有的前 10 名玩家")
async def leaderboard(interaction: discord.Interaction):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 10")
    data = c.fetchall(); conn.close()
    msg = "\n".join([f"{i+1}. <@{uid}>: {bal}" for i, (uid, bal) in enumerate(data)])
    await interaction.response.send_message(embed=discord.Embed(title="🏆 排行榜", description=msg))

# --- 管理員指令 ---
@bot.tree.command(name="give", description="[管理員] 發錢給指定玩家")
@is_app_host()
@app_commands.describe(member="要發放的對象", amount="要發放的金額")
async def give(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0: return await interaction.response.send_message("金額必須大於 0", ephemeral=True)
    conn = get_db_connection(); c = conn.cursor()
    c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, str(member.id)))
    if c.rowcount == 0: c.execute("INSERT IGNORE INTO users (user_id, balance) VALUES (%s, %s)", (str(member.id), amount))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"💸 已成功發放 **{amount}** 幣給 {member.mention}！")

@bot.tree.command(name="ban", description="[管理員] 將玩家加入黑名單")
@is_app_host()
async def ban(interaction: discord.Interaction, member: discord.Member):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("INSERT IGNORE INTO blacklist (user_id) VALUES (%s)", (str(member.id),))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"⛔ {member.mention} 已被列入黑名單，無法再參與遊戲！")

@bot.tree.command(name="unban", description="[管理員] 將玩家從黑名單移除")
@is_app_host()
async def unban(interaction: discord.Interaction, member: discord.Member):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("DELETE FROM blacklist WHERE user_id=%s", (str(member.id),))
    conn.commit(); conn.close()
    await interaction.response.send_message(f"✅ {member.mention} 已從黑名單移除！")

@bot.tree.command(name="lock", description="[管理員] 開關賭場營業狀態")
@is_app_host()
async def lock(interaction: discord.Interaction):
    global IS_EVENT_ACTIVE
    IS_EVENT_ACTIVE = not IS_EVENT_ACTIVE
    await interaction.response.send_message(f"賭場狀態：{'營業中' if IS_EVENT_ACTIVE else '已打烊'}")

@bot.tree.command(name="resetall_zero", description="[管理員] 將所有人的餘額歸零")
@is_app_host()
async def resetall_zero(interaction: discord.Interaction):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("UPDATE users SET balance=0")
    conn.commit(); conn.close()
    await interaction.response.send_message("💥 老闆發威：所有人的餘額已經被**全部歸零**！")

@bot.tree.command(name="resetall_default", description="[管理員] 重置所有人戰績並發放 50000 幣")
@is_app_host()
async def resetall_default(interaction: discord.Interaction):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("UPDATE users SET balance=50000, rescue_count=0, total_games=0, wins=0, total_profit=0")
    conn.commit(); conn.close()
    await interaction.response.send_message("🔄 已經為所有人重新發放 50,000 啟動資金，且重置所有戰績。")

bot.run(os.getenv('DISCORD_TOKEN'))