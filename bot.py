import discord
from discord import app_commands
from discord.ext import commands
import random
import pymysql
import os
import asyncio
import datetime
import time
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# ⚙️ 系統設定與全局變數
# ==========================================
ALLOWED_HOST_IDS = [531308526262550528, 600177596088582185]  # ⚠️ 填入你的 Discord ID
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
        port=int(os.getenv('DB_PORT', 3306)),
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
                  total_games INT DEFAULT 0, wins INT DEFAULT 0, total_profit BIGINT DEFAULT 0,
                  last_work TIMESTAMP NULL, last_beg TIMESTAMP NULL, last_rescue TIMESTAMP NULL)''')
    # 確保現有表也有新欄位 (Migration)
    try: c.execute("ALTER TABLE users ADD COLUMN last_work TIMESTAMP NULL")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN last_beg TIMESTAMP NULL")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN last_rescue TIMESTAMP NULL")
    except: pass

    c.execute('''CREATE TABLE IF NOT EXISTS activity_stats 
                 (user_id VARCHAR(255) PRIMARY KEY, msg_count INT DEFAULT 0, 
                  last_msg_reward TIMESTAMP NULL, last_vc_reward TIMESTAMP NULL)''')

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

def update_game_result(user_id, profit, is_win, is_push=False):
    conn = get_db_connection()
    c = conn.cursor()
    win_int = 1 if is_win else 0
    if is_push:
        c.execute("UPDATE users SET balance=balance+%s, total_profit=total_profit+%s WHERE user_id=%s",
                  (profit, profit, str(user_id)))
    else:
        c.execute("UPDATE users SET balance=balance+%s, total_profit=total_profit+%s, total_games=total_games+1, wins=wins+%s WHERE user_id=%s",
                  (profit, profit, win_int, str(user_id)))
    conn.commit()
    conn.close()
    if profit != 0:
        log_transaction(user_id, profit, "21點遊戲結算")

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
        if stats[0] < (self.base_bet + self.p_bet + self.s_bet): return await inter.followup.send("餘額不足", ephemeral=True)
        self.stop()
        gv = BlackjackGame(self.user, self.base_bet, self.p_bet, self.s_bet)
        await _send_game(inter.channel, gv, interaction=inter)
        msg = await inter.original_response()
        asyncio.create_task(gv.check_auto_bj(msg))

    @discord.ui.button(label="自訂下注金額", style=discord.ButtonStyle.primary)
    async def custom_bet(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.send_modal(BetModal(self))

class BlackjackGame(discord.ui.View):
    def __init__(self, user, bet, p_bet, s_bet):
        super().__init__(timeout=90)
        self.user, self.bet, self.p_bet, self.s_bet = user, bet, p_bet, s_bet
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
                c.disabled = calculate_score(self.p_hand) >= 21
        for c in to_remove: self.remove_item(c)

    def build_embed(self, done=False, res="", profit=0, animating=False, extra_msg="", guild_id=None):
        stats = get_user_stats(self.user.id)
        if stats: bal, total, wins, t_prof = stats
        else: bal, total, wins, t_prof = 0, 0, 0, 0
        wr = (wins/total*100) if total>0 else 0
        embed = discord.Embed(title="🃏 21點大賽", color=0x2b2d31)
        main_ui = f"💰 餘額：{bal} | 🏆 勝場：{wins} | 🎲 總局數：{total} | 📈 勝率：{wr:.1f}%\n"
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
        if calculate_score(self.p_hand) == 21:
            await asyncio.sleep(1.5)
            try: await self.advance_hand(message_obj=message)
            except: pass

    async def end(self, res, prof, win=False, is_push=False, message_obj=None, interaction=None):
        if getattr(self, '_game_over', False): return
        self._game_over = True
        
        # 關鍵修正：將結果更新至資料庫 (主注盈虧 + 旁注盈虧)
        total_p = prof + getattr(self, 'side_p', 0)
        update_game_result(self.user.id, total_p, win, is_push)
        
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
            if calculate_score(self.p_hand) == 21:
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
        if ps >= 21 or len(self.p_hand) == 5:
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
        stats = get_user_stats(self.user.id)
        if stats[0] < sum(self.hand_bets) + self.hand_bets[self.current_hand]: return await inter.followup.send("餘額不足", ephemeral=True)
        self.hand_bets[self.current_hand] *= 2
        self.p_hand.append(self.deck.pop())
        if calculate_score(self.p_hand) > 21: self.hand_results[self.current_hand] = ("你爆牌囉～小丑～", -self.hand_bets[self.current_hand], False)
        await self.advance_hand(interaction=inter, message_obj=inter.message)

    @discord.ui.button(label="分牌", style=discord.ButtonStyle.primary)
    async def split(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        stats = get_user_stats(self.user.id)
        if stats[0] < sum(self.hand_bets) + self.bet: return await inter.followup.send("餘額不足", ephemeral=True)
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
        stats = get_user_stats(self.user.id)
        if stats[0] < (self.last_bet + self.last_p_bet + self.last_s_bet): return await inter.followup.send("餘額不足", ephemeral=True)
        self.stop(); gv = BlackjackGame(self.user, self.last_bet, self.last_p_bet, self.last_s_bet)
        await _send_game(inter.channel, gv, interaction=inter)
        msg = await inter.original_response(); asyncio.create_task(gv.check_auto_bj(msg))
    @discord.ui.button(label="雙倍再局 (Double)", style=discord.ButtonStyle.primary)
    async def double_again(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        stats, new_bet = get_user_stats(self.user.id), self.last_bet * 2
        if stats[0] < (new_bet + self.last_p_bet + self.last_s_bet): return await inter.followup.send("餘額不足", ephemeral=True)
        self.stop(); gv = BlackjackGame(self.user, new_bet, self.last_p_bet, self.last_s_bet)
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

# --- 4. 指令系統 ---
intents = discord.Intents.default(); intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    init_db(); await bot.tree.sync(); bot.loop.create_task(vc_reward_task())
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
    if message.author.bot or not message.guild: return
    user_id, now = str(message.author.id), datetime.datetime.now()
    conn = get_db_connection(); c = conn.cursor()
    c.execute("INSERT INTO activity_stats (user_id, msg_count) VALUES (%s, 1) ON DUPLICATE KEY UPDATE msg_count=msg_count+1", (user_id,))
    c.execute("SELECT msg_count, last_msg_reward FROM activity_stats WHERE user_id=%s", (user_id,))
    row = c.fetchone()
    if row and row[0] >= 10:
        if row[1] is None or (now - row[1]).total_seconds() >= 1800:
            c.execute("INSERT INTO users (user_id, balance) VALUES (%s, 500) ON DUPLICATE KEY UPDATE balance=balance+500", (user_id,))
            c.execute("UPDATE activity_stats SET msg_count=0, last_msg_reward=%s WHERE user_id=%s", (now, user_id))
            log_transaction(user_id, 500, "聊天活躍獎勵 (10句)")
    conn.commit(); conn.close(); await bot.process_commands(message)

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
    c.execute("UPDATE users SET balance=balance+100000 WHERE user_id=%s", (str(interaction.user.id),))
    conn.commit(); conn.close(); log_transaction(interaction.user.id, 100000, "每日簽到")
    
    # 計算下一次領取時間
    tomorrow_tw = today_tw + datetime.timedelta(days=1)
    next_claim_dt = datetime.datetime.combine(tomorrow_tw, datetime.time.min, tzinfo=tz)
    ts = int(next_claim_dt.timestamp())
    await interaction.response.send_message(f"🎉 簽到成功！獲得 **100,000** 東雲幣！目前餘額：`{stats[0]+100000}`\n下次領取時間：<t:{ts}:f> (<t:{ts}:R>)")

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
    msg += f"📈 勝率：`{wr:.1f}%`"
    await interaction.response.send_message(msg)

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
async def lock(ctx):
    global IS_EVENT_ACTIVE; IS_EVENT_ACTIVE = not IS_EVENT_ACTIVE; await ctx.send(f"狀態: {IS_EVENT_ACTIVE}")

bot.run(os.getenv('DISCORD_TOKEN'))