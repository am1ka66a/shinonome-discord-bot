import datetime
import random
import typing

import discord
from discord import app_commands


def register_fun_commands(bot, ctx: typing.Dict[str, typing.Any]) -> None:
    ALLOWED_HOST_IDS = ctx["ALLOWED_HOST_IDS"]
    ROB_BASE_SUCCESS_RATE = ctx["ROB_BASE_SUCCESS_RATE"]
    COP_HUNT_CAPTURE_BASE_PCT = ctx["COP_HUNT_CAPTURE_BASE_PCT"]
    COP_HUNT_CAPTURE_PER_STAR_PCT = ctx["COP_HUNT_CAPTURE_PER_STAR_PCT"]
    COP_HUNT_FEE = ctx["COP_HUNT_FEE"]
    WANTED_BUYOUT_COST = ctx["WANTED_BUYOUT_COST"]
    GOOD_CITIZEN_CERT_COST = ctx["GOOD_CITIZEN_CERT_COST"]
    GOOD_CITIZEN_DESTROY_COST = ctx["GOOD_CITIZEN_DESTROY_COST"]
    BAIL_COST = ctx["BAIL_COST"]
    COUNTER_ROB_BASE_SUCCESS_RATE = ctx["COUNTER_ROB_BASE_SUCCESS_RATE"]
    MINECRAFT_DEATH_MESSAGES = ctx["MINECRAFT_DEATH_MESSAGES"]
    MINECRAFT_ITEMS = ctx["MINECRAFT_ITEMS"]
    BICYCLE_COOLDOWN_SECONDS = ctx["BICYCLE_COOLDOWN_SECONDS"]
    TW_TZ = ctx["TW_TZ"]
    resolve_slash_target = ctx["resolve_slash_target"]
    ensure_user_exists_async = ctx["ensure_user_exists_async"]
    now_tw_naive = ctx["now_tw_naive"]
    get_db_connection = ctx["get_db_connection"]
    lock_user_rows = ctx["lock_user_rows"]
    log_transaction_in_tx = ctx["log_transaction_in_tx"]

    @bot.tree.command(name="help", description="機器人指令總覽（一般玩家）")
    async def help_slash(interaction: discord.Interaction):
        """東雲幣、賭場、通緝／警察、等級等 Slash 說明（不含主機／管理員專用指令）。"""
        emb = discord.Embed(
            title="📖 東雲機器人指令說明",
            description="以下為**一般玩家**常用指令；管理／主機專用請見伺服公告或管理員。",
            color=0x5865F2,
        )
        _cop_hunt_pct_1star = min(
            95,
            COP_HUNT_CAPTURE_BASE_PCT + COP_HUNT_CAPTURE_PER_STAR_PCT,
        )
        emb.add_field(
            name="💰 日常與經濟",
            value=(
                "`/daily` — 每日簽到領幣\n"
                "`/hourly` — 每小時簽到（依等級累積）\n"
                "`/beg` — 乞討\n"
                f"`/rob` — 搶劫（**僅搶匪**；約 **{int(round(ROB_BASE_SUCCESS_RATE * 100))}%** 基礎成功率、每級差 ±1%；**30 分鐘**冷卻；成功累積通緝）\n"
                "`/rescue` — 破產救濟（餘額 0 時）\n"
                "`/transfer` — 轉帳給其他玩家\n"
                "`/redpacket` — 發紅包\n"
                "`/record` — 最近帳務紀錄（翻頁）\n"
                "`/balance` — 餘額與戰績"
            ),
            inline=False,
        )
        emb.add_field(
            name="🃏 賭場與等級",
            value=(
                "`/bj` — 二十一點\n"
                "`/duel` — E 卡決鬥（兩大局；第二大局交換陣營；奴贏王 +3、其餘決勝 +1；依積分分配彩池）\n"
                "`/level` — 等級與 EXP\n"
                "`/leaderboard` — 餘額榜前 10\n"
                "`/lvleaderboard` — 等級榜前 10\n"
                "`/casino_stats` — 經濟總金流統計"
            ),
            inline=False,
        )
        emb.add_field(
            name="🚔 通緝與警察",
            value=(
                "`/role_choose` — 選警察／搶匪／平民\n"
                "`/wanted_status` — 自己的通緝、監獄、搶劫紀錄\n"
                "`/wanted_list` — 目前通緝名單與可否追捕\n"
                f"`/good_citizen` — [平民] 付 `{GOOD_CITIZEN_CERT_COST:,}` 啟用防搶；再付同額解除（啟用/解除皆 24h 冷卻）\n"
                "`/good_citizen_list` — 查看目前良民證持有者\n"
                f"`/break_citizen` — 摧毀目標良民證（花費 `{GOOD_CITIZEN_DESTROY_COST:,}`，目標 10 天禁用）\n"
                f"`/cop_hunt` — 警察追捕（僅警察；每次 **`{COP_HUNT_FEE:,}`** 幣、成敗皆扣）。"
                f"成功率 **1★ 約 {_cop_hunt_pct_1star}%** 起，通緝每多 **1** 星 **+{COP_HUNT_CAPTURE_PER_STAR_PCT}%**，並受等級差影響（每級 ±1%，保底 **5%**、上限 **95%**）\n"
                f"`/wanted_buyout` — [搶匪] 付 `{WANTED_BUYOUT_COST:,}` 消除全部通緝星並**清空最近搶劫紀錄**（**24 小時**冷卻）\n"
                f"`/counter_rob` — 已改為平民被搶後**自動反制結算**（約 **{int(round(COUNTER_ROB_BASE_SUCCESS_RATE * 100))}%** 基礎、級差 ±1%）\n"
                f"`/bail` — 入獄繳 **基礎 `{BAIL_COST:,}` + 累計假釋欠款** 出獄"
            ),
            inline=False,
        )
        emb.add_field(
            name="🎮 其他",
            value="`/kill` — Minecraft 風格隨機死法（需選本群成員）\n"
            "`/跳蛋` — 悼念早安同學\n"
            "`/bicycle` — 嘗試偷走奈音的腳踏車",
            inline=False,
        )
        emb.set_footer(text="私訊轉接、群組 @ 機器人可聯繫管理員｜管理員請用 /adminhelp（僅主機）")
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @bot.tree.command(name="kill", description="在目前頻道送出 Minecraft 風格隨機死法")
    @app_commands.describe(target="目標（選人）", user_id="或填使用者 ID／貼提及")
    async def kill(
        interaction: discord.Interaction,
        target: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
    ):
        m_user, err = await resolve_slash_target(
            interaction, target, user_id, required=True, in_guild_only=True
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        if not isinstance(m_user, discord.Member):
            return await interaction.response.send_message("目標必須是此伺服器成員。", ephemeral=True)
        target = m_user
        template = random.choice(MINECRAFT_DEATH_MESSAGES)
        item = random.choice(MINECRAFT_ITEMS)
        target_text = target.mention
        msg = (
            template.format(target=target_text)
            .replace("<死者>", target_text)
            .replace("死者", target_text)
            .replace("擊殺者", interaction.user.mention)
            .replace("击杀者", interaction.user.mention)
            .replace("物品", item)
        )
        await interaction.response.send_message(msg)

    @bot.tree.command(name="跳蛋", description="悼念早安同學")
    async def tiaodan_slash(interaction: discord.Interaction):
        now_text = now_tw_naive().strftime("%Y/%m/%d %H:%M:%S")
        msg = (
            f"早安同學已於{now_text}因使用了便宜的跳蛋被電而不幸逝世，"
            "感謝大家在他的生命中出現過，並給予了他光和溫暖，也謝謝大家曾給過他幫助和愛。"
            "斯人已逝，惟願他早登極樂，往生淨土。"
        )
        await interaction.response.send_message(msg)

    @bot.tree.command(name="bicycle", description="嘗試偷走奈音的腳踏車")
    async def bicycle_slash(interaction: discord.Interaction):
        thief_id = str(interaction.user.id)
        target_id = "1027248561177509919"
        steal_amount = 100
        now = now_tw_naive()
        await ensure_user_exists_async(interaction.user.id, 50000)
        await ensure_user_exists_async(int(target_id), 0)

        conn = get_db_connection()
        c = conn.cursor()
        lock_user_rows(c, [thief_id, target_id])
        c.execute("SELECT last_bicycle FROM users WHERE user_id=%s", (thief_id,))
        row = c.fetchone()
        last_bicycle = row[0] if row else None
        if last_bicycle and (now - last_bicycle).total_seconds() < BICYCLE_COOLDOWN_SECONDS:
            conn.close()
            remain = BICYCLE_COOLDOWN_SECONDS - int((now - last_bicycle).total_seconds())
            ts = int((now + datetime.timedelta(seconds=remain)).replace(tzinfo=TW_TZ).timestamp())
            return await interaction.response.send_message(
                f"⏳ /bicycle 冷卻中，請於 <t:{ts}:R> 再試。",
                ephemeral=True,
            )

        success = random.random() < 0.99
        if not success:
            c.execute("UPDATE users SET last_bicycle=%s WHERE user_id=%s", (now, thief_id))
            conn.commit()
            conn.close()
            return await interaction.response.send_message("小黑龜再練練 連個腳踏車都偷不走")

        c.execute(
            "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
            (steal_amount, target_id, steal_amount),
        )
        if c.rowcount > 0:
            c.execute(
                "UPDATE users SET balance=balance+%s WHERE user_id=%s",
                (steal_amount, thief_id),
            )
            c.execute("UPDATE users SET last_bicycle=%s WHERE user_id=%s", (now, thief_id))
            log_transaction_in_tx(c, target_id, -steal_amount, f"腳踏車被偷（偷車者:{thief_id}）")
            log_transaction_in_tx(c, thief_id, steal_amount, f"偷走奈音腳踏車（目標:{target_id}）")
            conn.commit()
            conn.close()
            return await interaction.response.send_message("你成功偷走了奈音的腳踏車!")

        c.execute("UPDATE users SET last_bicycle=%s WHERE user_id=%s", (now, thief_id))
        conn.commit()
        conn.close()
        return await interaction.response.send_message("奈音身上沒錢了! 沒有腳踏車能偷!")

    @bot.tree.command(name="say", description="[管理員] 指定機器人對特定頻道發送內容")
    @app_commands.describe(text="你要機器人說什麼？", channel="指定發送到哪個頻道？(選填)")
    @app_commands.default_permissions(manage_messages=True)
    async def say_slash(interaction: discord.Interaction, text: str, channel: discord.TextChannel = None):
        if interaction.user.id not in ALLOWED_HOST_IDS:
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        target_channel = channel or interaction.channel
        await target_channel.send(text)
        await interaction.response.send_message(f"✅ 訊息已發送到 {target_channel.mention}！", ephemeral=True)
