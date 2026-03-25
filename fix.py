with open("c:\\Users\\USER\\.gemini\\antigravity\\scratch\\discord-bot\\bot.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

new_content = """    @discord.ui.button(label="再一局", style=discord.ButtonStyle.success)
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
        await inter.response.send_message("⚠️ 警告：你確定要把所有的財產全部押在賭博上嗎？輸了你這個雜魚就什麼都沒了喔～", view=cv, ephemeral=True)

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
    
    import datetime
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

@bot.tree.command(name="balance", description="查詢個人的戰績與餘額")
@app_commands.describe(member="你想查詢的對象 (選填)")
async def balance(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    stats = get_user_stats(target.id)
    if not stats: return await interaction.response.send_message(f"{target.mention} 尚未註冊！", ephemeral=True)
    
    bal, total_games, wins, total_profit = stats
    win_rate = (wins / total_games * 100) if total_games > 0 else 0
    
    embed = discord.Embed(title="📊 玩家戰績與帳戶餘額", color=0x2b2d31)
    embed.description = f"**{target.mention}** 的統計資料\\n"
    embed.add_field(name="💰 目前餘額", value=f"`{bal}` 東雲幣", inline=False)
    embed.add_field(name="📈 歷史總獲利", value=f"`{total_profit}` 東雲幣", inline=False)
    embed.add_field(name="🎲 總遊玩局數", value=f"`{total_games}` 局", inline=True)
    embed.add_field(name="🏆 勝率", value=f"`{win_rate:.1f}%` ({wins}勝)", inline=True)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="record", description="查詢近期所有的收入紀錄")
@app_commands.describe(member="你想查詢的對象 (選填)")
async def record_cmd(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT amount, reason, created_at FROM logs WHERE user_id=%s ORDER BY created_at DESC LIMIT 10", (str(target.id),))
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return await interaction.response.send_message(f"{target.mention} 目前尚無任何收入紀錄！", ephemeral=True)
    
    embed = discord.Embed(title="📜 近期收入紀錄", color=0x2b2d31)
    embed.description = f"**{target.mention}** 的最近 10 筆紀錄\\n\\n"
    
    for r in rows:
        amt, reason, dt = r[0], r[1], r[2]
        sign = "+" if amt > 0 else ""
        embed.description += f"[{dt.strftime('%m/%d %H:%M')}] {reason}: `{sign}{amt}`\\n"
        
    await interaction.response.send_message(embed=embed)"""

lines = lines[:437] + [new_content + "\n"] + lines[565:]

with open("c:\\Users\\USER\\.gemini\\antigravity\\scratch\\discord-bot\\bot.py", "w", encoding="utf-8") as f:
    f.writelines(lines)
