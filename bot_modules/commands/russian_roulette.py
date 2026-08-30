import random
import typing

import discord
from discord import app_commands


def register_russian_roulette_commands(bot, ctx: typing.Dict[str, typing.Any]) -> None:
    FEATURE_TOGGLES = ctx["FEATURE_TOGGLES"]
    get_is_event_active = ctx["get_is_event_active"]
    COINFLIP_MIN_BET = ctx["COINFLIP_MIN_BET"]
    COINFLIP_MAX_BET = ctx["COINFLIP_MAX_BET"]
    RUSSIAN_ROULETTE_CHAMBERS = ctx["RUSSIAN_ROULETTE_CHAMBERS"]
    ensure_user_exists_async = ctx["ensure_user_exists_async"]
    interaction_send = ctx["interaction_send"]
    interaction_defer_if_needed = ctx["interaction_defer_if_needed"]
    try_deduct_balance_async = ctx["try_deduct_balance_async"]
    credit_balance_with_log_async = ctx["credit_balance_with_log_async"]
    update_game_result_async = ctx["update_game_result_async"]
    record_rr_result_async = ctx["record_rr_result_async"]
    fetch_rr_stats_async = ctx["fetch_rr_stats_async"]
    fetch_rr_leaderboard_async = ctx["fetch_rr_leaderboard_async"]
    resolve_slash_target = ctx["resolve_slash_target"]

    class RrMatch:
        def __init__(self, challenger: discord.Member, opponent: discord.Member, bet: int):
            self.challenger = challenger
            self.opponent = opponent
            self.bet = int(bet)
            self.chambers = int(RUSSIAN_ROULETTE_CHAMBERS)
            self.bullet_at = random.randrange(self.chambers)
            self.trigger_index = 0
            self.turn_uid = challenger.id if random.random() < 0.5 else opponent.id
            self.history: typing.List[str] = []
            self.settled = False
            self.message: typing.Optional[discord.Message] = None
            self.view: typing.Optional[discord.ui.View] = None

        def other_uid(self, uid: int) -> int:
            return self.opponent.id if uid == self.challenger.id else self.challenger.id

        def member_for(self, uid: int) -> discord.Member:
            return self.challenger if uid == self.challenger.id else self.opponent

        def build_embed(
            self,
            *,
            finished: bool = False,
            loser_uid: typing.Optional[int] = None,
            awaiting_choice: bool = False,
            timeout_loss: bool = False,
        ) -> discord.Embed:
            if finished and loser_uid is not None:
                winner_uid = self.other_uid(loser_uid)
                if timeout_loss:
                    loss_line = f"{self.member_for(loser_uid).mention} 逾時未扣扳機，視同落敗。"
                else:
                    loss_line = f"{self.member_for(loser_uid).mention} 扣下扳機，當場出局。"
                emb = discord.Embed(
                    title="💥 俄羅斯輪盤 — 對決結束",
                    description=(
                        f"{loss_line}\n"
                        f"🏆 勝者 {self.member_for(winner_uid).mention} 贏得 **`{self.bet * 2:,}`** 東雲幣！"
                    ),
                    color=0xED4245,
                )
            else:
                turn_member = self.member_for(self.turn_uid)
                remaining = self.chambers - self.trigger_index
                emb = discord.Embed(
                    title="🔫 俄羅斯輪盤對決",
                    description=(
                        f"{self.challenger.mention} vs {self.opponent.mention}\n"
                        f"彩池：**`{self.bet * 2:,}`** 東雲幣（各 `{self.bet:,}`）"
                    ),
                    color=0x5865F2,
                )
                emb.add_field(name="輪到", value=turn_member.mention, inline=True)
                emb.add_field(name="剩餘膛室", value=str(remaining), inline=True)
                if awaiting_choice:
                    emb.set_footer(text="空膛！可選擇繼續射，或停手換對手")
            if self.history:
                emb.add_field(name="歷程", value="\n".join(self.history[-6:]), inline=False)
            return emb

        async def settle(self, loser_uid: int) -> None:
            if self.settled:
                return
            self.settled = True
            winner_uid = self.other_uid(loser_uid)
            pot = self.bet * 2
            await credit_balance_with_log_async(winner_uid, pot, "俄羅斯輪盤勝利")
            await update_game_result_async(winner_uid, self.bet, self.bet, True)
            await update_game_result_async(loser_uid, 0, -self.bet, False)
            await record_rr_result_async(winner_uid, is_win=True, profit_delta=self.bet)
            await record_rr_result_async(loser_uid, is_win=False, profit_delta=-self.bet)

        async def refund_both(self, reason: str) -> None:
            if self.settled:
                return
            self.settled = True
            await credit_balance_with_log_async(self.challenger.id, self.bet, reason)
            await credit_balance_with_log_async(self.opponent.id, self.bet, reason)

        async def settle_turn_timeout(self, reason: str) -> int:
            loser_uid = self.turn_uid
            actor = self.member_for(loser_uid)
            self.history.append(f"⌛ {actor.display_name} {reason}")
            await self.settle(loser_uid)
            return loser_uid

    async def _finish_turn_timeout(view: discord.ui.View, *, reason: str) -> None:
        m = view.match  # type: ignore[attr-defined]
        if m.settled:
            return
        loser_uid = await m.settle_turn_timeout(reason)
        for child in view.children:
            child.disabled = True
        try:
            if m.message:
                await m.message.edit(
                    content=None,
                    embed=m.build_embed(finished=True, loser_uid=loser_uid, timeout_loss=True),
                    view=view,
                )
        except Exception:
            pass
        view.stop()

    class RrInviteView(discord.ui.View):
        def __init__(self, match: RrMatch):
            super().__init__(timeout=120)
            self.match = match
            self.resolved = False
            self.message: typing.Optional[discord.Message] = None

        @discord.ui.button(label="接受", style=discord.ButtonStyle.success)
        async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            if interaction.user.id != m.opponent.id:
                return await interaction.response.send_message("這場對決不是邀請你的。", ephemeral=True)
            if self.resolved or m.settled:
                return await interaction.response.defer()
            if not await try_deduct_balance_async(m.opponent.id, m.bet, "俄羅斯輪盤下注"):
                return await interaction.response.send_message(
                    f"❌ 餘額不足，需要 `{m.bet:,}` 東雲幣才能接受。",
                    ephemeral=True,
                )
            self.resolved = True
            for child in self.children:
                child.disabled = True
            play_view = RrShootView(m)
            m.view = play_view
            await interaction.response.edit_message(
                embed=m.build_embed(),
                view=play_view,
            )
            try:
                m.message = await interaction.original_response()
            except Exception:
                pass
            self.stop()

        @discord.ui.button(label="拒絕", style=discord.ButtonStyle.danger)
        async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            if interaction.user.id != m.opponent.id:
                return await interaction.response.send_message("這場對決不是邀請你的。", ephemeral=True)
            if self.resolved or m.settled:
                return await interaction.response.defer()
            self.resolved = True
            await credit_balance_with_log_async(m.challenger.id, m.bet, "俄羅斯輪盤取消退款")
            m.settled = True
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=(
                    f"❌ {m.opponent.mention} 拒絕了 {m.challenger.mention} 的俄羅斯輪盤對決。"
                    f"已退回 `{m.bet:,}` 東雲幣。"
                ),
                embed=None,
                view=self,
            )
            self.stop()

        async def on_timeout(self):
            m = self.match
            if self.resolved or m.settled:
                return
            self.resolved = True
            await credit_balance_with_log_async(m.challenger.id, m.bet, "俄羅斯輪盤邀請逾時退款")
            m.settled = True
            for child in self.children:
                child.disabled = True
            try:
                if self.message:
                    await self.message.edit(
                        content=(
                            f"⌛ {m.opponent.mention} 未回應邀請，"
                            f"{m.challenger.mention} 的 `{m.bet:,}` 已退回。"
                        ),
                        embed=None,
                        view=self,
                    )
            except Exception:
                pass

    class RrShootView(discord.ui.View):
        def __init__(self, match: RrMatch):
            super().__init__(timeout=180)
            self.match = match

        async def _fire(self, interaction: discord.Interaction) -> None:
            m = self.match
            if m.settled:
                await interaction.response.send_message("對決已結束。", ephemeral=True)
                return
            if interaction.user.id != m.turn_uid:
                other = m.member_for(m.turn_uid)
                await interaction.response.send_message(
                    f"現在輪到 {other.mention}。",
                    ephemeral=True,
                )
                return

            actor = m.member_for(interaction.user.id)
            if m.trigger_index == m.bullet_at:
                m.history.append(f"💥 第 {m.trigger_index + 1} 發 — {actor.display_name} 中彈！")
                await m.settle(interaction.user.id)
                for child in self.children:
                    child.disabled = True
                await interaction.response.edit_message(
                    embed=m.build_embed(finished=True, loser_uid=interaction.user.id),
                    view=self,
                )
                self.stop()
                return

            m.history.append(f"😮‍💨 第 {m.trigger_index + 1} 發 — {actor.display_name} 空膛")
            m.trigger_index += 1
            choice_view = RrChoiceView(m)
            m.view = choice_view
            await interaction.response.edit_message(
                embed=m.build_embed(awaiting_choice=True),
                view=choice_view,
            )
            self.stop()

        @discord.ui.button(label="🔫 扣扳機", style=discord.ButtonStyle.danger)
        async def pull_trigger(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._fire(interaction)

        async def on_timeout(self):
            await _finish_turn_timeout(self, reason="逾時未扣扳機")

    class RrChoiceView(discord.ui.View):
        def __init__(self, match: RrMatch):
            super().__init__(timeout=180)
            self.match = match

        @discord.ui.button(label="🔫 繼續射", style=discord.ButtonStyle.danger)
        async def continue_shoot(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            if m.settled:
                return await interaction.response.send_message("對決已結束。", ephemeral=True)
            if interaction.user.id != m.turn_uid:
                other = m.member_for(m.turn_uid)
                return await interaction.response.send_message(
                    f"請由 {other.mention} 決定是否繼續。",
                    ephemeral=True,
                )
            shoot_view = RrShootView(m)
            m.view = shoot_view
            self.stop()
            await shoot_view._fire(interaction)

        @discord.ui.button(label="🛑 停手換人", style=discord.ButtonStyle.secondary)
        async def pass_turn(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            if m.settled:
                return await interaction.response.send_message("對決已結束。", ephemeral=True)
            if interaction.user.id != m.turn_uid:
                other = m.member_for(m.turn_uid)
                return await interaction.response.send_message(
                    f"請由 {other.mention} 決定是否停手。",
                    ephemeral=True,
                )
            actor = m.member_for(interaction.user.id)
            m.history.append(f"🛑 {actor.display_name} 停手，輪換對手")
            m.turn_uid = m.other_uid(m.turn_uid)
            shoot_view = RrShootView(m)
            m.view = shoot_view
            await interaction.response.edit_message(
                embed=m.build_embed(),
                view=shoot_view,
            )
            self.stop()

        async def on_timeout(self):
            await _finish_turn_timeout(self, reason="逾時未選擇（視同落敗）")

    rr_group = app_commands.Group(name="russian_roulette", description="俄羅斯輪盤對決與戰績")

    @rr_group.command(
        name="duel",
        description="發起俄羅斯輪盤對決：六膛一彈，空膛可繼續射或停手換人；逾時未行動判負",
    )
    @app_commands.describe(member="對手", bet="雙方下注金額（相同）")
    async def russian_roulette_duel_slash(
        interaction: discord.Interaction,
        member: discord.Member,
        bet: app_commands.Range[int, 1, 10_000_000],
    ):
        if not FEATURE_TOGGLES.get("russian_roulette", True) or not get_is_event_active():
            return await interaction_send(interaction, "🚫 賭場目前休息中。", ephemeral=True)
        if not interaction.guild:
            return await interaction_send(interaction, "請在伺服器頻道使用。", ephemeral=True)
        if member.bot or member.id == interaction.user.id:
            return await interaction_send(interaction, "請指定其他玩家。", ephemeral=True)
        amount = int(bet)
        if amount < COINFLIP_MIN_BET or amount > COINFLIP_MAX_BET:
            return await interaction_send(
                interaction,
                f"下注須介於 `{COINFLIP_MIN_BET:,}`～`{COINFLIP_MAX_BET:,}` 東雲幣。",
                ephemeral=True,
            )
        await interaction_defer_if_needed(interaction)
        await ensure_user_exists_async(interaction.user.id, 50000)
        await ensure_user_exists_async(member.id, 50000)
        if not await try_deduct_balance_async(interaction.user.id, amount, "俄羅斯輪盤下注"):
            return await interaction_send(
                interaction,
                f"❌ 餘額不足，需要 `{amount:,}` 東雲幣才能發起對決。",
                ephemeral=True,
            )
        match = RrMatch(interaction.user, member, amount)
        view = RrInviteView(match)
        sent = await interaction_send(
            interaction,
            content=(
                f"🔫 {interaction.user.mention} 向 {member.mention} 發起 **俄羅斯輪盤對決**！\n"
                f"注額：各 `{amount:,}` 東雲幣｜**{RUSSIAN_ROULETTE_CHAMBERS} 膛 1 彈**\n"
                f"規則：輪流 **扣扳機**；**空膛**可 **繼續射** 或 **停手換人**，中彈者輸；**180 秒**未行動視同落敗\n"
                f"{member.mention} 請於 **120 秒** 內 **接受** 或 **拒絕**。"
            ),
            view=view,
        )
        try:
            view.message = sent or await interaction.original_response()
            match.message = view.message
        except Exception:
            pass

    @rr_group.command(name="stats", description="查看俄羅斯輪盤勝率與排行榜")
    @app_commands.describe(member="要查看的玩家（留空看自己）", user_id="或填使用者 ID／貼提及")
    async def russian_roulette_stats_slash(
        interaction: discord.Interaction,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
    ):
        target_user, err = await resolve_slash_target(
            interaction, member, user_id, required=False, in_guild_only=False
        )
        if err:
            return await interaction_send(interaction, err, ephemeral=True)
        target = target_user or interaction.user
        await ensure_user_exists_async(target.id, 50000)
        await interaction_defer_if_needed(interaction)
        stats = await fetch_rr_stats_async(target.id)
        board = await fetch_rr_leaderboard_async(10)

        emb = discord.Embed(
            title=f"🔫 {target.display_name} 的俄羅斯輪盤戰績",
            color=0x5865F2,
        )
        emb.set_thumbnail(url=target.display_avatar.url)
        if stats["games"] <= 0:
            personal = "尚未完成任何對決。"
        else:
            personal = (
                f"局數 `{stats['games']}`｜勝 `{stats['wins']}`｜負 `{stats['losses']}`\n"
                f"勝率 **`{stats['win_rate']:.1f}%`**｜累計 **`{stats['profit']:+,}`** 東雲幣"
            )
            if stats.get("win_rank"):
                personal += f"\n勝場榜 `# {stats['win_rank']}`"
            if stats.get("rate_rank"):
                personal += f"｜勝率榜 `# {stats['rate_rank']}`（至少 3 局）"
        emb.add_field(name="個人戰績", value=personal, inline=False)

        if board:
            lines = []
            for i, row in enumerate(board, start=1):
                lines.append(
                    f"{i}. <@{row['user_id']}> — "
                    f"`{row['wins']}` 勝 / `{row['games']}` 局 "
                    f"（`{row['win_rate']:.1f}%`）"
                )
            emb.add_field(
                name="🏆 勝場榜 Top 10（至少 3 局）",
                value="\n".join(lines)[:1024],
                inline=False,
            )
        else:
            emb.add_field(name="🏆 勝場榜", value="（尚無足夠戰績）", inline=False)
        emb.set_footer(text="僅統計已完成對決；邀請逾時／拒絕不計入")
        await interaction_send(interaction, embed=emb)

    bot.tree.add_command(rr_group)


__all__ = ["register_russian_roulette_commands"]
