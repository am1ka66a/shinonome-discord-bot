import asyncio
import logging
import random
import typing
from bot_modules.runtime.policy import allowed
from bot_modules.maintenance import active_game_action
from functools import wraps

import discord
from discord import app_commands

from bot_modules.games.roulette import (
    RouletteState, RR_ROYALE_MIN_PLAYERS, RR_ROYALE_MAX_PLAYERS,
    RR_ROYALE_BASE_PLAYERS, RR_ROYALE_CHAMBERS_PER_EXTRA_PLAYER,
)
from bot_modules.ui import roulette as roulette_ui
from bot_modules import rr_match_repo

DECLINE_REASONS = (
    "好了啦 你愛肛交",
    "不跟菜雞打",
    "老子沒錢",
    "這裡不是林口",
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
    original_debit = ctx["try_deduct_balance_async"]
    async def try_deduct_balance_async(uid, *args):
        if not await allowed(bot, uid):
            return False
        return await original_debit(uid, *args, require_casino_open=True)
    credit_balance_with_log_async = ctx["credit_balance_with_log_async"]
    fetch_rr_stats_async = ctx["fetch_rr_stats_async"]
    fetch_rr_leaderboard_async = ctx["fetch_rr_leaderboard_async"]
    fetch_rr_rate_leaderboard_async = ctx["fetch_rr_rate_leaderboard_async"]
    save_rr_match_async = ctx["save_rr_match_async"]
    delete_rr_match_async = ctx["delete_rr_match_async"]
    fetch_active_rr_matches_async = ctx["fetch_active_rr_matches_async"]
    resolve_slash_target = ctx["resolve_slash_target"]

    def match_action(callback):
        @wraps(callback)
        async def guarded(self, *args, **kwargs):
            m = self.match
            interaction = args[0] if args else None
            if interaction is not None:
                await interaction_defer_if_needed(interaction, thinking=False)
            with active_game_action(bot):
                return await locked_action(self, interaction, m, callback, args, kwargs)
        return guarded

    async def locked_action(self, interaction, m, callback, args, kwargs):
        async with m.lock:
            # Ignore queued callbacks from an earlier phase of the match.
            view = self.view if isinstance(self, discord.ui.Select) else self
            if m.view is not None and m.view is not view:
                if interaction is not None:
                    await interaction_send(interaction, "畫面已更新，請使用目前的按鈕。", ephemeral=True)
                return
            return await callback(self, *args, **kwargs)


    def _busy_msg(uid: int) -> str:
        return f"<@{uid}> 已有進行中的俄羅斯輪盤，請先完成或等待該場結束。"

    class RrMatch(RouletteState):
        default_chambers = RUSSIAN_ROULETTE_CHAMBERS

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.message = None
            self.view = None
            self.lock = asyncio.Lock()

        def build_embed(self, interaction=None, **kwargs):
            return roulette_ui.build_embed(self, interaction, **kwargs)

        def build_history_embed(self):
            return roulette_ui.build_history_embed(self)

        async def persist(self, phase: str) -> None:
            # 結算後才落盤會留下幽靈對局，把玩家永久卡在「進行中」
            if self.settled:
                return
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


        async def payout_duel(self, loser_uid: int, winner_uid: int) -> None:
            pot = self.pot
            win_stake = self.stake_of(winner_uid)
            lose_stake = self.stake_of(loser_uid)
            await asyncio.to_thread(rr_match_repo.settle_event_sync,
                match_id=self.match_id, event_key="duel", credits={winner_uid: pot},
                results={winner_uid: (True, pot - win_stake), loser_uid: (False, -lose_stake)},
                reason="俄羅斯輪盤勝利", payload=self.to_payload(), finished=True)


        async def settle_duel(self, loser_uid: int) -> int:
            winner_uid = self.other_uid_duel(loser_uid)
            if self.mark_settled():
                await self.payout_duel(loser_uid, winner_uid)
            return winner_uid


        async def payout_royale(self, loser_uid: int, winner_uid: typing.Optional[int]) -> None:
            lose_stake = self.stake_of(loser_uid)
            results = {loser_uid: (False, -lose_stake)}
            credits = {}
            if winner_uid is not None:
                credits[winner_uid] = self.pot
                results[winner_uid] = (True, self.pot - self.stake_of(winner_uid))
            await asyncio.to_thread(rr_match_repo.settle_event_sync,
                match_id=self.match_id, event_key=f"eliminate:{loser_uid}", credits=credits,
                results=results, reason="俄羅斯輪盤淘汰賽勝利", payload=self.to_payload(),
                finished=winner_uid is not None)


        async def eliminate_royale(self, uid: int, *, timeout: bool = False) -> typing.Optional[int]:
            winner_uid = self.apply_royale_elimination(uid)
            await self.payout_royale(int(uid), winner_uid)
            return winner_uid


        async def refund_participants(self, reason: str, uids: typing.Iterable[int]) -> None:
            if self.settled:
                return
            self.settled = True
            credits = {uid: self.stake_of(uid) for uid in uids if self.stake_of(uid) > 0}
            await asyncio.to_thread(rr_match_repo.settle_event_sync,
                match_id=self.match_id, event_key="refund", credits=credits, results={},
                reason=reason, payload=self.to_payload(), finished=True)


    async def _finish_turn_timeout(view: discord.ui.View, *, reason: str) -> None:
        m: RrMatch = view.match  # type: ignore[attr-defined]
        if m.settled:
            return
        actor_uid = int(m.turn_uid or 0)
        m.history.append(f"⌛ <@{actor_uid}> {reason}")
        if m.mode == "duel":
            await m.settle_duel(actor_uid)
            rematch_view = RrRematchView(
                m.guild_id, m.channel_id, m.participant_ids[0], m.participant_ids[1], m.bet_base, match=m
            )
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
                            view=RrHistoryView(m),
                        )
                    else:
                        play_view = RrShootView(m)
                        m.view = play_view
                        await m.message.edit(embed=m.build_embed(), view=play_view)
                        await m.persist("playing")
            except Exception:
                pass
        view.stop()

    class RrHistoryButton(discord.ui.Button):
        """任何人都能點，機器人以私人訊息把完整歷程送給點的人。"""

        def __init__(self, match: "RrMatch", *, row: typing.Optional[int] = None):
            super().__init__(label="📜 歷程", style=discord.ButtonStyle.secondary, row=row)
            self.match = match

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_message(
                embed=self.match.build_history_embed(),
                ephemeral=True,
            )

    class RrHistoryView(discord.ui.View):
        """對局結束後留在訊息上的歷程按鈕。"""

        def __init__(self, match: "RrMatch"):
            super().__init__(timeout=1800)
            self.match = match
            self.add_item(RrHistoryButton(match))

        async def on_timeout(self):
            for child in self.children:
                child.disabled = True
            try:
                if self.match.message:
                    await self.match.message.edit(view=self)
            except Exception:
                pass

    class DeclineReasonSelect(discord.ui.Select):
        def __init__(self, match: RrMatch):
            self.match = match
            options = [
                discord.SelectOption(label=reason, value=reason)
                for reason in DECLINE_REASONS
            ]
            super().__init__(
                placeholder="拒絕並選理由…",
                options=options,
                min_values=1,
                max_values=1,
            )

        @match_action
        async def callback(self, interaction: discord.Interaction):
            m = self.match
            view: RrInviteView = self.view  # type: ignore[assignment]
            if interaction.user.id != m.opponent_id:
                return await interaction_send(interaction, "這場對決不是邀請你的。", ephemeral=True)
            if view.resolved or m.settled:
                return await interaction_defer_if_needed(interaction, thinking=False)
            view.resolved = True
            reason = self.values[0]
            await m.refund_participants("俄羅斯輪盤取消退款", [m.challenger_id])
            for child in view.children:
                child.disabled = True
            await interaction.edit_original_response(
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
        def __init__(
            self,
            guild_id: int,
            channel_id: int,
            uid_a: int,
            uid_b: int,
            bet: int,
            *,
            match: typing.Optional["RrMatch"] = None,
        ):
            super().__init__(timeout=120)
            self.guild_id = guild_id
            self.channel_id = channel_id
            self.uid_a = uid_a
            self.uid_b = uid_b
            self.bet = int(bet)
            self.accepted: typing.Set[int] = set()
            self.message: typing.Optional[discord.Message] = None
            self.finished_match = match
            self._callback_lock = asyncio.Lock()
            self.started = False
            if match is not None:
                self.add_item(RrHistoryButton(match))

        async def _try_start(self, interaction: discord.Interaction) -> None:
            if self.started:
                return await interaction_send(interaction, "重賽已經開始。", ephemeral=True)
            if self.accepted != {self.uid_a, self.uid_b}:
                await interaction_send(interaction,
                    f"重賽確認：`{len(self.accepted)}/2`（需雙方都按 🔁 重賽）",
                    ephemeral=True,
                )
                return
            busy = rr_match_repo.any_user_busy([self.uid_a, self.uid_b])
            if busy:
                return await interaction_send(interaction, _busy_msg(busy), ephemeral=True)
            guild = interaction.guild or bot.get_guild(self.guild_id)
            if not guild:
                return await interaction_send(interaction, "無法取得伺服器。", ephemeral=True)
            if not await try_deduct_balance_async(self.uid_a, self.bet, "俄羅斯輪盤重賽下注"):
                return await interaction_send(interaction, "發起方餘額不足，重賽取消。", ephemeral=True)
            if not await try_deduct_balance_async(self.uid_b, self.bet, "俄羅斯輪盤重賽下注"):
                await credit_balance_with_log_async(self.uid_a, self.bet, "俄羅斯輪盤重賽退款")
                return await interaction_send(interaction, "對手餘額不足，重賽取消。", ephemeral=True)
            self.started = True
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
            await interaction.edit_original_response(
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
            await interaction_defer_if_needed(interaction, thinking=False)
            async with self._callback_lock:
                self.accepted.add(interaction.user.id)
                await self._try_start(interaction)

        async def on_timeout(self):
            # 重賽逾時後只留下歷程按鈕，讓大家還能回顧這場
            if self.finished_match is not None:
                replacement: discord.ui.View = RrHistoryView(self.finished_match)
            else:
                for child in self.children:
                    child.disabled = True
                replacement = self
            try:
                if self.message:
                    await self.message.edit(view=replacement)
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
        @match_action
        async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            if interaction.user.id != m.opponent_id:
                return await interaction_send(interaction, "這場對決不是邀請你的。", ephemeral=True)
            if self.resolved or m.settled:
                return await interaction_defer_if_needed(interaction, thinking=False)
            if not await try_deduct_balance_async(m.opponent_id, m.bet_base, "俄羅斯輪盤下注"):
                return await interaction_send(interaction,
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
            await interaction.edit_original_response(
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

        @discord.ui.button(label="↩️ 撤回", style=discord.ButtonStyle.secondary)
        @match_action
        async def withdraw(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            if interaction.user.id != m.challenger_id:
                return await interaction_send(interaction,
                    "只有發起人可以撤回這場邀請。",
                    ephemeral=True,
                )
            if self.resolved or m.settled:
                return await interaction_defer_if_needed(interaction, thinking=False)
            self.resolved = True
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(
                content=(
                    f"↩️ <@{m.challenger_id}> 撤回了對 <@{m.opponent_id}> 的對決邀請，"
                    f"已退回 `{m.bet_base:,}` 東雲幣。"
                ),
                embed=None,
                view=self,
            )
            await m.refund_participants("俄羅斯輪盤邀請撤回退款", [m.challenger_id])
            self.stop()

        @match_action
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
            async with m.lock:
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

        @discord.ui.button(label="🚪 退賽", style=discord.ButtonStyle.secondary)
        async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            uid = interaction.user.id
            async with m.lock:
                if m.lobby_started or m.settled:
                    return await interaction.response.send_message(
                        "這場已經開打了，無法退賽。",
                        ephemeral=True,
                    )
                if uid == m.host_id:
                    return await interaction.response.send_message(
                        "你是主持人，請改用 **🧹 解散** 解除組隊（會退款給所有人）。",
                        ephemeral=True,
                    )
                if uid not in m.participant_ids:
                    return await interaction.response.send_message("你不在這場的名單裡。", ephemeral=True)
                refund = m.remove_participant(uid)
                await interaction.response.edit_message(embed=m.build_embed(interaction), view=self)
                try:
                    m.message = await interaction.original_response()
                except Exception:
                    pass
                if refund > 0:
                    await credit_balance_with_log_async(uid, refund, "俄羅斯輪盤淘汰賽退賽退款")
                await m.persist("lobby")
            await interaction.followup.send(
                f"🚪 已退出這場淘汰賽，退回 `{refund:,}` 東雲幣。",
                ephemeral=True,
            )

        @discord.ui.button(label="🧹 解散", style=discord.ButtonStyle.secondary)
        async def disband(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            async with m.lock:
                if m.lobby_started or m.settled:
                    return await interaction.response.send_message("這場已開始或結束。", ephemeral=True)
                if interaction.user.id != m.host_id:
                    return await interaction.response.send_message(
                        "只有主持人可以解散這場淘汰賽。",
                        ephemeral=True,
                    )
                refunded = list(m.participant_ids)
                total = m.pot
                for child in self.children:
                    child.disabled = True
                await interaction.response.edit_message(
                    content=(
                        f"🧹 <@{m.host_id}> 解散了淘汰賽，"
                        f"已退回共 `{total:,}` 東雲幣給 {len(refunded)} 位玩家。"
                    ),
                    embed=None,
                    view=self,
                )
                await m.refund_participants("俄羅斯輪盤淘汰賽解散退款", refunded)
            self.stop()

        @discord.ui.button(label="🚀 開始", style=discord.ButtonStyle.danger)
        async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            async with m.lock:
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
            async with m.lock:
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
            self.add_item(RrHistoryButton(match))

        async def _finish_duel(self, interaction: discord.Interaction, loser_uid: int, *, timeout: bool = False):
            m = self.match
            winner_uid = m.other_uid_duel(loser_uid)
            owns_payout = m.mark_settled()
            if owns_payout:
                await m.payout_duel(loser_uid, winner_uid)
            rematch_view = RrRematchView(
                m.guild_id, m.channel_id, m.participant_ids[0], m.participant_ids[1], m.bet_base, match=m
            )
            rematch_view.message = m.message
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(
                embed=m.build_embed(finished=True, loser_uid=loser_uid, timeout_loss=timeout),
                view=rematch_view,
            )
            self.stop()

        async def _finish_royale_hit(self, interaction: discord.Interaction, loser_uid: int):
            m = self.match
            m.history.append(f"💥 第 {m.trigger_index + 1} 發 — <@{loser_uid}> 中彈！")
            winner_uid = m.apply_royale_elimination(loser_uid)
            for child in self.children:
                child.disabled = True
            if winner_uid:
                await m.payout_royale(loser_uid, winner_uid)
                await interaction.edit_original_response(
                    embed=m.build_embed(finished=True, loser_uid=loser_uid, winner_uid=winner_uid),
                    view=RrHistoryView(m),
                )
                self.stop()
                return
            m.trigger_index += 1
            await m.payout_royale(loser_uid, None)
            play_view = RrShootView(m)
            m.view = play_view
            await interaction.edit_original_response(embed=m.build_embed(interaction), view=play_view)
            self.stop()

        async def _fire(self, interaction: discord.Interaction) -> None:
            m = self.match
            if m.settled:
                await interaction_send(interaction, "對決已結束。", ephemeral=True)
                return
            if interaction.user.id != m.turn_uid:
                return await interaction_send(interaction,
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
            await interaction.edit_original_response(
                embed=m.build_embed(interaction, awaiting_choice=True),
                view=choice_view,
            )
            await m.persist("choice")
            self.stop()

        @discord.ui.button(label="🔫 扣扳機", style=discord.ButtonStyle.danger)
        @match_action
        async def pull_trigger(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._fire(interaction)

        @match_action
        async def on_timeout(self):
            await _finish_turn_timeout(self, reason="逾時未扣扳機")

    class RrChoiceView(discord.ui.View):
        def __init__(self, match: RrMatch):
            super().__init__(timeout=180)
            self.match = match
            self.add_item(RrHistoryButton(match))

        @discord.ui.button(label="🔫 繼續射", style=discord.ButtonStyle.danger)
        @match_action
        async def continue_shoot(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            if m.settled:
                return await interaction_send(interaction, "對決已結束。", ephemeral=True)
            if interaction.user.id != m.turn_uid:
                return await interaction_send(interaction,
                    f"請由 <@{m.turn_uid}> 決定。",
                    ephemeral=True,
                )
            shoot_view = RrShootView(m)
            m.view = shoot_view
            self.stop()
            await shoot_view._fire(interaction)

        @discord.ui.button(label="💰 加注繼續", style=discord.ButtonStyle.primary)
        @match_action
        async def double_down(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            if m.settled:
                return await interaction_send(interaction, "對決已結束。", ephemeral=True)
            if interaction.user.id != m.turn_uid:
                return await interaction_send(interaction,
                    f"請由 <@{m.turn_uid}> 決定。",
                    ephemeral=True,
                )
            if not await try_deduct_balance_async(interaction.user.id, m.bet_base, "俄羅斯輪盤加注"):
                return await interaction_send(interaction,
                    f"❌ 餘額不足，加注需要 `{m.bet_base:,}` 東雲幣。",
                    ephemeral=True,
                )
            m.stakes[interaction.user.id] = m.stake_of(interaction.user.id) + m.bet_base
            m.history.append(f"💰 <@{interaction.user.id}> 加注 `{m.bet_base:,}` 並繼續")
            # 不在這裡落盤：_fire() 回覆玩家之後就會存檔，先存只是多一次 DB 往返拖慢按鈕
            shoot_view = RrShootView(m)
            m.view = shoot_view
            self.stop()
            await shoot_view._fire(interaction)

        @discord.ui.button(label="🛑 停手換人", style=discord.ButtonStyle.secondary)
        @match_action
        async def pass_turn(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            if m.settled:
                return await interaction_send(interaction, "對決已結束。", ephemeral=True)
            if interaction.user.id != m.turn_uid:
                return await interaction_send(interaction,
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
            await interaction.edit_original_response(embed=m.build_embed(interaction), view=shoot_view)
            await m.persist("playing")
            self.stop()

        @match_action
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
                f"<@{member.id}> 請 **120 秒** 內 **接受** 或 **選理由拒絕**；"
                f"發起人可按 **↩️ 撤回** 收回邀請。"
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
        description=(
            f"淘汰賽：{RR_ROYALE_MIN_PLAYERS}～{RR_ROYALE_MAX_PLAYERS} 人；"
            f"子彈＝人數 − 1，{RR_ROYALE_BASE_PLAYERS} 人 {RUSSIAN_ROULETTE_CHAMBERS} 膛、每多 1 人 +{RR_ROYALE_CHAMBERS_PER_EXTRA_PLAYER} 膛"
        ),
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
                f"子彈：**人數 − 1 發**｜彈膛：**{RR_ROYALE_BASE_PLAYERS} 人 {RUSSIAN_ROULETTE_CHAMBERS} 膛**，"
                f"每多 1 人 **+{RR_ROYALE_CHAMBERS_PER_EXTRA_PLAYER} 膛**\n"
                f"按 **✅ 加入** 參賽、**🚪 退賽** 可在開打前退出並退款；\n"
                f"主持人湊滿人後按 **🚀 開始**，或按 **🧹 解散** 解除組隊。"
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

    recovery_lock = asyncio.Lock()
    recovered_matches = set()

    @bot.listen("on_ready")
    async def _recover_rr_matches_on_ready():
        async with recovery_lock:
            if getattr(bot, "_rr_recovered", False):
                return
            ready = getattr(bot, "_db_ready_event", None)
            if ready is not None:
                await ready.wait()
            try:
                records = await fetch_active_rr_matches_async()
                for record in records:
                    if record['match_id'] not in recovered_matches:
                        await _recover_one(record)
                        recovered_matches.add(record['match_id'])
                bot._rr_recovered = True
            except Exception:
                logging.getLogger("shinonome_bot").exception("輪盤恢復失敗，保留存檔供下次連線重試")


__all__ = ["register_russian_roulette_commands"]
