import random
import typing

import discord
from discord import app_commands

from bot_modules import rr_match_repo

RR_ROYALE_MIN_PLAYERS = 3
RR_ROYALE_MAX_PLAYERS = 6
RR_ROYALE_BULLETS_PER_EXTRA_PLAYER = 3
DECLINE_REASONS = (
    ("不想玩", "今天不想玩。"),
    ("餘額不足", "錢不夠，下次再來。"),
    ("稍後再說", "現在沒空。"),
    ("太可怕了", "這太刺激了，我撤。"),
)


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
    fetch_rr_rate_leaderboard_async = ctx["fetch_rr_rate_leaderboard_async"]
    save_rr_match_async = ctx["save_rr_match_async"]
    delete_rr_match_async = ctx["delete_rr_match_async"]
    fetch_active_rr_matches_async = ctx["fetch_active_rr_matches_async"]
    resolve_slash_target = ctx["resolve_slash_target"]

    def _hit_prob_pct(remaining_chambers: int, remaining_bullets: int) -> float:
        if remaining_chambers <= 0 or remaining_bullets <= 0:
            return 0.0
        return remaining_bullets * 100.0 / remaining_chambers

    def _royale_bullet_count(player_count: int) -> int:
        return 1 + max(0, player_count - RR_ROYALE_MIN_PLAYERS) * RR_ROYALE_BULLETS_PER_EXTRA_PLAYER

    def _royale_chamber_count(player_count: int, bullet_count: int) -> int:
        return max(RUSSIAN_ROULETTE_CHAMBERS, bullet_count + player_count * 2)

    def _busy_msg(uid: int) -> str:
        return f"<@{uid}> 已有進行中的俄羅斯輪盤，請先完成或等待該場結束。"

    class RrMatch:
        def __init__(
            self,
            *,
            match_id: str,
            mode: str,
            guild_id: int,
            channel_id: int,
            bet_base: int,
            participant_ids: typing.List[int],
            stakes: typing.Dict[int, int],
            chambers: int = RUSSIAN_ROULETTE_CHAMBERS,
            bullet_positions: typing.Optional[typing.Iterable[int]] = None,
            bullet_at: typing.Optional[int] = None,
            trigger_index: int = 0,
            turn_uid: typing.Optional[int] = None,
            alive_ids: typing.Optional[typing.List[int]] = None,
            history: typing.Optional[typing.List[str]] = None,
            settled: bool = False,
            invite_resolved: bool = False,
            lobby_started: bool = False,
            host_id: typing.Optional[int] = None,
            challenger_id: typing.Optional[int] = None,
            opponent_id: typing.Optional[int] = None,
        ):
            self.match_id = match_id
            self.mode = mode
            self.guild_id = guild_id
            self.channel_id = channel_id
            self.bet_base = int(bet_base)
            self.chambers = int(chambers)
            if bullet_positions is not None:
                self.bullet_positions = {int(x) for x in bullet_positions}
            elif bullet_at is not None:
                self.bullet_positions = {int(bullet_at)}
            elif mode == "royale" and not lobby_started:
                self.bullet_positions = set()
            else:
                self.bullet_positions = {random.randrange(self.chambers)}
            self.trigger_index = int(trigger_index)
            self.participant_ids = [int(x) for x in participant_ids]
            self.stakes = {int(k): int(v) for k, v in stakes.items()}
            self.alive_ids = list(alive_ids if alive_ids is not None else self.participant_ids)
            self.history = list(history or [])
            self.settled = bool(settled)
            self.invite_resolved = bool(invite_resolved)
            self.lobby_started = bool(lobby_started)
            self.host_id = host_id
            self.challenger_id = challenger_id
            self.opponent_id = opponent_id
            self.turn_uid = turn_uid
            if self.turn_uid is None and self.mode == "duel" and self.invite_resolved:
                self.turn_uid = random.choice(self.participant_ids)
            self.message: typing.Optional[discord.Message] = None
            self.view: typing.Optional[discord.ui.View] = None

        @property
        def pot(self) -> int:
            return sum(self.stakes.values())

        @property
        def remaining_chambers(self) -> int:
            return max(0, self.chambers - self.trigger_index)

        def remaining_bullets(self) -> int:
            return sum(1 for pos in self.bullet_positions if pos >= self.trigger_index)

        def configure_royale_gun(self, *, use_alive: bool = False) -> None:
            player_count = len(self.alive_ids) if use_alive else len(self.participant_ids)
            bullet_count = _royale_bullet_count(player_count)
            self.chambers = _royale_chamber_count(player_count, bullet_count)
            self.bullet_positions = set(random.sample(range(self.chambers), bullet_count))
            self.trigger_index = 0

        def ensure_chambers_for_royale(self) -> bool:
            """若膛室打空但尚未分出勝負，重新装弹。"""
            if self.mode != "royale" or len(self.alive_ids) <= 1:
                return False
            if self.trigger_index < self.chambers:
                return False
            self.history.append("🔄 膛室打空，重新装弹！")
            self.configure_royale_gun(use_alive=True)
            return True

        def stake_of(self, uid: int) -> int:
            return int(self.stakes.get(int(uid), 0))

        def mention_line(self) -> str:
            return " ".join(f"<@{uid}>" for uid in self.participant_ids)

        def other_uid_duel(self, uid: int) -> int:
            return self.participant_ids[1] if uid == self.participant_ids[0] else self.participant_ids[0]

        def next_alive_uid(self, after_uid: int) -> int:
            if not self.alive_ids:
                return after_uid
            if after_uid not in self.alive_ids:
                return self.alive_ids[0]
            idx = self.alive_ids.index(after_uid)
            return self.alive_ids[(idx + 1) % len(self.alive_ids)]

        def to_payload(self) -> typing.Dict[str, typing.Any]:
            return {
                "bet_base": self.bet_base,
                "chambers": self.chambers,
                "bullet_positions": sorted(self.bullet_positions),
                "trigger_index": self.trigger_index,
                "turn_uid": self.turn_uid,
                "participant_ids": self.participant_ids,
                "stakes": {str(k): v for k, v in self.stakes.items()},
                "alive_ids": self.alive_ids,
                "history": self.history,
                "settled": self.settled,
                "invite_resolved": self.invite_resolved,
                "lobby_started": self.lobby_started,
                "host_id": self.host_id,
                "challenger_id": self.challenger_id,
                "opponent_id": self.opponent_id,
            }

        @classmethod
        def from_record(cls, record: typing.Dict[str, typing.Any]) -> "RrMatch":
            payload = record.get("payload") or {}
            stakes_raw = payload.get("stakes") or {}
            stakes = {int(k): int(v) for k, v in stakes_raw.items()}
            bullet_positions = payload.get("bullet_positions")
            legacy_bullet_at = payload.get("bullet_at")
            m = cls(
                match_id=str(record["match_id"]),
                mode=str(record["mode"]),
                guild_id=int(record["guild_id"]),
                channel_id=int(record["channel_id"]),
                bet_base=int(payload.get("bet_base") or 0),
                participant_ids=[int(x) for x in payload.get("participant_ids") or []],
                stakes=stakes,
                chambers=int(payload.get("chambers") or RUSSIAN_ROULETTE_CHAMBERS),
                bullet_positions=bullet_positions if bullet_positions is not None else None,
                bullet_at=int(legacy_bullet_at) if legacy_bullet_at is not None else None,
                trigger_index=int(payload.get("trigger_index") or 0),
                turn_uid=payload.get("turn_uid"),
                alive_ids=[int(x) for x in payload.get("alive_ids") or []],
                history=list(payload.get("history") or []),
                settled=bool(payload.get("settled")),
                invite_resolved=bool(payload.get("invite_resolved")),
                lobby_started=bool(payload.get("lobby_started")),
                host_id=payload.get("host_id"),
                challenger_id=payload.get("challenger_id"),
                opponent_id=payload.get("opponent_id"),
            )
            if m.turn_uid is not None:
                m.turn_uid = int(m.turn_uid)
            if m.host_id is not None:
                m.host_id = int(m.host_id)
            if m.challenger_id is not None:
                m.challenger_id = int(m.challenger_id)
            if m.opponent_id is not None:
                m.opponent_id = int(m.opponent_id)
            if (
                m.mode == "royale"
                and m.lobby_started
                and not m.bullet_positions
                and not m.settled
            ):
                m.configure_royale_gun(use_alive=True)
            return m

        async def persist(self, phase: str) -> None:
            msg_id = self.message.id if self.message else None
            await save_rr_match_async(
                match_id=self.match_id,
                guild_id=self.guild_id,
                channel_id=self.channel_id,
                message_id=msg_id,
                mode=self.mode,
                phase=phase,
                payload=self.to_payload(),
            )

        async def clear_persist(self) -> None:
            await delete_rr_match_async(self.match_id, self.to_payload())

        def build_embed(
            self,
            interaction: typing.Optional[discord.Interaction] = None,
            *,
            finished: bool = False,
            loser_uid: typing.Optional[int] = None,
            awaiting_choice: bool = False,
            timeout_loss: bool = False,
            winner_uid: typing.Optional[int] = None,
        ) -> discord.Embed:
            if finished:
                if self.mode == "royale" and winner_uid is not None:
                    loss_line = ""
                    if loser_uid is not None:
                        if timeout_loss:
                            loss_line = f"<@{loser_uid}> 逾時未行動，淘汰。\n"
                        else:
                            loss_line = f"<@{loser_uid}> 中彈出局。\n"
                    emb = discord.Embed(
                        title="💥 俄羅斯輪盤淘汰賽 — 結束",
                        description=(
                            f"{loss_line}"
                            f"🏆 最後倖存者 <@{winner_uid}> 贏得 **`{self.pot:,}`** 東雲幣！"
                        ),
                        color=0xED4245,
                    )
                elif loser_uid is not None:
                    win_uid = self.other_uid_duel(loser_uid)
                    if timeout_loss:
                        loss_line = f"<@{loser_uid}> 逾時未扣扳機，視同落敗。"
                    else:
                        loss_line = f"<@{loser_uid}> 扣下扳機，當場出局。"
                    emb = discord.Embed(
                        title="💥 俄羅斯輪盤 — 對決結束",
                        description=(
                            f"{loss_line}\n"
                            f"🏆 勝者 <@{win_uid}> 贏得 **`{self.pot:,}`** 東雲幣！"
                        ),
                        color=0xED4245,
                    )
                else:
                    emb = discord.Embed(title="💥 俄羅斯輪盤 — 結束", color=0xED4245)
            elif self.mode == "royale" and not self.lobby_started:
                n = len(self.participant_ids)
                bullets = _royale_bullet_count(n)
                emb = discord.Embed(
                    title="👥 俄羅斯輪盤淘汰賽 — 報名中",
                    description=(
                        f"主持人 <@{self.host_id}>｜入場費 **`{self.bet_base:,}`** 東雲幣\n"
                        f"目前 **`{n}` / {RR_ROYALE_MAX_PLAYERS}** 人"
                        f"（至少 {RR_ROYALE_MIN_PLAYERS} 人開局）\n"
                        f"開局子彈：**`{bullets}`** 發"
                        f"（{RR_ROYALE_MIN_PLAYERS} 人 1 發，每多 1 人 +{RR_ROYALE_BULLETS_PER_EXTRA_PLAYER} 發）\n"
                        f"參加者：{self.mention_line()}"
                    ),
                    color=0xFEE75C,
                )
            else:
                title = "👥 俄羅斯輪盤淘汰賽" if self.mode == "royale" else "🔫 俄羅斯輪盤對決"
                desc = f"彩池：**`{self.pot:,}`** 東雲幣\n"
                if self.mode == "duel":
                    desc = (
                        f"<@{self.participant_ids[0]}> vs <@{self.participant_ids[1]}>\n"
                        f"彩池：**`{self.pot:,}`** 東雲幣"
                    )
                elif self.mode == "royale":
                    alive_line = " ".join(f"<@{uid}>" for uid in self.alive_ids)
                    desc += f"倖存：**{alive_line}**"
                emb = discord.Embed(title=title, description=desc, color=0x5865F2)
                if self.turn_uid is not None:
                    emb.add_field(name="輪到", value=f"<@{self.turn_uid}>", inline=True)
                remaining = self.remaining_chambers
                rem_bullets = self.remaining_bullets()
                emb.add_field(name="剩餘膛室", value=str(remaining), inline=True)
                if self.mode == "royale":
                    emb.add_field(
                        name="彈倉",
                        value=f"`{len(self.bullet_positions)}` 彈 / `{self.chambers}` 膛",
                        inline=True,
                    )
                prob = _hit_prob_pct(remaining, rem_bullets)
                if remaining and rem_bullets:
                    prob_text = f"`{prob:.1f}%`（{rem_bullets}/{remaining}）"
                elif remaining:
                    prob_text = f"`0%`（0/{remaining}）"
                else:
                    prob_text = "`0%`"
                emb.add_field(name="下一發中彈機率", value=prob_text, inline=True)
                if awaiting_choice:
                    emb.set_footer(text="空膛！可繼續射、加注繼續，或停手換人")
            if self.history:
                emb.add_field(name="歷程", value="\n".join(self.history[-8:]), inline=False)
            return emb

        async def settle_duel(self, loser_uid: int) -> int:
            if self.settled:
                return self.other_uid_duel(loser_uid)
            self.settled = True
            winner_uid = self.other_uid_duel(loser_uid)
            pot = self.pot
            win_stake = self.stake_of(winner_uid)
            lose_stake = self.stake_of(loser_uid)
            await credit_balance_with_log_async(winner_uid, pot, "俄羅斯輪盤勝利")
            await update_game_result_async(winner_uid, 0, pot - win_stake, True, share_recovery=False)
            await update_game_result_async(loser_uid, 0, -lose_stake, False, share_recovery=False)
            await record_rr_result_async(winner_uid, is_win=True, profit_delta=pot - win_stake)
            await record_rr_result_async(loser_uid, is_win=False, profit_delta=-lose_stake)
            await self.clear_persist()
            return winner_uid

        async def eliminate_royale(self, uid: int, *, timeout: bool = False) -> typing.Optional[int]:
            uid = int(uid)
            was_turn = self.turn_uid == uid
            next_turn = self.next_alive_uid(uid) if was_turn else self.turn_uid
            if uid in self.alive_ids:
                self.alive_ids = [x for x in self.alive_ids if x != uid]
            lose_stake = self.stake_of(uid)
            await update_game_result_async(uid, 0, -lose_stake, False, share_recovery=False)
            await record_rr_result_async(uid, is_win=False, profit_delta=-lose_stake)
            if len(self.alive_ids) == 1:
                self.settled = True
                winner_uid = self.alive_ids[0]
                pot = self.pot
                win_stake = self.stake_of(winner_uid)
                await credit_balance_with_log_async(winner_uid, pot, "俄羅斯輪盤淘汰賽勝利")
                await update_game_result_async(winner_uid, 0, pot - win_stake, True, share_recovery=False)
                await record_rr_result_async(winner_uid, is_win=True, profit_delta=pot - win_stake)
                await self.clear_persist()
                return winner_uid
            if was_turn and self.alive_ids:
                self.turn_uid = next_turn if next_turn in self.alive_ids else self.alive_ids[0]
            return None

        async def refund_participants(self, reason: str, uids: typing.Iterable[int]) -> None:
            if self.settled:
                return
            self.settled = True
            for uid in uids:
                amt = self.stake_of(uid)
                if amt > 0:
                    await credit_balance_with_log_async(uid, amt, reason)
            await self.clear_persist()

    async def _finish_turn_timeout(view: discord.ui.View, *, reason: str) -> None:
        m: RrMatch = view.match  # type: ignore[attr-defined]
        if m.settled:
            return
        actor_uid = int(m.turn_uid or 0)
        m.history.append(f"⌛ <@{actor_uid}> {reason}")
        if m.mode == "duel":
            await m.settle_duel(actor_uid)
            rematch_view = RrRematchView(m.guild_id, m.channel_id, m.participant_ids[0], m.participant_ids[1], m.bet_base)
            rematch_view.message = m.message
            for child in view.children:
                child.disabled = True
            try:
                if m.message:
                    await m.message.edit(
                        content=None,
                        embed=m.build_embed(finished=True, loser_uid=actor_uid, timeout_loss=True),
                        view=rematch_view,
                    )
            except Exception:
                pass
        else:
            winner_uid = await m.eliminate_royale(actor_uid, timeout=True)
            for child in view.children:
                child.disabled = True
            try:
                if m.message:
                    if winner_uid:
                        await m.message.edit(
                            content=None,
                            embed=m.build_embed(finished=True, loser_uid=actor_uid, winner_uid=winner_uid, timeout_loss=True),
                            view=None,
                        )
                    else:
                        play_view = RrShootView(m)
                        m.view = play_view
                        await m.message.edit(embed=m.build_embed(), view=play_view)
                        await m.persist("playing")
            except Exception:
                pass
        view.stop()

    class DeclineReasonSelect(discord.ui.Select):
        def __init__(self, match: RrMatch):
            self.match = match
            options = [
                discord.SelectOption(label=label, value=label, description=desc[:100])
                for label, desc in DECLINE_REASONS
            ]
            super().__init__(
                placeholder="拒絕並選理由…",
                options=options,
                min_values=1,
                max_values=1,
            )

        async def callback(self, interaction: discord.Interaction):
            m = self.match
            view: RrInviteView = self.view  # type: ignore[assignment]
            if interaction.user.id != m.opponent_id:
                return await interaction.response.send_message("這場對決不是邀請你的。", ephemeral=True)
            if view.resolved or m.settled:
                return await interaction.response.defer()
            view.resolved = True
            reason = self.values[0]
            await credit_balance_with_log_async(m.challenger_id, m.bet_base, "俄羅斯輪盤取消退款")
            m.settled = True
            await m.clear_persist()
            for child in view.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=(
                    f"❌ <@{m.opponent_id}> 拒絕了 <@{m.challenger_id}> 的對決。"
                    f"理由：**{reason}**\n"
                    f"已退回 `{m.bet_base:,}` 東雲幣。"
                ),
                embed=None,
                view=view,
            )
            view.stop()

    class RrRematchView(discord.ui.View):
        def __init__(self, guild_id: int, channel_id: int, uid_a: int, uid_b: int, bet: int):
            super().__init__(timeout=120)
            self.guild_id = guild_id
            self.channel_id = channel_id
            self.uid_a = uid_a
            self.uid_b = uid_b
            self.bet = int(bet)
            self.accepted: typing.Set[int] = set()
            self.message: typing.Optional[discord.Message] = None

        async def _try_start(self, interaction: discord.Interaction) -> None:
            if self.accepted != {self.uid_a, self.uid_b}:
                await interaction.response.send_message(
                    f"重賽確認：`{len(self.accepted)}/2`（需雙方都按 🔁 重賽）",
                    ephemeral=True,
                )
                return
            busy = rr_match_repo.any_user_busy([self.uid_a, self.uid_b])
            if busy:
                return await interaction.response.send_message(_busy_msg(busy), ephemeral=True)
            guild = interaction.guild or bot.get_guild(self.guild_id)
            if not guild:
                return await interaction.response.send_message("無法取得伺服器。", ephemeral=True)
            member_a = guild.get_member(self.uid_a)
            member_b = guild.get_member(self.uid_b)
            if not member_a or not member_b:
                return await interaction.response.send_message("找不到對戰玩家。", ephemeral=True)
            if not await try_deduct_balance_async(self.uid_a, self.bet, "俄羅斯輪盤重賽下注"):
                return await interaction.response.send_message("發起方餘額不足，重賽取消。", ephemeral=True)
            if not await try_deduct_balance_async(self.uid_b, self.bet, "俄羅斯輪盤重賽下注"):
                await credit_balance_with_log_async(self.uid_a, self.bet, "俄羅斯輪盤重賽退款")
                return await interaction.response.send_message("對手餘額不足，重賽取消。", ephemeral=True)
            match_id = rr_match_repo.new_match_id()
            match = RrMatch(
                match_id=match_id,
                mode="duel",
                guild_id=guild.id,
                channel_id=self.channel_id,
                bet_base=self.bet,
                participant_ids=[self.uid_a, self.uid_b],
                stakes={self.uid_a: self.bet, self.uid_b: self.bet},
                invite_resolved=True,
                challenger_id=self.uid_a,
                opponent_id=self.uid_b,
                turn_uid=random.choice([self.uid_a, self.uid_b]),
            )
            play_view = RrShootView(match)
            match.view = play_view
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=f"🔁 重賽開始！各 `{self.bet:,}` 東雲幣",
                embed=match.build_embed(),
                view=play_view,
            )
            try:
                match.message = await interaction.original_response()
            except Exception:
                pass
            await match.persist("playing")
            self.stop()

        @discord.ui.button(label="🔁 重賽", style=discord.ButtonStyle.primary)
        async def rematch(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id not in (self.uid_a, self.uid_b):
                return await interaction.response.send_message("這場重賽與你無關。", ephemeral=True)
            self.accepted.add(interaction.user.id)
            await self._try_start(interaction)

        async def on_timeout(self):
            for child in self.children:
                child.disabled = True
            try:
                if self.message:
                    await self.message.edit(view=self)
            except Exception:
                pass
            self.stop()

    class RrInviteView(discord.ui.View):
        def __init__(self, match: RrMatch):
            super().__init__(timeout=120)
            self.match = match
            self.resolved = False
            self.add_item(DeclineReasonSelect(match))

        @discord.ui.button(label="接受", style=discord.ButtonStyle.success)
        async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            if interaction.user.id != m.opponent_id:
                return await interaction.response.send_message("這場對決不是邀請你的。", ephemeral=True)
            if self.resolved or m.settled:
                return await interaction.response.defer()
            if not await try_deduct_balance_async(m.opponent_id, m.bet_base, "俄羅斯輪盤下注"):
                return await interaction.response.send_message(
                    f"❌ 餘額不足，需要 `{m.bet_base:,}` 東雲幣才能接受。",
                    ephemeral=True,
                )
            self.resolved = True
            m.invite_resolved = True
            m.stakes[int(m.opponent_id)] = m.bet_base
            m.turn_uid = random.choice(m.participant_ids)
            for child in self.children:
                child.disabled = True
            play_view = RrShootView(m)
            m.view = play_view
            await interaction.response.edit_message(
                content=None,
                embed=m.build_embed(interaction),
                view=play_view,
            )
            try:
                m.message = await interaction.original_response()
            except Exception:
                pass
            await m.persist("playing")
            self.stop()

        async def on_timeout(self):
            m = self.match
            if self.resolved or m.settled:
                return
            self.resolved = True
            await m.refund_participants("俄羅斯輪盤邀請逾時退款", [m.challenger_id])
            for child in self.children:
                child.disabled = True
            try:
                if m.message:
                    await m.message.edit(
                        content=(
                            f"⌛ <@{m.opponent_id}> 未回應邀請，"
                            f"<@{m.challenger_id}> 的 `{m.bet_base:,}` 已退回。"
                        ),
                        embed=None,
                        view=self,
                    )
            except Exception:
                pass

    class RrRoyaleLobbyView(discord.ui.View):
        def __init__(self, match: RrMatch):
            super().__init__(timeout=600)
            self.match = match

        @discord.ui.button(label="✅ 加入", style=discord.ButtonStyle.success)
        async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            if m.lobby_started or m.settled:
                return await interaction.response.send_message("這場已開始或結束。", ephemeral=True)
            uid = interaction.user.id
            if uid == m.host_id:
                return await interaction.response.send_message("主持人無需重複加入。", ephemeral=True)
            if uid in m.participant_ids:
                return await interaction.response.send_message("你已經在名單裡。", ephemeral=True)
            if len(m.participant_ids) >= RR_ROYALE_MAX_PLAYERS:
                return await interaction.response.send_message("名額已滿。", ephemeral=True)
            if rr_match_repo.user_active_match_id(uid):
                return await interaction.response.send_message("你已有進行中的輪盤。", ephemeral=True)
            if not await try_deduct_balance_async(uid, m.bet_base, "俄羅斯輪盤淘汰賽入場"):
                return await interaction.response.send_message(
                    f"❌ 餘額不足，需要 `{m.bet_base:,}` 東雲幣。",
                    ephemeral=True,
                )
            m.participant_ids.append(uid)
            m.stakes[uid] = m.bet_base
            m.alive_ids = list(m.participant_ids)
            await interaction.response.edit_message(embed=m.build_embed(interaction), view=self)
            try:
                m.message = await interaction.original_response()
            except Exception:
                pass
            await m.persist("lobby")

        @discord.ui.button(label="🚀 開始", style=discord.ButtonStyle.danger)
        async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            if m.lobby_started or m.settled:
                return await interaction.response.send_message("這場已開始或結束。", ephemeral=True)
            if interaction.user.id != m.host_id:
                return await interaction.response.send_message("只有主持人可以開始。", ephemeral=True)
            if len(m.participant_ids) < RR_ROYALE_MIN_PLAYERS:
                return await interaction.response.send_message(
                    f"至少需要 {RR_ROYALE_MIN_PLAYERS} 人才能開始。",
                    ephemeral=True,
                )
            m.lobby_started = True
            m.configure_royale_gun()
            m.turn_uid = random.choice(m.alive_ids)
            play_view = RrShootView(m)
            m.view = play_view
            bullet_count = len(m.bullet_positions)
            await interaction.response.edit_message(
                content=(
                    f"🚀 淘汰賽開始！共 {len(m.participant_ids)} 人｜"
                    f"**{m.chambers}** 膛 **{bullet_count}** 彈"
                ),
                embed=m.build_embed(interaction),
                view=play_view,
            )
            try:
                m.message = await interaction.original_response()
            except Exception:
                pass
            await m.persist("playing")
            self.stop()

        async def on_timeout(self):
            m = self.match
            if m.settled or m.lobby_started:
                return
            await m.refund_participants("俄羅斯輪盤淘汰賽報名逾時退款", m.participant_ids)
            for child in self.children:
                child.disabled = True
            try:
                if m.message:
                    await m.message.edit(content="⌛ 淘汰賽報名逾時，已退款。", embed=None, view=self)
            except Exception:
                pass

    class RrShootView(discord.ui.View):
        def __init__(self, match: RrMatch):
            super().__init__(timeout=180)
            self.match = match

        async def _finish_duel(self, interaction: discord.Interaction, loser_uid: int, *, timeout: bool = False):
            m = self.match
            await m.settle_duel(loser_uid)
            rematch_view = RrRematchView(m.guild_id, m.channel_id, m.participant_ids[0], m.participant_ids[1], m.bet_base)
            rematch_view.message = m.message
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                embed=m.build_embed(finished=True, loser_uid=loser_uid, timeout_loss=timeout),
                view=rematch_view,
            )
            self.stop()

        async def _finish_royale_hit(self, interaction: discord.Interaction, loser_uid: int):
            m = self.match
            m.history.append(f"💥 第 {m.trigger_index + 1} 發 — <@{loser_uid}> 中彈！")
            winner_uid = await m.eliminate_royale(loser_uid)
            for child in self.children:
                child.disabled = True
            if winner_uid:
                await interaction.response.edit_message(
                    embed=m.build_embed(finished=True, loser_uid=loser_uid, winner_uid=winner_uid),
                    view=None,
                )
                self.stop()
                return
            m.trigger_index += 1
            play_view = RrShootView(m)
            m.view = play_view
            await interaction.response.edit_message(embed=m.build_embed(interaction), view=play_view)
            await m.persist("playing")
            self.stop()

        async def _fire(self, interaction: discord.Interaction) -> None:
            m = self.match
            if m.settled:
                await interaction.response.send_message("對決已結束。", ephemeral=True)
                return
            if interaction.user.id != m.turn_uid:
                return await interaction.response.send_message(
                    f"現在輪到 <@{m.turn_uid}>。",
                    ephemeral=True,
                )
            m.ensure_chambers_for_royale()
            actor_uid = interaction.user.id
            if m.trigger_index in m.bullet_positions:
                if m.mode == "duel":
                    m.history.append(f"💥 第 {m.trigger_index + 1} 發 — <@{actor_uid}> 中彈！")
                    await self._finish_duel(interaction, actor_uid)
                    return
                await self._finish_royale_hit(interaction, actor_uid)
                return

            m.history.append(f"😮‍💨 第 {m.trigger_index + 1} 發 — <@{actor_uid}> 空膛")
            m.trigger_index += 1
            choice_view = RrChoiceView(m)
            m.view = choice_view
            await interaction.response.edit_message(
                embed=m.build_embed(interaction, awaiting_choice=True),
                view=choice_view,
            )
            await m.persist("choice")
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
                return await interaction.response.send_message(
                    f"請由 <@{m.turn_uid}> 決定。",
                    ephemeral=True,
                )
            shoot_view = RrShootView(m)
            m.view = shoot_view
            self.stop()
            await shoot_view._fire(interaction)

        @discord.ui.button(label="💰 加注繼續", style=discord.ButtonStyle.primary)
        async def double_down(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            if m.settled:
                return await interaction.response.send_message("對決已結束。", ephemeral=True)
            if interaction.user.id != m.turn_uid:
                return await interaction.response.send_message(
                    f"請由 <@{m.turn_uid}> 決定。",
                    ephemeral=True,
                )
            if not await try_deduct_balance_async(interaction.user.id, m.bet_base, "俄羅斯輪盤加注"):
                return await interaction.response.send_message(
                    f"❌ 餘額不足，加注需要 `{m.bet_base:,}` 東雲幣。",
                    ephemeral=True,
                )
            m.stakes[interaction.user.id] = m.stake_of(interaction.user.id) + m.bet_base
            m.history.append(f"💰 <@{interaction.user.id}> 加注 `{m.bet_base:,}` 並繼續")
            await m.persist("choice")
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
                return await interaction.response.send_message(
                    f"請由 <@{m.turn_uid}> 決定。",
                    ephemeral=True,
                )
            m.history.append(f"🛑 <@{interaction.user.id}> 停手換人")
            if m.mode == "duel":
                m.turn_uid = m.other_uid_duel(m.turn_uid)
            else:
                m.turn_uid = m.next_alive_uid(m.turn_uid)
            shoot_view = RrShootView(m)
            m.view = shoot_view
            await interaction.response.edit_message(embed=m.build_embed(interaction), view=shoot_view)
            await m.persist("playing")
            self.stop()

        async def on_timeout(self):
            await _finish_turn_timeout(self, reason="逾時未選擇（視同落敗）")

    def _validate_bet(amount: int) -> typing.Optional[str]:
        if amount < COINFLIP_MIN_BET or amount > COINFLIP_MAX_BET:
            return f"下注須介於 `{COINFLIP_MIN_BET:,}`～`{COINFLIP_MAX_BET:,}` 東雲幣。"
        return None

    async def _ensure_rr_open(interaction: discord.Interaction) -> bool:
        if not FEATURE_TOGGLES.get("russian_roulette", True) or not get_is_event_active():
            await interaction_send(interaction, "🚫 賭場目前休息中。", ephemeral=True)
            return False
        if not interaction.guild:
            await interaction_send(interaction, "請在伺服器頻道使用。", ephemeral=True)
            return False
        return True

    rr_group = app_commands.Group(name="russian_roulette", description="俄羅斯輪盤對決、淘汰賽與戰績")
    stats_group = app_commands.Group(name="stats", description="俄羅斯輪盤戰績與排行榜", parent=rr_group)

    @rr_group.command(name="duel", description="1v1 對決：空膛可繼續射、加注或換人；逾時判負")
    @app_commands.describe(member="對手", bet="雙方下注金額（相同）")
    async def russian_roulette_duel_slash(
        interaction: discord.Interaction,
        member: discord.Member,
        bet: app_commands.Range[int, 1, 10_000_000],
    ):
        if not await _ensure_rr_open(interaction):
            return
        if member.bot or member.id == interaction.user.id:
            return await interaction_send(interaction, "請指定其他玩家。", ephemeral=True)
        amount = int(bet)
        err = _validate_bet(amount)
        if err:
            return await interaction_send(interaction, err, ephemeral=True)
        busy = rr_match_repo.any_user_busy([interaction.user.id, member.id])
        if busy:
            return await interaction_send(interaction, _busy_msg(busy), ephemeral=True)
        await interaction_defer_if_needed(interaction)
        await ensure_user_exists_async(interaction.user.id, 50000)
        await ensure_user_exists_async(member.id, 50000)
        if not await try_deduct_balance_async(interaction.user.id, amount, "俄羅斯輪盤下注"):
            return await interaction_send(
                interaction,
                f"❌ 餘額不足，需要 `{amount:,}` 東雲幣才能發起對決。",
                ephemeral=True,
            )
        match_id = rr_match_repo.new_match_id()
        match = RrMatch(
            match_id=match_id,
            mode="duel",
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            bet_base=amount,
            participant_ids=[interaction.user.id, member.id],
            stakes={interaction.user.id: amount},
            challenger_id=interaction.user.id,
            opponent_id=member.id,
        )
        view = RrInviteView(match)
        sent = await interaction_send(
            interaction,
            content=(
                f"🔫 <@{interaction.user.id}> 向 <@{member.id}> 發起 **俄羅斯輪盤對決**！\n"
                f"注額：各 `{amount:,}` 東雲幣｜**{RUSSIAN_ROULETTE_CHAMBERS} 膛 1 彈**\n"
                f"規則：空膛可 **繼續射**、**加注繼續** 或 **停手換人**；**180 秒**未行動判負\n"
                f"<@{member.id}> 請 **120 秒** 內 **接受** 或 **選理由拒絕**。"
            ),
            embed=match.build_embed(interaction),
            view=view,
        )
        try:
            view.message = sent or await interaction.original_response()
            match.message = view.message
        except Exception:
            pass
        await match.persist("invite")

    @rr_group.command(
        name="royale",
        description=f"淘汰賽：{RR_ROYALE_MIN_PLAYERS}～{RR_ROYALE_MAX_PLAYERS} 人；3 人 1 彈，每多 1 人 +3 彈",
    )
    @app_commands.describe(bet="每位玩家入場費（相同）")
    async def russian_roulette_royale_slash(
        interaction: discord.Interaction,
        bet: app_commands.Range[int, 1, 10_000_000],
    ):
        if not await _ensure_rr_open(interaction):
            return
        amount = int(bet)
        err = _validate_bet(amount)
        if err:
            return await interaction_send(interaction, err, ephemeral=True)
        if rr_match_repo.user_active_match_id(interaction.user.id):
            return await interaction_send(interaction, _busy_msg(interaction.user.id), ephemeral=True)
        await interaction_defer_if_needed(interaction)
        await ensure_user_exists_async(interaction.user.id, 50000)
        if not await try_deduct_balance_async(interaction.user.id, amount, "俄羅斯輪盤淘汰賽入場"):
            return await interaction_send(
                interaction,
                f"❌ 餘額不足，需要 `{amount:,}` 東雲幣才能開局。",
                ephemeral=True,
            )
        match_id = rr_match_repo.new_match_id()
        match = RrMatch(
            match_id=match_id,
            mode="royale",
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            bet_base=amount,
            participant_ids=[interaction.user.id],
            stakes={interaction.user.id: amount},
            host_id=interaction.user.id,
        )
        view = RrRoyaleLobbyView(match)
        sent = await interaction_send(
            interaction,
            content=(
                f"👥 <@{interaction.user.id}> 開了 **俄羅斯輪盤淘汰賽**！\n"
                f"入場費各 `{amount:,}` 東雲幣｜**{RR_ROYALE_MIN_PLAYERS}～{RR_ROYALE_MAX_PLAYERS}** 人\n"
                f"子彈：**3 人 1 發**，每多 1 人 **+{RR_ROYALE_BULLETS_PER_EXTRA_PLAYER} 發**\n"
                f"按 **✅ 加入** 參賽，主持人湊滿人後按 **🚀 開始**。"
            ),
            embed=match.build_embed(interaction),
            view=view,
        )
        try:
            view.message = sent or await interaction.original_response()
            match.message = view.message
        except Exception:
            pass
        await match.persist("lobby")

    async def _render_personal_stats(target: discord.abc.User) -> discord.Embed:
        stats = await fetch_rr_stats_async(target.id)
        emb = discord.Embed(
            title=f"🔫 {target.display_name} 的俄羅斯輪盤戰績",
            color=0x5865F2,
        )
        if hasattr(target, "display_avatar"):
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
        emb.set_footer(text="僅統計已完成對局；邀請逾時／拒絕不計入")
        return emb

    @stats_group.command(name="me", description="查看個人俄羅斯輪盤戰績")
    @app_commands.describe(member="要查看的玩家（留空看自己）", user_id="或填使用者 ID／貼提及")
    async def rr_stats_me_slash(
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
        emb = await _render_personal_stats(target)
        await interaction_send(interaction, embed=emb)

    @stats_group.command(name="wins", description="俄羅斯輪盤勝場榜 Top 10")
    async def rr_stats_wins_slash(interaction: discord.Interaction):
        await interaction_defer_if_needed(interaction)
        board = await fetch_rr_leaderboard_async(10)
        emb = discord.Embed(title="🏆 俄羅斯輪盤勝場榜", color=0xFEE75C)
        if board:
            lines = [
                f"{i}. <@{row['user_id']}> — `{row['wins']}` 勝 / `{row['games']}` 局（`{row['win_rate']:.1f}%`）"
                for i, row in enumerate(board, start=1)
            ]
            emb.description = "\n".join(lines)
            emb.set_footer(text="至少 3 局才上榜")
        else:
            emb.description = "（尚無足夠戰績）"
        await interaction_send(interaction, embed=emb)

    @stats_group.command(name="rate", description="俄羅斯輪盤勝率榜 Top 10")
    async def rr_stats_rate_slash(interaction: discord.Interaction):
        await interaction_defer_if_needed(interaction)
        board = await fetch_rr_rate_leaderboard_async(10)
        emb = discord.Embed(title="📈 俄羅斯輪盤勝率榜", color=0x57F287)
        if board:
            lines = [
                f"{i}. <@{row['user_id']}> — `{row['win_rate']:.1f}%`（`{row['wins']}/{row['games']}`）"
                for i, row in enumerate(board, start=1)
            ]
            emb.description = "\n".join(lines)
            emb.set_footer(text="至少 3 局才上榜")
        else:
            emb.description = "（尚無足夠戰績）"
        await interaction_send(interaction, embed=emb)

    bot.tree.add_command(rr_group)

    async def _recover_one(record: typing.Dict[str, typing.Any]) -> None:
        match = RrMatch.from_record(record)
        guild = bot.get_guild(match.guild_id)
        if not guild:
            await match.refund_participants("俄羅斯輪盤恢復失敗退款", match.participant_ids)
            return
        channel = guild.get_channel(match.channel_id)
        if not isinstance(channel, discord.TextChannel):
            await match.refund_participants("俄羅斯輪盤恢復失敗退款", match.participant_ids)
            return
        message = None
        if record.get("message_id"):
            try:
                message = await channel.fetch_message(int(record["message_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            await match.refund_participants("俄羅斯輪盤恢復失敗退款", match.participant_ids)
            return
        match.message = message
        phase = str(record.get("phase") or "")
        if match.mode == "duel" and phase == "invite" and not match.invite_resolved:
            view: discord.ui.View = RrInviteView(match)
        elif match.mode == "royale" and phase == "lobby" and not match.lobby_started:
            view = RrRoyaleLobbyView(match)
        elif phase == "choice":
            view = RrChoiceView(match)
        else:
            view = RrShootView(match)
        match.view = view
        try:
            await message.edit(embed=match.build_embed(), view=view)
        except Exception:
            await match.refund_participants("俄羅斯輪盤恢復失敗退款", match.participant_ids)

    @bot.listen("on_ready")
    async def _recover_rr_matches_on_ready():
        if getattr(bot, "_rr_recovered", False):
            return
        bot._rr_recovered = True
        try:
            records = await fetch_active_rr_matches_async()
            for record in records:
                await _recover_one(record)
        except Exception:
            pass


__all__ = ["register_russian_roulette_commands"]
