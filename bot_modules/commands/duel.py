import random
import typing

import discord
from discord import app_commands


def register_duel_commands(bot, ctx: typing.Dict[str, typing.Any]) -> None:
    interaction_send = ctx["interaction_send"]
    interaction_defer_if_needed = ctx["interaction_defer_if_needed"]
    ensure_user_exists_async = ctx["ensure_user_exists_async"]
    try_deduct_balance_async = ctx["try_deduct_balance_async"]
    credit_balance_with_log_async = ctx["credit_balance_with_log_async"]
    settle_duel_payouts_with_log_async = ctx["settle_duel_payouts_with_log_async"]
    feature_toggles = ctx["FEATURE_TOGGLES"]

    duel_cards = {
        "king": ("👑", "王"),
        "citizen": ("🧑", "民"),
        "slave": ("🗡️", "奴"),
    }

    def duel_resolve_round(em_pick: str, sl_pick: str) -> str:
        if em_pick == "citizen" and sl_pick == "citizen":
            return "draw"
        if em_pick == "king" and sl_pick == "slave":
            return "slave"
        return "emperor"

    class EDuelInviteView(discord.ui.View):
        def __init__(self, challenger: discord.Member, opponent: discord.Member, bet: int):
            super().__init__(timeout=120)
            self.challenger = challenger
            self.opponent = opponent
            self.bet = int(bet)
            self.message: typing.Optional[discord.Message] = None
            self.resolved = False

        async def _refund_challenger(self):
            await credit_balance_with_log_async(self.challenger.id, self.bet, "E卡決鬥取消退款")

        @discord.ui.button(label="接受", style=discord.ButtonStyle.success)
        async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != self.opponent.id:
                return await interaction.response.send_message("這場決鬥不是邀請你的。", ephemeral=True)
            if self.resolved:
                return await interaction.response.defer()
            if not await try_deduct_balance_async(self.opponent.id, self.bet, "E卡決鬥下注"):
                return await interaction.response.send_message(
                    f"❌ 你的餘額不足，需要 `{self.bet:,}` 東雲幣才能接受此決鬥。",
                    ephemeral=True,
                )
            self.resolved = True
            for child in self.children:
                child.disabled = True
            match = EDuelMatch(self.challenger, self.opponent, self.bet)
            play = EDuelPlayView(match)
            match.view = play
            match.channel = interaction.channel
            await interaction.response.edit_message(
                content=None,
                embed=match.build_selection_embed(),
                view=play,
            )
            try:
                match.message = await interaction.original_response()
            except Exception:
                pass
            self.stop()

        @discord.ui.button(label="拒絕", style=discord.ButtonStyle.danger)
        async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != self.opponent.id:
                return await interaction.response.send_message("這場決鬥不是邀請你的。", ephemeral=True)
            if self.resolved:
                return await interaction.response.defer()
            self.resolved = True
            await self._refund_challenger()
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=(
                    f"❌ {self.opponent.mention} 拒絕了 {self.challenger.mention} 的 E 卡決鬥。"
                    f"已退回挑戰者注金 `{self.bet:,}`。"
                ),
                view=self,
            )
            self.stop()

        async def on_timeout(self):
            if self.resolved:
                return
            self.resolved = True
            await self._refund_challenger()
            for child in self.children:
                child.disabled = True
            try:
                if self.message:
                    await self.message.edit(
                        content=(
                            f"⌛ {self.opponent.mention} 未在時限內回應 {self.challenger.mention} 的 E 卡決鬥邀請，"
                            f"挑戰者注金 `{self.bet:,}` 已退回。"
                        ),
                        view=self,
                    )
            except Exception:
                pass

    class DuelCardButton(discord.ui.Button):
        def __init__(self, key: str, match: "EDuelMatch", picker_id: int, count: int):
            emoji, label = duel_cards[key]
            super().__init__(
                style=discord.ButtonStyle.primary,
                label=f"{label} ×{count}",
                emoji=emoji,
            )
            self.key = key
            self.match = match
            self.picker_id = picker_id

        async def callback(self, interaction: discord.Interaction):
            await self.match.record_pick(interaction, self.picker_id, self.key)

    class EDuelPickerView(discord.ui.View):
        def __init__(self, match: "EDuelMatch", picker_id: int):
            super().__init__(timeout=120)
            hand = match.hands.get(picker_id, {})
            for key in ("king", "citizen", "slave"):
                cnt = int(hand.get(key, 0) or 0)
                if cnt <= 0:
                    continue
                self.add_item(DuelCardButton(key, match, picker_id, cnt))

    class EDuelMatch:
        def __init__(self, challenger: discord.Member, opponent: discord.Member, bet: int):
            self.challenger = challenger
            self.opponent = opponent
            self.bet = int(bet)
            first_emperor = random.choice([challenger.id, opponent.id])
            first_slave = opponent.id if first_emperor == challenger.id else challenger.id
            self.game_roles: typing.List[typing.Dict[int, str]] = [
                {first_emperor: "emperor", first_slave: "slave"},
                {first_emperor: "slave", first_slave: "emperor"},
            ]
            self.game_no = 1
            self.round_in_game = 1
            self.scores: typing.Dict[int, int] = {challenger.id: 0, opponent.id: 0}
            self.round_history: typing.List[typing.Dict[str, typing.Any]] = []
            self.last_round: typing.Optional[typing.Dict[str, typing.Any]] = None
            self.picks: typing.Dict[int, str] = {}
            self.hands: typing.Dict[int, typing.Dict[str, int]] = self._make_hands(0)
            self.channel: typing.Optional[discord.abc.Messageable] = None
            self.view: typing.Optional["EDuelPlayView"] = None
            self.message: typing.Optional[discord.Message] = None
            self.settled = False

        def _make_hands(self, game_idx: int) -> typing.Dict[int, typing.Dict[str, int]]:
            roles = self.game_roles[game_idx]
            return {
                uid: ({"king": 1, "citizen": 4} if r == "emperor" else {"slave": 1, "citizen": 4})
                for uid, r in roles.items()
            }

        def _role_of(self, uid: int, game_no: typing.Optional[int] = None) -> str:
            gn = game_no or self.game_no
            return self.game_roles[gn - 1].get(uid, "emperor")

        def _uid_for_role(self, role: str, game_no: typing.Optional[int] = None) -> int:
            gn = game_no or self.game_no
            for uid, r in self.game_roles[gn - 1].items():
                if r == role:
                    return uid
            return 0

        def _member_for_uid(self, uid: int) -> discord.Member:
            return self.challenger if self.challenger.id == uid else self.opponent

        def _format_hand(self, uid: int) -> str:
            h = self.hands.get(uid, {})
            role = self._role_of(uid)
            if role == "emperor":
                return f"👑 ×{int(h.get('king', 0) or 0)}　🧑 ×{int(h.get('citizen', 0) or 0)}"
            return f"🗡️ ×{int(h.get('slave', 0) or 0)}　🧑 ×{int(h.get('citizen', 0) or 0)}"

        def _role_label(self, role: str) -> str:
            return "👑 國王方" if role == "emperor" else "🗡️ 奴隸方"

        def _player_status_value(self, uid: int) -> str:
            return "✅ 已選牌" if uid in self.picks else "⏳ 等待選牌中…"

        def _last_round_outcome_text(self) -> str:
            entry = self.last_round
            if not entry:
                return ""
            em_member = self._member_for_uid(entry["em_uid"])
            sl_member = self._member_for_uid(entry["sl_uid"])
            em_e, em_l = duel_cards[entry["em_pick"]]
            sl_e, sl_l = duel_cards[entry["sl_pick"]]
            if entry["result"] == "draw":
                tag = "🤝 平手（民 vs 民），雙方消耗該牌"
            elif entry["result"] == "emperor":
                tag = f"🏆 {em_member.display_name}（👑 國王方）+1"
            else:
                tag = f"🏆 {sl_member.display_name}（🗡️ 奴隸方）+3"
            return (
                f"第 {entry['game']} 大局・第 {entry['round']} 小局　"
                f"👑 {em_e} {em_l}　vs　🗡️ {sl_e} {sl_l}　— {tag}"
            )

        def build_selection_embed(self) -> discord.Embed:
            em_uid = self._uid_for_role("emperor")
            sl_uid = self._uid_for_role("slave")
            em_member = self._member_for_uid(em_uid)
            sl_member = self._member_for_uid(sl_uid)
            emb = discord.Embed(
                title=f"⚔️ 第 {self.game_no} 大局・第 {self.round_in_game} 小局｜選牌中",
                description=(
                    f"{self.challenger.mention}　**VS**　{self.opponent.mention}\n"
                    f"注額：`{self.bet:,}`　｜　彩池：`{self.bet * 2:,}` 東雲幣\n"
                    f"本大局陣營：👑 {em_member.mention}　vs　🗡️ {sl_member.mention}\n"
                    "勝：👑 > 🧑、🧑 > 🗡️、🗡️ > 👑（民vs民平手繼續）\n"
                    "計分：奴隸贏王 +3｜其餘決勝 +1"
                ),
                color=0x5865F2,
            )
            if self.last_round:
                emb.add_field(name="上一小局結果", value=self._last_round_outcome_text(), inline=False)
            emb.add_field(
                name=f"{em_member.display_name}　👑 國王方｜剩餘",
                value=self._format_hand(em_uid),
                inline=True,
            )
            emb.add_field(name="\u200b", value="⚔️", inline=True)
            emb.add_field(
                name=f"{sl_member.display_name}　🗡️ 奴隸方｜剩餘",
                value=self._format_hand(sl_uid),
                inline=True,
            )
            emb.add_field(
                name="目前積分",
                value=(
                    f"{self.challenger.display_name}: `{self.scores[self.challenger.id]}`　｜　"
                    f"{self.opponent.display_name}: `{self.scores[self.opponent.id]}`"
                ),
                inline=False,
            )
            emb.add_field(
                name=self.challenger.display_name,
                value=self._player_status_value(self.challenger.id),
                inline=True,
            )
            emb.add_field(name="\u200b", value="\u200b", inline=True)
            emb.add_field(
                name=self.opponent.display_name,
                value=self._player_status_value(self.opponent.id),
                inline=True,
            )
            emb.set_footer(text="第二大局會交換陣營｜長時間未動作將退款")
            return emb

        def build_picker_embed(self, picker_id: int) -> discord.Embed:
            h = self.hands.get(picker_id, {})
            role = self._role_of(picker_id)
            order = ("king", "citizen") if role == "emperor" else ("slave", "citizen")
            parts = []
            for key in order:
                e, l = duel_cards[key]
                cnt = int(h.get(key, 0) or 0)
                if cnt > 0:
                    parts.append(f"{e} {l} ×{cnt}")
            hand_text = "　".join(parts) if parts else "（已無牌）"
            emb = discord.Embed(
                title=f"🎴 第 {self.game_no} 大局・第 {self.round_in_game} 小局｜選牌",
                description=(
                    f"你本大局：{self._role_label(role)}\n"
                    f"你的剩餘持牌：{hand_text}\n"
                    "勝：👑 > 🧑、🧑 > 🗡️、🗡️ > 👑"
                ),
                color=0x5865F2,
            )
            emb.set_footer(text="送出後不可更改｜長時間未動作將退款")
            return emb

        async def _refresh_message(self) -> None:
            if not self.message or not self.view:
                return
            try:
                await self.message.edit(embed=self.build_selection_embed(), view=self.view)
            except Exception:
                pass

        def _format_history(self) -> str:
            if not self.round_history:
                return "（尚無紀錄）"
            lines = []
            last_game = 0
            for entry in self.round_history:
                if entry["game"] != last_game:
                    last_game = entry["game"]
                    lines.append(f"**第 {last_game} 大局**")
                em_e, _ = duel_cards[entry["em_pick"]]
                sl_e, _ = duel_cards[entry["sl_pick"]]
                em_member = self._member_for_uid(entry["em_uid"])
                sl_member = self._member_for_uid(entry["sl_uid"])
                res = entry["result"]
                if res == "draw":
                    tag = "🤝 平手"
                elif res == "emperor":
                    tag = f"🏆 {em_member.display_name} +1"
                else:
                    tag = f"🏆 {sl_member.display_name} +3"
                lines.append(f"　第 {entry['round']} 小局：👑 {em_e}　vs　🗡️ {sl_e}　— {tag}")
            return "\n".join(lines)

        async def _refund_both(self, reason: str) -> None:
            await credit_balance_with_log_async(self.challenger.id, self.bet, reason)
            await credit_balance_with_log_async(self.opponent.id, self.bet, reason)

        async def _settle_match(self) -> typing.Dict[str, int]:
            pot = self.bet * 2
            s_a = self.scores[self.challenger.id]
            s_b = self.scores[self.opponent.id]
            total = s_a + s_b
            if total <= 0:
                a_amt = self.bet
                b_amt = pot - a_amt
            else:
                a_amt = pot * s_a // total
                b_amt = pot - a_amt
            await settle_duel_payouts_with_log_async(
                self.challenger.id,
                self.opponent.id,
                a_amt,
                b_amt,
                s_a,
                s_b,
            )
            return {"a": a_amt, "b": b_amt, "s_a": s_a, "s_b": s_b}

        def _build_final_embed(self, payouts: typing.Dict[str, int]) -> discord.Embed:
            s_a = payouts["s_a"]
            s_b = payouts["s_b"]
            a_amt = payouts["a"]
            b_amt = payouts["b"]
            if s_a == s_b:
                outcome = f"🤝 兩大局後積分平手 **{s_a} : {s_b}**，平分彩池。"
                color = 0xFFD166
            elif s_a > s_b:
                outcome = f"🏆 {self.challenger.mention} 以 **{s_a} : {s_b}** 勝出！"
                color = 0x57F287
            else:
                outcome = f"🏆 {self.opponent.mention} 以 **{s_b} : {s_a}** 勝出！"
                color = 0x57F287
            emb = discord.Embed(title="⚔️ E 卡決鬥｜整場結算", description=outcome, color=color)
            emb.add_field(
                name="積分",
                value=(
                    f"{self.challenger.display_name}: `{s_a}`　｜　"
                    f"{self.opponent.display_name}: `{s_b}`"
                ),
                inline=False,
            )
            emb.add_field(
                name="彩池分配",
                value=(
                    f"{self.challenger.display_name}: `{a_amt:,}`　｜　"
                    f"{self.opponent.display_name}: `{b_amt:,}`"
                ),
                inline=False,
            )
            emb.add_field(name="對戰過程", value=self._format_history()[:1024], inline=False)
            emb.set_footer(text="計分：奴贏王 +3｜其餘決勝 +1｜兩大局制（第二大局交換陣營）")
            return emb

        async def record_pick(self, interaction: discord.Interaction, picker_id: int, key: str):
            if interaction.user.id != picker_id:
                return await interaction.response.send_message("這個按鈕不是給你的。", ephemeral=True)
            if self.settled:
                return await interaction.response.send_message("此場決鬥已結束。", ephemeral=True)
            if picker_id in self.picks:
                return await interaction.response.send_message("你本小局已經選過了。", ephemeral=True)
            if int(self.hands.get(picker_id, {}).get(key, 0) or 0) <= 0:
                return await interaction.response.send_message("你已沒有這張牌。", ephemeral=True)
            self.picks[picker_id] = key
            emoji, label = duel_cards[key]
            confirm_emb = discord.Embed(
                title=f"✅ 已送出（第 {self.game_no} 大局・第 {self.round_in_game} 小局）",
                description=f"你出的是：{emoji} **{label}**\n等待對手出牌與翻牌結算…",
                color=0x57F287,
            )
            await interaction.response.edit_message(content=None, embed=confirm_emb, view=None)
            if not self.settled and len(self.picks) < 2:
                await self._refresh_message()
            if len(self.picks) == 2 and not self.settled:
                await self._process_round()

        async def _process_round(self):
            em_uid = self._uid_for_role("emperor")
            sl_uid = self._uid_for_role("slave")
            em_pick = self.picks[em_uid]
            sl_pick = self.picks[sl_uid]
            em_hand = self.hands[em_uid]
            sl_hand = self.hands[sl_uid]
            em_hand[em_pick] = max(0, int(em_hand.get(em_pick, 0)) - 1)
            sl_hand[sl_pick] = max(0, int(sl_hand.get(sl_pick, 0)) - 1)
            result = duel_resolve_round(em_pick, sl_pick)
            if result == "emperor":
                self.scores[em_uid] += 1
            elif result == "slave":
                self.scores[sl_uid] += 3
            entry = {
                "game": self.game_no,
                "round": self.round_in_game,
                "em_uid": em_uid,
                "sl_uid": sl_uid,
                "em_pick": em_pick,
                "sl_pick": sl_pick,
                "result": result,
            }
            self.round_history.append(entry)
            self.last_round = entry

            if result == "draw":
                self.picks.clear()
                self.round_in_game += 1
                await self._refresh_message()
                return

            if self.game_no < 2:
                self.game_no = 2
                self.round_in_game = 1
                self.hands = self._make_hands(1)
                self.picks.clear()
                await self._refresh_message()
                return

            self.settled = True
            payouts = await self._settle_match()
            if self.message and self.view:
                for child in self.view.children:
                    child.disabled = True
                self.view.stop()
                try:
                    await self.message.edit(embed=self._build_final_embed(payouts), view=None)
                except Exception:
                    pass

        async def handle_timeout(self, view: "EDuelPlayView") -> None:
            if self.settled or self.view is not view:
                return
            self.settled = True
            await self._refund_both("E卡決鬥逾時退款")
            pending = []
            if self.challenger.id not in self.picks:
                pending.append(self.challenger.mention)
            if self.opponent.id not in self.picks:
                pending.append(self.opponent.mention)
            try:
                for child in view.children:
                    child.disabled = True
                if self.message:
                    await self.message.edit(
                        content=(
                            f"⌛ E 卡決鬥逾時（第 {self.game_no} 大局・第 {self.round_in_game} 小局未完成；"
                            f"未選：{', '.join(pending) or '—'}）。雙方注金 `{self.bet:,}` 已退回。"
                        ),
                        embed=None,
                        view=view,
                    )
            except Exception:
                pass

    class EDuelPlayView(discord.ui.View):
        def __init__(self, match: EDuelMatch):
            super().__init__(timeout=300)
            self.match = match

        @discord.ui.button(label="選牌（私下）", style=discord.ButtonStyle.primary, emoji="🎴")
        async def pick(self, interaction: discord.Interaction, button: discord.ui.Button):
            m = self.match
            if interaction.user.id not in (m.challenger.id, m.opponent.id):
                return await interaction.response.send_message("你不是這場決鬥的玩家。", ephemeral=True)
            if m.settled:
                return await interaction.response.send_message("此場決鬥已結束。", ephemeral=True)
            if interaction.user.id in m.picks:
                return await interaction.response.send_message(
                    "你本小局已經選過了，等對手出牌。", ephemeral=True
                )
            view = EDuelPickerView(m, interaction.user.id)
            await interaction.response.send_message(
                embed=m.build_picker_embed(interaction.user.id),
                view=view,
                ephemeral=True,
            )

        async def on_timeout(self):
            await self.match.handle_timeout(self)

    @bot.tree.command(
        name="duel",
        description="E 卡決鬥（賭博默示錄風）：兩大局制，第二大局交換陣營，依積分分配彩池",
    )
    @app_commands.describe(member="對手", bet="雙方下注金額（兩邊相同）")
    async def duel_slash(
        interaction: discord.Interaction,
        member: discord.Member,
        bet: int,
    ):
        if not feature_toggles.get("duel", True):
            return await interaction_send(interaction, "⛔ `/duel` 目前暫時關閉中。", ephemeral=True)
        if not interaction.guild:
            return await interaction_send(interaction, "請在伺服器頻道使用。", ephemeral=True)
        if member.bot:
            return await interaction_send(interaction, "不能對機器人發起決鬥。", ephemeral=True)
        if member.id == interaction.user.id:
            return await interaction_send(interaction, "不能對自己發起決鬥。", ephemeral=True)
        if bet <= 0:
            return await interaction_send(interaction, "注金需大於 0。", ephemeral=True)
        await interaction_defer_if_needed(interaction)
        await ensure_user_exists_async(interaction.user.id, 50000)
        await ensure_user_exists_async(member.id, 50000)
        if not await try_deduct_balance_async(interaction.user.id, bet, "E卡決鬥下注"):
            return await interaction_send(
                interaction,
                f"❌ 你的餘額不足，需要 `{bet:,}` 東雲幣才能發起決鬥。",
                ephemeral=True,
            )
        view = EDuelInviteView(interaction.user, member, bet)
        sent = await interaction_send(
            interaction,
            content=(
                f"⚔️ {interaction.user.mention} 向 {member.mention} 發起 **E 卡決鬥**！\n"
                f"注額：`{bet:,}` 東雲幣\n"
                f"持牌：國王方 👑×1+🧑×4　奴隸方 🗡️×1+🧑×4\n"
                f"勝：👑 > 🧑、🧑 > 🗡️、🗡️ > 👑\n"
                f"計分：奴贏王 +3｜其餘決勝 +1\n"
                f"規則：兩大局制，第一大局隨機分派陣營，第二大局交換；依積分分配彩池\n"
                f"{member.mention} 請於 **120 秒** 內點 **接受** 或 **拒絕**。"
            ),
            view=view,
        )
        try:
            view.message = sent or await interaction.original_response()
        except Exception:
            pass


__all__ = ["register_duel_commands"]
