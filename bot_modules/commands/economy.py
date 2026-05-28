import asyncio
import random
import typing

import discord
from discord import app_commands

from bot_modules.ui.views import LinePagerView


def register_economy_commands(bot, ctx: typing.Dict[str, typing.Any]) -> None:
    FEATURE_TOGGLES = ctx["FEATURE_TOGGLES"]
    MAX_LEVEL = ctx["MAX_LEVEL"]
    LEVEL_MILESTONE_COINS = ctx["LEVEL_MILESTONE_COINS"]
    RED_PACKET_MIN_SECONDS = ctx["RED_PACKET_MIN_SECONDS"]
    red_packet_seq_ref = ctx["red_packet_seq_ref"]
    resolve_slash_target = ctx["resolve_slash_target"]
    ensure_user_exists_async = ctx["ensure_user_exists_async"]
    interaction_send = ctx["interaction_send"]
    interaction_defer_if_needed = ctx["interaction_defer_if_needed"]
    claim_daily_reward_async = ctx["claim_daily_reward_async"]
    claim_hourly_reward_async = ctx["claim_hourly_reward_async"]
    claim_beg_sync_async = ctx["claim_beg_sync_async"]
    claim_rescue_sync_async = ctx["claim_rescue_sync_async"]
    get_user_stats_async = ctx["get_user_stats_async"]
    get_level_stats_async = ctx["get_level_stats_async"]
    calc_level_from_exp = ctx["calc_level_from_exp"]
    get_claimed_milestones_async = ctx["get_claimed_milestones_async"]
    build_exp_progress_bar = ctx["build_exp_progress_bar"]
    transfer_sync_async = ctx["transfer_sync_async"]
    try_deduct_balance_async = ctx["try_deduct_balance_async"]
    credit_balance_with_log_async = ctx["credit_balance_with_log_async"]
    fetch_record_rows_async = ctx["fetch_record_rows_async"]
    now_tw_naive = ctx["now_tw_naive"]
    logger = ctx["logger"]

    class RedPacketView(discord.ui.View):
        def __init__(self, creator_id, total_amount, count):
            super().__init__(timeout=120)
            red_packet_seq_ref[0] += 1
            self.packet_id = red_packet_seq_ref[0]
            self.creator_id = creator_id
            self.total_amount = total_amount
            self.count = count
            self.left_amount = total_amount
            self.left_count = count
            self.claimed_users = set()
            self.claim_results = []
            self._claim_lock = asyncio.Lock()

        def summary_text(self):
            claimed = self.count - self.left_count
            return (
                f"🧧 紅包編號 #{self.packet_id}\n"
                f"總金額：`{self.total_amount}` | 份數：`{self.count}`\n"
                f"已搶：`{claimed}` 人 | 剩餘金額：`{self.left_amount}`"
            )

        def winners_text(self):
            if not self.claim_results:
                return "尚未有人搶到紅包。"
            lines = [f"{i+1}. <@{uid}>：`{amt}`" for i, (uid, amt) in enumerate(self.claim_results)]
            return "🎉 搶到紅包名單：\n" + "\n".join(lines)

        @discord.ui.button(label="搶紅包", style=discord.ButtonStyle.success)
        async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction_defer_if_needed(interaction, ephemeral=True, thinking=False)
            async with self._claim_lock:
                if interaction.user.bot:
                    return await interaction_send(interaction, "機器人不能搶紅包", ephemeral=True)
                if interaction.user.id in self.claimed_users:
                    return await interaction_send(interaction, "你已經搶過這包了", ephemeral=True)
                if self.left_count <= 0 or self.left_amount <= 0:
                    return await interaction_send(interaction, "紅包已搶完", ephemeral=True)

                if self.left_count == 1:
                    amount = self.left_amount
                else:
                    max_pick = self.left_amount - (self.left_count - 1)
                    non_last_cap = max(1, int(self.left_amount * 0.4))
                    capped_max_pick = max(1, min(max_pick, non_last_cap))
                    amount = random.randint(1, capped_max_pick)
                self.left_amount -= amount
                self.left_count -= 1
                self.claimed_users.add(interaction.user.id)
                self.claim_results.append((interaction.user.id, amount))

                await credit_balance_with_log_async(interaction.user.id, amount, f"搶紅包 #{self.packet_id}")

                if self.left_count <= 0 or self.left_amount <= 0:
                    for child in self.children:
                        child.disabled = True
                    try:
                        await interaction.message.edit(
                            content=self.summary_text() + "\n✅ 紅包已被搶完！\n" + self.winners_text(),
                            view=self,
                        )
                    except Exception:
                        logger.exception("RedPacketView.claim 結算更新失敗 packet_id=%s", self.packet_id)
                    await interaction_send(interaction, f"🎉 你搶到 `{amount}` 東雲幣！", ephemeral=True)
                    return
                try:
                    await interaction.message.edit(content=self.summary_text(), view=self)
                except Exception:
                    logger.exception("RedPacketView.claim 更新摘要失敗 packet_id=%s", self.packet_id)
                await interaction_send(interaction, f"🎉 你搶到 `{amount}` 東雲幣！", ephemeral=True)

        async def on_timeout(self):
            for child in self.children:
                child.disabled = True
            try:
                if hasattr(self, "message") and self.message:
                    await self.message.edit(
                        content=self.summary_text() + "\n⌛ 紅包已逾時關閉。\n" + self.winners_text(),
                        view=self,
                    )
            except Exception:
                logger.exception("RedPacketView.on_timeout 更新訊息失敗 packet_id=%s", self.packet_id)

    @bot.tree.command(name="daily", description="每日簽到領取 100,000 東雲幣")
    async def daily(interaction: discord.Interaction):
        await interaction_defer_if_needed(interaction)
        result = await claim_daily_reward_async(interaction.user.id)
        if not result["claimed"]:
            ts = result["next_ts"]
            return await interaction_send(
                interaction,
                f"⚠️ 你今天已經簽到過囉！下次簽到時間：<t:{ts}:F> (<t:{ts}:R>)",
                ephemeral=True,
            )

        daily_reward = result["reward"]
        new_bal = result["balance"]
        ts = result["next_ts"]
        embed = discord.Embed(title="✅ 每日簽到成功", color=discord.Color.green())
        embed.add_field(name="獲得", value=f"`{daily_reward:,}` 東雲幣", inline=False)
        embed.add_field(name="目前餘額", value=f"`{new_bal:,}` 東雲幣", inline=False)
        embed.add_field(name="下次可領取", value=f"<t:{ts}:f>（<t:{ts}:R>）", inline=False)
        embed.set_footer(text=f"簽到者：{interaction.user.display_name}")
        await interaction_send(interaction, embed=embed)

    @bot.tree.command(name="hourly", description="每小時簽到（可依等級累積）")
    async def hourly(interaction: discord.Interaction):
        await interaction_defer_if_needed(interaction)
        result = await claim_hourly_reward_async(interaction.user.id)
        if not result.get("ok"):
            return await interaction_send(interaction, "資料初始化失敗", ephemeral=True)
        level_num = result["level"]
        if not result["claimed"]:
            mins = result["mins"]
            return await interaction_send(
                interaction,
                f"⏳ 目前尚無可領時段。下次可累積約 `{mins}` 分鐘後。\n"
                f"你目前 Lv.{level_num}，最多可累積 `{level_num}` 小時。",
                ephemeral=True
            )
        bank = result["bank"]
        reward_per_slot = result["reward_per_slot"]
        payout = result["payout"]
        new_bal = result["balance"]
        embed = discord.Embed(title="✅ 每小時簽到成功", color=discord.Color.green())
        embed.add_field(name="累積時段", value=f"`{bank}` 小時（上限 `{level_num}`）", inline=False)
        embed.add_field(name="每小時獎勵", value=f"`{reward_per_slot:,}` 東雲幣", inline=False)
        embed.add_field(name="本次獲得", value=f"`{payout:,}` 東雲幣", inline=False)
        embed.add_field(name="目前餘額", value=f"`{new_bal:,}` 東雲幣", inline=False)
        embed.set_footer(text=f"領取者：{interaction.user.display_name}")
        await interaction_send(interaction, embed=embed)

    @bot.tree.command(name="beg", description="街頭乞討")
    async def beg(interaction: discord.Interaction):
        result = await claim_beg_sync_async(interaction.user.id)
        if not result.get("ok"):
            return await interaction.response.send_message("太快了", ephemeral=True)
        if result.get("fail"):
            return await interaction.response.send_message("沒人鳥你 乞丐")
        earn = int(result.get("earned") or 0)
        return await interaction.response.send_message(f"你獲得了{earn}東雲幣!錢給你啦 乞丐!")



    @bot.tree.command(name="rescue", description="破產救濟計畫，餘額為 0 元時可領 1,000 (每人限領 10 次)")
    async def rescue(interaction: discord.Interaction):
        result = await claim_rescue_sync_async(interaction.user.id)
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "not_bankrupt":
                bal = int(result.get("balance") or 0)
                return await interaction.response.send_message(
                    f"💰 還沒破產（餘額: {bal}），請這位賭狗先去賭到傾家蕩產！完全歸零時再來領。",
                    ephemeral=True,
                )
            if reason == "limit_reached":
                return await interaction.response.send_message(
                    "🚫 抱歉，你的救濟次數已達 10 次上限。這輩子不能再領了，賭鬼！",
                    ephemeral=True,
                )
            if reason == "cooldown":
                rem = int(result.get("remain_sec") or 0)
                return await interaction.response.send_message(f"🕒 銀行還不想給你錢！請再等 `{int(rem//60)}` 分鐘。", ephemeral=True)
            return await interaction.response.send_message("暫時無法領取救濟，請稍後再試。", ephemeral=True)
        rescue_reward = int(result["reward"])
        claim_no = int(result["claim_no"])
        embed = discord.Embed(title="✅ 破產救濟發放", color=discord.Color.green())
        embed.add_field(name="獲得", value=f"`{rescue_reward:,}` 東雲幣", inline=False)
        embed.add_field(name="累計次數", value=f"`{claim_no}/10`", inline=False)
        embed.set_footer(text="請謹慎下注，避免再次破產")
        await interaction.response.send_message(embed=embed)

    # /bj 已抽離到 bot_modules/commands/blackjack.py

    @bot.tree.command(name="balance", description="查詢個人的戰績與餘額")
    @app_commands.describe(member="要查詢的成員（選填）", user_id="或填對方使用者 ID（選填）")
    async def balance(
        interaction: discord.Interaction,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
    ):
        resolved, err = await resolve_slash_target(
            interaction, member, user_id, required=False, in_guild_only=False
        )
        if err:
            return await interaction_send(interaction, err, ephemeral=True)
        await interaction_defer_if_needed(interaction)
        target: typing.Union[discord.Member, discord.User] = resolved or interaction.user
        if target.id == interaction.user.id:
            await ensure_user_exists_async(target.id, 50000)
        else:
            await ensure_user_exists_async(target.id, 0)
        stats = await get_user_stats_async(target.id)
        bal, total, wins, t_prof = stats
        wr = (wins/total*100) if total > 0 else 0
        embed = discord.Embed(title="📊 帳戶統計", color=0x2b2d31)
        av = target.display_avatar.url if getattr(target, "display_avatar", None) else None
        embed.set_author(name=target.display_name, icon_url=av)
        embed.add_field(name="目前餘額", value=f"`{bal:,}` 東雲幣", inline=False)
        embed.add_field(name="總遊玩局數", value=f"`{total:,}` 局", inline=False)
        embed.add_field(name="勝利場次", value=f"`{wins:,}` 場", inline=False)
        embed.add_field(name="勝率", value=f"`{wr:.1f}%`", inline=False)
        embed.add_field(name="歷史總盈虧", value=f"`{t_prof:,}` 東雲幣", inline=False)
        await interaction_send(interaction, embed=embed)

    @bot.tree.command(name="level", description="查詢等級與經驗值")
    @app_commands.describe(member="要查詢的成員（選填）", user_id="或填對方使用者 ID（選填）")
    async def level(
        interaction: discord.Interaction,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
    ):
        resolved, err = await resolve_slash_target(
            interaction, member, user_id, required=False, in_guild_only=False
        )
        if err:
            return await interaction_send(interaction, err, ephemeral=True)
        await interaction_defer_if_needed(interaction)
        target: typing.Union[discord.Member, discord.User] = resolved or interaction.user
        if target.id == interaction.user.id:
            await ensure_user_exists_async(target.id, 50000)
        else:
            await ensure_user_exists_async(target.id, 0)
        lv_row = await get_level_stats_async(target.id)
        exp = int(lv_row[0] or 0)
        level_num = int(lv_row[1] or 1)
        calc_lv, cur_progress, next_need = calc_level_from_exp(exp)
        level_num = max(level_num, calc_lv)
        claimed = await get_claimed_milestones_async(target.id)
        bar = build_exp_progress_bar(cur_progress, next_need) if level_num < MAX_LEVEL else "▓" * 12 + "  100%"
        need_more = (next_need - cur_progress) if level_num < MAX_LEVEL and next_need > 0 else 0
        ms_lines = []
        for m, coins in sorted(LEVEL_MILESTONE_COINS.items()):
            if m in claimed:
                ms_lines.append(f"**Lv.{m}** — ✅ 已領 {coins:,} 幣")
            elif level_num < m:
                ms_lines.append(f"**Lv.{m}** — 💰 達到 Lv.{m} 領 {coins:,} 幣")
            else:
                ms_lines.append(f"**Lv.{m}** — 已達此級（獎勵僅在**升級當下首次跨過**時發放，不事後補發）")
        emb = discord.Embed(title="🏅 等級與經驗", color=0x2b2d31)
        av = target.display_avatar.url if getattr(target, "display_avatar", None) else None
        emb.set_author(name=target.display_name, icon_url=av)
        if level_num >= MAX_LEVEL:
            emb.description = f"{target.mention} **Lv.{level_num}**（已滿級）\n✨ 總 EXP：`{exp:,}`"
        else:
            emb.description = (
                f"{target.mention} **Lv.{level_num}**\n"
                f"✨ 總 EXP：`{exp:,}`\n"
                f"📈 本級累積 **{cur_progress:,} / {next_need:,}** EXP，尚餘 **{need_more:,}** EXP 可升 Lv.{level_num + 1}\n"
                f"`{bar}`"
            )
        emb.add_field(
            name="階段里程碑 Lv.20／40／60／80／100",
            value="\n".join(ms_lines) or "未設定",
            inline=False,
        )
        await interaction_send(interaction, embed=emb)

    @bot.tree.command(name="transfer", description="轉帳給其他玩家")
    @app_commands.describe(
        member="收款人（選人，選填若已填 user_id）",
        user_id="或填收款人使用者 ID",
        amount="轉帳金額",
        note="備註（選填）",
    )
    async def transfer(
        interaction: discord.Interaction,
        amount: int,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
        note: str = "",
    ):
        m_user, err = await resolve_slash_target(
            interaction, member, user_id, required=True, in_guild_only=True
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        if not isinstance(m_user, discord.Member):
            return await interaction.response.send_message("轉帳對象必須是此伺服器成員。", ephemeral=True)
        member = m_user
        if amount <= 0:
            return await interaction.response.send_message("金額必須大於 0", ephemeral=True)
        if member.bot:
            return await interaction.response.send_message("不能轉帳給機器人", ephemeral=True)
        if member.id == interaction.user.id:
            return await interaction.response.send_message("不能轉帳給自己", ephemeral=True)
        note_text = (note or "").strip()
        if len(note_text) > 100:
            note_text = note_text[:100]
        result = await transfer_sync_async(interaction.user.id, member.id, amount, note_text)
        if not result.get("ok"):
            return await interaction.response.send_message("餘額不足，無法轉帳", ephemeral=True)
        sender_after = int(result["sender_after"])
        receiver_after = int(result["receiver_after"])

        now_text = now_tw_naive().strftime("%Y/%m/%d %H:%M:%S")

        embed = discord.Embed(
            title="✅ 轉帳成功",
            color=discord.Color.green()
        )
        embed.add_field(
            name="匯款方",
            value=(
                f"{interaction.user.mention}\n"
                f"轉帳後餘額：`{sender_after:,}` 東雲幣"
            ),
            inline=False
        )
        embed.add_field(
            name="收款方",
            value=(
                f"{member.mention}\n"
                f"收款後餘額：`{receiver_after:,}` 東雲幣"
            ),
            inline=False
        )
        embed.add_field(
            name="轉帳金額",
            value=f"`{amount:,}` 東雲幣",
            inline=False
        )
        embed.add_field(
            name="轉帳備註",
            value=note_text if note_text else "（無）",
            inline=False
        )
        embed.set_footer(text=f"交易時間：{now_text}")
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="redpacket", description="發送紅包！")
    @app_commands.describe(total_amount="紅包總金額", count="份數", seconds="有效秒數(最少10秒)")
    async def redpacket(interaction: discord.Interaction, total_amount: int, count: int, seconds: int = 60):
        if not FEATURE_TOGGLES.get("redpacket", True):
            return await interaction.response.send_message("⛔ `/redpacket` 目前暫時關閉中。", ephemeral=True)
        await ensure_user_exists_async(interaction.user.id, 50000)
        if total_amount < count or total_amount <= 0:
            return await interaction.response.send_message("總金額需大於 0，且至少要能每包 1 元。", ephemeral=True)
        if count < 1 or count > 100:
            return await interaction.response.send_message("份數需介於 1 到 100。", ephemeral=True)
        if not await try_deduct_balance_async(interaction.user.id, total_amount, "發送紅包扣款"):
            return await interaction.response.send_message("餘額不足，無法發紅包", ephemeral=True)
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
        except Exception:
            logger.exception("redpacket: 無法取得 original_response 供 timeout 編輯使用")

    @bot.tree.command(name="record", description="最近紀錄（最多 50 筆，10 筆一頁）")
    @app_commands.describe(member="要查詢的玩家（選填）", user_id="或填對方使用者 ID（選填）", page="頁碼（每頁 10 筆）")
    async def record_cmd(
        interaction: discord.Interaction,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
        page: int = 1,
    ):
        resolved, err = await resolve_slash_target(
            interaction, member, user_id, required=False, in_guild_only=False
        )
        if err:
            return await interaction_send(interaction, err, ephemeral=True)
        await interaction_defer_if_needed(interaction, ephemeral=True)
        target: typing.Union[discord.Member, discord.User] = resolved or interaction.user
        rows = await fetch_record_rows_async(target.id, 50)
        if not rows:
            return await interaction_send(interaction, "無紀錄", ephemeral=True)

        page_size = 10
        page = max(1, int(page))
        all_lines = []
        for i, r in enumerate(rows):
            time_text = r[2].strftime('%m/%d %H:%M') if r[2] else "N/A"
            all_lines.append(f"{i+1}. [{time_text}] {r[1]}: `{r[0]}`")
        view = LinePagerView(
            owner_id=interaction.user.id,
            title=f"📒 {target.display_name} 的最近紀錄",
            lines=all_lines,
            page_size=page_size,
            start_page=page,
            footer_prefix=f"共 {len(rows)} 筆（最多顯示 50）"
        )
        sent = await interaction_send(interaction, embed=view.build_embed(), view=view)
        try:
            view.message = sent or await interaction.original_response()
        except Exception:
            logger.exception("record: 無法取得 original_response 供翻頁更新使用")
