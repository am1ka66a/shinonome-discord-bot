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
                  total_games INT DEFAULT 0, wins INT DEFAULT 0, total_profit BIGINT DEFAULT 0)''')
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
        # 平手只更新餘額與盈虧，不計入總局數 (避免平手被視為敗場)
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

# Unicode 撲克牌字符對映表（iOS/macOS 渲染為實體卡牌圖示）
# ==========================================
# 🖼️ 系統 Discord 表符 (Emoji - 原生模式)
# ==========================================
_EMOJI_CACHE = {}  # {guild_id: {card_code: emoji_string}}

def get_card_code(card) -> str:
    """將卡牌轉換為 emoji_cards 資料夾對應的代碼 (例如 sp_A, pb_10)"""
    # ♠️=sp, ♥️=cu (Cœur), ♦️=lo (Losange), ♣️=pb
    suit_map = {'♠️': 'sp', '♥️': 'cu', '♦️': 'lo', '♣️': 'pb'}
    # 有時 suit 會夾雜 \ufe0f (變體選擇符)
    s = card['suit'].replace('\ufe0f', '')
    if s == '♠': ps = 'sp'
    elif s == '♥': ps = 'cu'
    elif s == '♦': ps = 'lo'
    elif s == '♣': ps = 'pb'
    else: ps = suit_map.get(card['suit'], 'sp')
    return f"{ps}_{card['rank']}"

def card_to_emoji(card, guild_id=None) -> str:
    """取得伺服器自訂 Emoji 表符，若無則回退文字"""
    code = get_card_code(card)
    if guild_id and guild_id in _EMOJI_CACHE:
        emoji = _EMOJI_CACHE[guild_id].get(code)
        if emoji: return emoji
    return f"**{card['rank']}**{card['suit']} "

async def sync_guild_emojis(guild: discord.Guild):
    """自動同步卡牌圖片至伺服器作為自訂表符"""
    if guild.id not in _EMOJI_CACHE:
        _EMOJI_CACHE[guild.id] = {e.name: str(e) for e in guild.emojis}
    if len([k for k in _EMOJI_CACHE[guild.id] if '_' in k]) >= 52: return
    if len(guild.emojis) >= guild.emoji_limit: return
    folder = "emoji_cards"
    if not os.path.exists(folder): return
    print(f"🔄 正在為 {guild.name} 同步卡牌表符...")
    files = [f for f in os.listdir(folder) if f.endswith('.png')]
    for filename in files:
        name = filename.replace('.png', '')
        if name not in _EMOJI_CACHE[guild.id]:
            if len(guild.emojis) >= guild.emoji_limit: break
            try:
                with open(os.path.join(folder, filename), "rb") as f:
                    new_emoji = await guild.create_custom_emoji(name=name, image=f.read())
                    _EMOJI_CACHE[guild.id][name] = str(new_emoji)
            except: break

def card_back_emoji(guild_id=None) -> str:
    if guild_id and guild_id in _EMOJI_CACHE:
        emoji = _EMOJI_CACHE[guild_id].get('card_back')
        if emoji: return emoji
    return "🎴"

async def _send_game(channel, gv: 'BlackjackGame', interaction: discord.Interaction = None, message_obj: discord.Message = None, view=None) -> discord.Message:
    """使用 Discord 原生表符渲染遊戲畫面"""
    if channel.guild:
        await sync_guild_emojis(channel.guild)
    
    embed = gv.build_embed(guild_id=channel.guild.id if channel.guild else None)
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
        if not stats:
            return await interaction.response.send_message("找不到你的帳號，請先 /register 註冊！", ephemeral=True)
        if stats[0] < (b + p + s):
            return await interaction.response.send_message(f"餘額不足！你目前有 {stats[0]} 東雲幣", ephemeral=True)

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
        # 立即延遲響應，避免上傳 Emoji 時超時導致按鈕「消失」
        await inter.response.defer()
        
        stats = get_user_stats(self.user.id)
        if not stats: return await inter.followup.send("請先使用 /register 註冊！", ephemeral=True)
        if stats[0] < (self.base_bet + self.p_bet + self.s_bet):
            return await inter.followup.send("餘額不足", ephemeral=True)
            
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

    async def _edit(self, message=None, extra_msg="", interaction: discord.Interaction = None):
        """核心渲染工具：統一經由 _send_game 更新看板圖"""
        try:
            # 優先使用交互進行響應
            if interaction:
                await _send_game(interaction.channel, self, interaction=interaction)
            elif message:
                await _send_game(message.channel, self, message_obj=message)
        except Exception as e:
            print(f"❌ 渲染錯誤: {e}")

    @property
    def p_hand(self):
        return self.hands[self.current_hand]

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
        for c in to_remove:
            self.remove_item(c)

    def build_embed(self, done=False, res="", profit=0, animating=False, extra_msg="", guild_id=None):
        stats = get_user_stats(self.user.id)
        bal, total, wins, t_prof = stats
        wr = (wins/total*100) if total>0 else 0
        embed = discord.Embed(title="🃏 21點大賽", color=0x2b2d31)
        embed.description = f"目前餘額：{bal} | 勝率：{wr:.1f}% | 總場次：{total} | 總盈虧：{t_prof}"
        if extra_msg:
            embed.description += f"\n\n**{extra_msg}**"
        for i, hand in enumerate(self.hands):
            indicator = "👉 " if i == self.current_hand and not done else ""
            title_text = f"{indicator}👤 {self.user.display_name} 的手牌"
            if len(self.hands) > 1: title_text += f" (第 {i+1} 手)"
            
            p_cards = ' '.join([card_to_emoji(c, guild_id) for c in hand])
            embed.add_field(name=title_text, value=f"{p_cards}\n點數：**{calculate_score(hand)}**", inline=False)
        if done or animating:
            d_cards = ' '.join([card_to_emoji(c, guild_id) for c in self.d_hand])
            embed.add_field(name="🤖 莊家手牌", value=f"{d_cards}\n點數：**{calculate_score(self.d_hand)}**", inline=False)
            if done:
                total_profit = profit + self.side_p
                res_text = f"**{res}**\n{self.side_m}\n"
                if total_profit > 0: res_text += f"\n📈 本局總計：`+{total_profit}` 東雲幣"
                elif total_profit < 0: res_text += f"\n📉 本局總計：`{total_profit}` 東雲幣"
                else: res_text += f"\n➖ 本局無輸贏"
                res_text += f"\n💰 最新餘額：`{bal}` 東雲幣"
                embed.add_field(name="🏆 結果", value=res_text, inline=False)
        else:
            embed.add_field(name="🤖 莊家手牌", value=f"{card_to_emoji(self.d_hand[0], guild_id)} {card_back_emoji(guild_id)}\n點數：**❓**", inline=False)
        return embed



    async def check_auto_bj(self, message):
        if calculate_score(self.p_hand) == 21:
            await asyncio.sleep(1.5)
            try:
                await self.advance_hand(message_obj=message)
            except discord.NotFound:
                pass  # 消息可能已因超時被刪除
            except Exception:
                pass

    async def end(self, res, prof, win=False, is_push=False, message_obj=None, interaction=None):
        if getattr(self, '_game_over', False): return
        self._game_over = True
        for c in self.children: c.disabled = True
        update_game_result(self.user.id, prof + getattr(self, 'side_p', 0), win, is_push)
        nv  = NewGameView(self.user, self.bet, self.p_bet, self.s_bet, get_user_stats(self.user.id)[0])
        self.hand_results[self.current_hand] = (res, prof, win) # 確保結束時有結果，用於渲染
        # 結束時切換到「新遊戲」的 View (nv)
        await _send_game(message_obj.channel if message_obj else interaction.channel, self, interaction=interaction, message_obj=message_obj, view=nv)

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
        
        # 使用傳入的交互或訊息進行更新
        await self._edit(message=message_obj, interaction=interaction)

        if need_dealer:
            await asyncio.sleep(1.2)
            while calculate_score(self.d_hand) < 17 and len(self.d_hand) < 5:
                self.d_hand.append(self.deck.pop())
                await self._edit(message=message_obj, interaction=None) # 後續動畫不需要 interaction
                await asyncio.sleep(1.2)

        total_prof = 0
        final_res_texts = []
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
            player_bj = len(hand) == 2 and ps == 21
            player_5_card = len(hand) == 5 and ps <= 21

            if player_5_card and dealer_5_card:
                final_res_texts.append(f"第 {i+1} 手: 🤝 雙方皆過五關！平手" if len(self.hands)>1 else "🤝 雙方皆過五關！平手")
            elif player_5_card:
                final_res_texts.append(f"第 {i+1} 手: 🐉 你過五關啦！爽贏 2.5 倍！" if len(self.hands)>1 else "🐉 你過五關啦！爽贏 2.5 倍！")
                total_prof += int(self.hand_bets[i] * 2.5)
            elif dealer_5_card:
                final_res_texts.append(f"第 {i+1} 手: 🐉 老子過五關啦！你這低能兒～" if len(self.hands)>1 else "🐉 老子過五關啦！你這低能兒～")
                total_prof -= self.hand_bets[i]
            elif player_bj and dealer_bj:
                final_res_texts.append(f"第 {i+1} 手: 🤝 雙方皆為 BlackJack！平手" if len(self.hands)>1 else "🤝 雙方皆為 BlackJack！平手")
            elif player_bj:
                final_res_texts.append(f"第 {i+1} 手: 🌟 BlackJack！1.5倍賠率！" if len(self.hands)>1 else "🌟 BlackJack！1.5倍賠率！")
                total_prof += int(self.hand_bets[i] * 1.5)
            elif dealer_bj:
                final_res_texts.append(f"第 {i+1} 手: 💀 莊家 BlackJack！你輸啦～雜魚～" if len(self.hands)>1 else "💀 莊家 BlackJack！你輸啦～雜魚～")
                total_prof -= self.hand_bets[i]
            elif ds > 21 or ps > ds:
                final_res_texts.append(f"第 {i+1} 手: 🎉 這次算你贏啦，腦殘！" if len(self.hands)>1 else "🎉 這次算你贏啦，腦殘！！")
                total_prof += self.hand_bets[i]
            elif ps < ds:
                final_res_texts.append(f"第 {i+1} 手: 💀 你輸啦～雜魚～" if len(self.hands)>1 else "💀 你輸啦～雜魚～")
                total_prof -= self.hand_bets[i]
            else:
                final_res_texts.append(f"第 {i+1} 手: 🤝 就這點技術阿腦殘？" if len(self.hands)>1 else "🤝 就這點技術阿腦殘？")

        final_msg = "\n".join(final_res_texts)
        # 計算含旁注的總盈虧以判定勝負與是否平手
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
            if ps > 21:
                self.hand_results[self.current_hand] = ("爆牌輸了", -self.hand_bets[self.current_hand], False)
            await self.advance_hand(interaction=inter, message_obj=inter.message)
        else:
            await self._edit(interaction=inter)

    @discord.ui.button(label="停牌", style=discord.ButtonStyle.danger)
    async def stand(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        await self.advance_hand(interaction=inter, message_obj=inter.message)

    @discord.ui.button(label="投降", style=discord.ButtonStyle.secondary)
    async def surrender(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        self.hand_results[self.current_hand] = ("這樣就投降了嗎，雜魚～", -(self.hand_bets[self.current_hand]//2), False)
        await self.advance_hand(interaction=inter, message_obj=inter.message)

    @discord.ui.button(label="雙倍", style=discord.ButtonStyle.primary)
    async def double_down(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        stats = get_user_stats(self.user.id)
        available_balance = stats[0] + min(0, getattr(self, 'side_p', 0))
        extra_needed = self.hand_bets[self.current_hand]
        if available_balance < sum(self.hand_bets) + extra_needed:
            return await inter.followup.send("餘額不足，無法雙倍下注", ephemeral=True)
            
        self.hand_bets[self.current_hand] *= 2
        self.p_hand.append(self.deck.pop())
        self.update_buttons()
        
        ps = calculate_score(self.p_hand)
        if ps > 21:
            self.hand_results[self.current_hand] = ("你爆牌囉～小丑～", -self.hand_bets[self.current_hand], False)
        await self.advance_hand(interaction=inter, message_obj=inter.message)

    @discord.ui.button(label="分牌", style=discord.ButtonStyle.primary)
    async def split(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        stats = get_user_stats(self.user.id)
        available_balance = stats[0] + min(0, getattr(self, 'side_p', 0))
        current_max_loss = sum(self.hand_bets) + self.bet
        if available_balance < current_max_loss:
            return await inter.followup.send("餘額不足，無法分牌", ephemeral=True)
            
        self.is_split = True
        c1, c2 = self.hands[0][0], self.hands[0][1]
        self.hands = [[c1, self.deck.pop()], [c2, self.deck.pop()]]
        self.hand_results = [None, None]
        self.hand_bets = [self.bet, self.bet]
        self.update_buttons()
        await self._edit(interaction=inter, extra_msg="✌️ 你選擇了分牌！")
        if calculate_score(self.p_hand) == 21:
            await asyncio.sleep(1.5)
            await self.advance_hand(interaction=None, message_obj=inter.message)

class ConfirmAllInView(discord.ui.View):
    def __init__(self, user, parent_msg):
        super().__init__(timeout=30)
        self.user = user
        self.parent_msg = parent_msg

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("這不是你的按鈕！", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="確定 All In！", style=discord.ButtonStyle.danger)
    async def confirm(self, inter, btn):
        stats = get_user_stats(self.user.id)
        if not stats or stats[0] < 100:
            return await inter.response.send_message("破產仔沒資格 All In！", ephemeral=True)
        if is_blacklisted(self.user.id):
            return await inter.response.send_message("🚫 你已被列入黑名單，耍賴也沒用！", ephemeral=True)
        self.stop()
        await inter.response.edit_message(content="🔥 All In 已確認！正在為你開牌...", view=None)
        try:
            await self.parent_msg.delete()
        except:
            pass
        gv = BlackjackGame(self.user, stats[0], 0, 0)
        msg = await _send_game(inter.channel, gv)
        asyncio.create_task(gv.check_auto_bj(msg))

class NewGameView(discord.ui.View):
    def __init__(self, user, last_bet, last_p_bet, last_s_bet, current_bal):
        super().__init__(timeout=90)
        self.user = user
        self.last_bet = last_bet
        self.last_p_bet = last_p_bet
        self.last_s_bet = last_s_bet
        self.current_bal = current_bal

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("這不是你的牌局！", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="再來一局", style=discord.ButtonStyle.success)
    async def again(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        
        stats = get_user_stats(self.user.id)
        if not stats: return await inter.followup.send("請先使用 /register 註冊！", ephemeral=True)
        if stats[0] < (self.last_bet + self.last_p_bet + self.last_s_bet):
            return await inter.followup.send("餘額不足", ephemeral=True)
            
        self.stop()
        gv = BlackjackGame(self.user, self.last_bet, self.last_p_bet, self.last_s_bet)
        await _send_game(inter.channel, gv, interaction=inter)
        msg = await inter.original_response()
        asyncio.create_task(gv.check_auto_bj(msg))

    @discord.ui.button(label="雙倍再局 (Double)", style=discord.ButtonStyle.primary)
    async def double_again(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        
        stats = get_user_stats(self.user.id)
        new_bet = self.last_bet * 2
        if stats[0] < (new_bet + self.last_p_bet + self.last_s_bet):
            return await inter.followup.send("餘額不足以雙倍下注", ephemeral=True)
            
        self.stop()
        gv = BlackjackGame(self.user, new_bet, self.last_p_bet, self.last_s_bet)
        await _send_game(inter.channel, gv, interaction=inter)
        msg = await inter.original_response()
        asyncio.create_task(gv.check_auto_bj(msg))

    @discord.ui.button(label="修改下注", style=discord.ButtonStyle.secondary)
    async def modify_bet(self, inter, btn):
        self.stop()
        await inter.response.defer()
        try:
            await inter.message.delete()
        except:
            pass
        setup = SetupView(self.user, self.last_bet, self.last_p_bet, self.last_s_bet)
        await inter.channel.send(embed=setup.build_embed(), view=setup)

    @discord.ui.button(label="All In (全押)", style=discord.ButtonStyle.danger)
    async def all_in(self, inter, btn):
        cv = ConfirmAllInView(self.user, inter.message)
        await inter.response.send_message("⚠️ 警告：你確定要把所有的財產全部押在主注上嗎？輸了你這個雜魚就什麼都沒了喔～", view=cv, ephemeral=True)


# ==========================================
# 🤖 4. 指令系統
# ==========================================
intents = discord.Intents.default(); intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()
    print(f"{bot.user} 啟動並已同步斜線指令")


@bot.tree.command(name="register", description="註冊你的帳號並獲得 50,000 啟動資金")
async def register(interaction: discord.Interaction):
    if is_blacklisted(interaction.user.id): return await interaction.response.send_message("🚫 被ban的傻屌無法註冊！", ephemeral=True)
    conn = get_db_connection(); c = conn.cursor()
    c.execute("INSERT IGNORE INTO users (user_id, balance) VALUES (%s, 50000)", (str(interaction.user.id),))
    if c.rowcount == 0:
        await interaction.response.send_message(f"⚠️ {interaction.user.mention} 你已經註冊過了！", ephemeral=True)
    else:
        log_transaction(interaction.user.id, 50000, "註冊獎勵")
        await interaction.response.send_message(f"🎉 {interaction.user.mention} 註冊成功，獲得 50,000 東雲幣！")
    conn.commit(); conn.close()

@bot.tree.command(name="daily", description="每日簽到領取 10,000 東雲幣")
async def daily(interaction: discord.Interaction):
    if is_blacklisted(interaction.user.id): return await interaction.response.send_message("🚫 被ban的傻屌無法簽到！", ephemeral=True)
    stats = get_user_stats(interaction.user.id)
    if not stats: return await interaction.response.send_message("請先使用 /register 註冊！", ephemeral=True)
    
    conn = get_db_connection(); c = conn.cursor()
    today = datetime.date.today()
    c.execute("SELECT last_claim FROM daily_claims WHERE user_id=%s", (str(interaction.user.id),))
    row = c.fetchone()
    
    if row and row[0] == today:
        conn.close()
        return await interaction.response.send_message("⚠️ 你今天已經簽到過了！明天再來吧。", ephemeral=True)
        
    c.execute("INSERT INTO daily_claims (user_id, last_claim) VALUES (%s, %s) ON DUPLICATE KEY UPDATE last_claim=%s", 
              (str(interaction.user.id), today, today))
    c.execute("UPDATE users SET balance=balance+10000 WHERE user_id=%s", (str(interaction.user.id),))
    conn.commit(); conn.close()
    
    log_transaction(interaction.user.id, 10000, "每日簽到")
    await interaction.response.send_message(f"🎉 簽到成功！獲得 10,000 東雲幣。目前餘額：{stats[0]+10000} 東雲幣")

@bot.tree.command(name="bj", description="開始一場 21點對決")
@app_commands.describe(bet="你想要下注的金額 (預設 1000)")
async def bj(interaction: discord.Interaction, bet: int = 1000):
    if not IS_EVENT_ACTIVE: return await interaction.response.send_message("打烊了", ephemeral=True)
    if is_blacklisted(interaction.user.id): return await interaction.response.send_message("🚫 你已被列入黑名單，無法參與遊戲！", ephemeral=True)
    if bet < 100: return await interaction.response.send_message("低消 100", ephemeral=True)
    if get_user_stats(interaction.user.id) is None: return await interaction.response.send_message("請先使用 /register 獲取啟動資金", ephemeral=True)
    sv = SetupView(interaction.user, bet)
    await interaction.response.send_message(embed=sv.build_embed(), view=sv)

@bot.tree.command(name="test_emojis", description="[測試] 顯示目前讀取到的全部撲克牌 Emoji 功能是否正常")
async def test_emojis(interaction: discord.Interaction):
    deck = get_deck(1)
    spades = [c for c in deck if c['suit'] == '♠️']
    hearts = [c for c in deck if c['suit'] == '♥️']
    diamonds = [c for c in deck if c['suit'] == '♦️']
    clubs = [c for c in deck if c['suit'] == '♣️']
    msg = "**♠️ 黑桃:** " + " ".join([card_to_emoji(c) for c in spades]) + "\n\n"
    msg += "**♥️ 紅心:** " + " ".join([card_to_emoji(c) for c in hearts]) + "\n\n"
    msg += "**♦️ 方塊:** " + " ".join([card_to_emoji(c) for c in diamonds]) + "\n\n"
    msg += "**♣️ 梅花:** " + " ".join([card_to_emoji(c) for c in clubs]) + "\n\n"
    msg += "**🃏 牌背:** " + card_back_emoji()
    embed = discord.Embed(title="🃏 撲克牌 Emoji 測試清單", description=msg, color=0x2b2d31)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="balance", description="查詢個人的戰績與餘額")
@app_commands.describe(member="你想查詢的對象 (選填)")
async def balance(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    stats = get_user_stats(target.id)
    if not stats: return await interaction.response.send_message(f"{target.mention} 尚未註冊！", ephemeral=True)
    bal, total_games, wins, total_profit = stats
    win_rate = (wins / total_games * 100) if total_games > 0 else 0
    embed = discord.Embed(title="📊 玩家戰績與帳戶餘額", color=0x2b2d31)
    embed.description = f"**{target.mention}** 的統計資料\n"
    embed.add_field(name="💰 目前餘額", value=f"`{bal}` 東雲幣", inline=False)
    embed.add_field(name="📈 歷史總獲利", value=f"`{total_profit}` 東雲幣", inline=False)
    embed.add_field(name="🎲 總遊玩局數", value=f"`{total_games}` 局", inline=True)
    embed.add_field(name="🏆 勝率", value=f"`{win_rate:.1f}%` ({wins}勝)", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="record", description="查詢近期所有的收入紀錄")
@app_commands.describe(member="你想查詢的對象 (選填)")
async def record_cmd(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT amount, reason, created_at FROM logs WHERE user_id=%s ORDER BY created_at DESC LIMIT 10", (str(target.id),))
    rows = c.fetchall(); conn.close()
    if not rows: return await interaction.response.send_message(f"{target.mention} 目前尚無任何收入紀錄！", ephemeral=True)
    embed = discord.Embed(title="📜 近期收入紀錄", color=0x2b2d31)
    embed.description = f"**{target.mention}** 的最近 10 筆紀錄\n\n"
    for r in rows:
        amt, reason, dt = r[0], r[1], r[2]
        sign = "+" if amt > 0 else ""
        embed.description += f"[{dt.strftime('%m/%d %H:%M')}] {reason}: `{sign}{amt}`\n"
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="查看全伺服器最富有的前 10 名玩家")
async def leaderboard(interaction: discord.Interaction):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 10")
    data = c.fetchall(); conn.close()
    msg = "\n".join([f"{i+1}. <@{uid}>: {bal}" for i, (uid, bal) in enumerate(data)])
    await interaction.response.send_message(embed=discord.Embed(title="🏆 排行榜", description=msg))

# --- 管理員指令 ---
@bot.command()
@is_host()
async def give(ctx, member: discord.Member, amount: int):
    if amount <= 0: return await ctx.send("金額必須大於 0")
    conn = get_db_connection(); c = conn.cursor()
    c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, str(member.id)))
    if c.rowcount == 0: c.execute("INSERT IGNORE INTO users (user_id, balance) VALUES (%s, %s)", (str(member.id), amount))
    conn.commit(); conn.close()
    log_transaction(member.id, amount, "管理員發放")
    await ctx.send(f"💸 已成功發放 **{amount}** 東雲幣給 {member.mention}！")

@bot.command()
@is_host()
async def take(ctx, member: discord.Member, amount: int):
    if amount <= 0: return await ctx.send("金額必須大於 0")
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id=%s", (str(member.id),))
    row = c.fetchone()
    if not row:
        conn.close()
        return await ctx.send(f"⚠️ {member.mention} 尚未註冊！")
    new_bal = max(0, row[0] - amount)
    c.execute("UPDATE users SET balance=%s WHERE user_id=%s", (new_bal, str(member.id)))
    conn.commit(); conn.close()
    log_transaction(member.id, -(row[0] - new_bal), "管理員扣除")
    await ctx.send(f"📉 已成功從 {member.mention} 帳戶中扣除 **{amount}** 東雲幣！現在餘額：{new_bal}")

@bot.command()
@is_host()
async def ban(ctx, member: discord.Member):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("INSERT IGNORE INTO blacklist (user_id) VALUES (%s)", (str(member.id),))
    conn.commit(); conn.close()
    await ctx.send(f"⛔ {member.mention} 已被列入黑名單，無法再參與遊戲！")

@bot.command()
@is_host()
async def unban(ctx, member: discord.Member):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("DELETE FROM blacklist WHERE user_id=%s", (str(member.id),))
    conn.commit(); conn.close()
    await ctx.send(f"✅ {member.mention} 已從黑名單移除！")

@bot.command()
@is_host()
async def lock(ctx):
    global IS_EVENT_ACTIVE
    IS_EVENT_ACTIVE = not IS_EVENT_ACTIVE
    await ctx.send(f"賭場狀態：{'營業中' if IS_EVENT_ACTIVE else '已打烊'}")

@bot.command()
@is_host()
async def resetall_zero(ctx):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("UPDATE users SET balance=0")
    conn.commit(); conn.close()
    await ctx.send("💥 老子發威：所有人的餘額已經被**全部歸零**！")

@bot.command()
@is_host()
async def resetall_default(ctx):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("UPDATE users SET balance=50000, rescue_count=0, total_games=0, wins=0, total_profit=0")
    conn.commit(); conn.close()
    await ctx.send("🔄 已經為所有人重新發放 50,000 啟動資金，且重置所有戰績。")

@bot.command()
@is_host()
async def adminhelp(ctx):
    help_text = """**👑 管理員專屬指令清單 (Prefix 限制)**
`!give @玩家 <數量>` - 發放東雲幣
`!take @玩家 <數量>` - 扣除東雲幣
`!ban @玩家` - 設為黑名單
`!unban @玩家` - 解除黑名單
`!lock` - 開關賭場 (停止新的/bj)
`!resetall_zero` - 全服餘額歸零
`!resetall_default` - 全服重置為 50,000"""
    await ctx.send(help_text)

bot.run(os.getenv('DISCORD_TOKEN'))