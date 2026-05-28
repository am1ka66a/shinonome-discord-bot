import asyncio
import random
import typing

import discord
from discord import app_commands


def register_blackjack_commands(bot, ctx: typing.Dict[str, typing.Any]) -> None:
    logger = ctx["logger"]
    feature_toggles = ctx["FEATURE_TOGGLES"]
    get_is_event_active = ctx["get_is_event_active"]
    side_bet_ratio = float(ctx["SIDE_BET_RATIO"])
    level_mile_tiers = tuple(ctx["LEVEL_MILE_TIERS"])
    ensure_user_exists = ctx["ensure_user_exists"]
    ensure_user_exists_async = ctx["ensure_user_exists_async"]
    get_user_stats = ctx["get_user_stats"]
    get_user_stats_async = ctx["get_user_stats_async"]
    try_deduct_balance_async = ctx["try_deduct_balance_async"]
    update_game_result_async = ctx["update_game_result_async"]
    add_user_exp_async = ctx["add_user_exp_async"]
    process_level_ups = ctx["process_level_ups"]
    roll_gamble_exp_from_bet = ctx["roll_gamble_exp_from_bet"]
    interaction_send = ctx["interaction_send"]
    interaction_defer_if_needed = ctx["interaction_defer_if_needed"]

    def get_deck(num_decks=6):
        suits = ["♥️", "♦️", "♣️", "♠️"]
        ranks = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
        return [{"rank": r, "suit": s} for s in suits for r in ranks] * num_decks

    def card_to_emoji(card, guild_id=None) -> str:
        return f"**[{card['rank']} {card['suit']}]**"

    def card_back_emoji(guild_id=None) -> str:
        return "**[??]**"

    async def send_game(
        channel,
        gv: "BlackjackGame",
        interaction: discord.Interaction = None,
        message_obj: discord.Message = None,
        view=None,
        done=False,
        res="",
        profit=0,
        animating=False,
        extra_msg="",
    ) -> discord.Message:
        async def with_discord_retry(
            coro_factory: typing.Callable[[], typing.Awaitable[typing.Any]],
            *,
            action: str,
        ):
            last_exc: typing.Optional[Exception] = None
            for attempt in range(3):
                try:
                    return await coro_factory()
                except discord.DiscordServerError as e:
                    last_exc = e
                except discord.HTTPException as e:
                    if int(getattr(e, "status", 0) or 0) >= 500:
                        last_exc = e
                    else:
                        raise
                if attempt < 2:
                    await asyncio.sleep(0.6 * (attempt + 1))
            logger.warning("Discord API 臨時失敗（%s）重試後仍失敗: %s", action, last_exc)
            if last_exc:
                raise last_exc
            raise RuntimeError(f"Discord API retry failed: {action}")

        if hasattr(gv, "_build_embed_async"):
            embed = await gv._build_embed_async(
                done=done,
                res=res,
                profit=profit,
                animating=animating,
                extra_msg=extra_msg,
                guild_id=channel.guild.id if channel.guild else None,
            )
        else:
            embed = gv.build_embed(
                done=done,
                res=res,
                profit=profit,
                animating=animating,
                extra_msg=extra_msg,
                guild_id=channel.guild.id if channel.guild else None,
            )
        current_view = view if view is not None else gv

        if interaction:
            if interaction.response.is_done():
                if interaction.message is not None:
                    return await with_discord_retry(
                        lambda: interaction.message.edit(embed=embed, view=current_view, attachments=[]),
                        action="interaction.message.edit",
                    )
                return await with_discord_retry(
                    lambda: interaction.edit_original_response(embed=embed, view=current_view, attachments=[]),
                    action="interaction.edit_original_response",
                )
            await with_discord_retry(
                lambda: interaction.response.edit_message(embed=embed, view=current_view, attachments=[]),
                action="interaction.response.edit_message",
            )
            if interaction.message is not None:
                return interaction.message
            return await with_discord_retry(
                lambda: interaction.original_response(),
                action="interaction.original_response",
            )
        if message_obj:
            return await with_discord_retry(
                lambda: message_obj.edit(embed=embed, view=current_view, attachments=[]),
                action="message_obj.edit",
            )
        return await with_discord_retry(
            lambda: channel.send(embed=embed, view=current_view),
            action="channel.send",
        )

    def calculate_score(hand):
        score, aces = 0, 0
        values = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10, "J": 10, "Q": 10, "K": 10, "A": 11}
        for c in hand:
            score += values[c["rank"]]
            if c["rank"] == "A":
                aces += 1
        while score > 21 and aces:
            score -= 10
            aces -= 1
        return score

    def check_sidebets(player_hand, dealer_up, p_bet, s_bet):
        res_msg, total_p = "", 0
        if p_bet > 0:
            c1, c2 = player_hand[0], player_hand[1]
            if c1["rank"] == c2["rank"]:
                if c1["suit"] == c2["suit"]:
                    mult, m = 30, "同花對子"
                else:
                    mult, m = 5, "混合對子"
                total_p += p_bet * mult
                res_msg += f"🧧 {m}！+{p_bet*mult} "
            else:
                total_p -= p_bet
                res_msg += f"🧧 對子未中 -{p_bet} "
        if s_bet > 0:
            cards = [player_hand[0], player_hand[1], dealer_up]
            suits = [c["suit"] for c in cards]
            rv = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10, "J": 11, "Q": 12, "K": 13, "A": 14}
            v = sorted([rv[c["rank"]] for c in cards])
            if v == [2, 3, 14]:
                v = [1, 2, 3]
            is_flush = len(set(suits)) == 1
            is_straight = v[2] - v[1] == 1 and v[1] - v[0] == 1
            is_triplet = len(set([c["rank"] for c in cards])) == 1
            if is_flush and is_triplet:
                mult, m = 50, "同花三條"
            elif is_flush and is_straight:
                mult, m = 25, "同花順"
            elif is_triplet:
                mult, m = 25, "三條"
            elif is_straight:
                mult, m = 10, "順子"
            elif is_flush:
                mult, m = 5, "同花"
            else:
                mult, m = -1, "未中"

            if mult > 0:
                total_p += s_bet * mult
                res_msg += f"🎯 21+3 {m}！+{s_bet*mult} "
            else:
                total_p -= s_bet
                res_msg += f"🎯 21+3 未中 -{s_bet} "
        return total_p, res_msg

    class BetModal(discord.ui.Modal, title="自訂下注金額"):
        def __init__(self, view):
            super().__init__()
            self.view = view
            self.b_input = discord.ui.TextInput(label="主注 (最低 100)", default=str(view.base_bet), required=True)
            self.p_input = discord.ui.TextInput(label="對子旁注", default=str(view.p_bet), required=False)
            self.s_input = discord.ui.TextInput(label="21+3旁注", default=str(view.s_bet), required=False)
            self.add_item(self.b_input)
            self.add_item(self.p_input)
            self.add_item(self.s_input)

        async def on_submit(self, interaction: discord.Interaction):
            try:
                b = int(self.b_input.value)
                p = int(self.p_input.value or 0)
                s = int(self.s_input.value or 0)
                if b < 100 or p < 0 or s < 0:
                    raise ValueError
            except ValueError:
                return await interaction.response.send_message("請輸入有效正整數 (主注最低 100)", ephemeral=True)
            max_side = int(b * side_bet_ratio)
            if p + s > max_side:
                return await interaction.response.send_message(
                    f"旁注總和 ({p+s}) 不能超過主注的 {int(side_bet_ratio*100)}% ({max_side})",
                    ephemeral=True,
                )
            await ensure_user_exists_async(self.view.user.id, 50000)
            stats = await get_user_stats_async(self.view.user.id)
            if stats[0] < (b + p + s):
                return await interaction.response.send_message(f"餘額不足！你目前有 {stats[0]} 東雲幣", ephemeral=True)
            self.view.base_bet = b
            self.view.max_side = max_side
            self.view.p_bet = p
            self.view.s_bet = s
            await interaction.response.edit_message(embed=await self.view._build_embed_async(), view=self.view)

    class SetupView(discord.ui.View):
        def __init__(self, user, base_bet, p_bet=0, s_bet=0):
            super().__init__(timeout=90)
            self.user, self.base_bet = user, base_bet
            self.p_bet, self.s_bet = p_bet, s_bet
            self.max_side = int(base_bet * side_bet_ratio)

        async def _build_embed_async(self, err: str = "") -> discord.Embed:
            await ensure_user_exists_async(self.user.id, 50000)
            stats = await get_user_stats_async(self.user.id)
            bal = stats[0] if stats else 0
            embed = discord.Embed(title="🃏 21點 — 下注設定", color=0x2B2D31)
            err_prefix = f"❌ {err}\n" if err else ""
            embed.description = (
                f"{err_prefix}主注：`{self.base_bet}`\n"
                f"旁注剩餘額度：**`{self.max_side - (self.p_bet + self.s_bet)}`**\n"
                f"你的餘額：`{bal}`"
            )
            embed.add_field(name="🧧 對子旁注", value=f"下注金額：`{self.p_bet}`\n**同花對子**: 30倍\n**混合對子**: 5倍", inline=True)
            embed.add_field(name="🎯 21+3旁注", value=f"下注金額：`{self.s_bet}`\n**同花三條**: 50倍\n**同花順**: 25倍\n**三條**: 25倍\n**順子**: 10倍\n**同花**: 5倍", inline=True)
            return embed

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
            ensure_user_exists(self.user.id, 50000)
            stats = get_user_stats(self.user.id)
            embed = discord.Embed(title="🃏 21點 — 下注設定", color=0x2B2D31)
            err_prefix = f"❌ {err}\n" if err else ""
            embed.description = f"{err_prefix}主注：`{self.base_bet}`\n旁注剩餘額度：**`{self.max_side - (self.p_bet + self.s_bet)}`**\n你的餘額：`{stats[0]}`"
            embed.add_field(name="🧧 對子旁注", value=f"下注金額：`{self.p_bet}`\n**同花對子**: 30倍\n**混合對子**: 5倍", inline=True)
            embed.add_field(name="🎯 21+3旁注", value=f"下注金額：`{self.s_bet}`\n**同花三條**: 50倍\n**同花順**: 25倍\n**三條**: 25倍\n**順子**: 10倍\n**同花**: 5倍", inline=True)
            return embed

        @discord.ui.button(label="開始遊戲 (再來一局)", style=discord.ButtonStyle.success)
        async def start(self, inter, btn):
            if inter.user.id != self.user.id:
                return
            if not feature_toggles.get("bj", True) or not get_is_event_active():
                return await inter.response.send_message("打烊", ephemeral=True)
            await inter.response.defer()
            await ensure_user_exists_async(self.user.id, 50000)
            total_cost = self.base_bet + self.p_bet + self.s_bet
            if not await try_deduct_balance_async(self.user.id, total_cost, "21點開局扣款"):
                return await inter.followup.send("餘額不足", ephemeral=True)
            self.stop()
            gv = BlackjackGame(self.user, self.base_bet, self.p_bet, self.s_bet, upfront_cost=total_cost)
            msg = await send_game(inter.channel, gv, interaction=inter)
            if msg is not None:
                await gv.check_auto_bj(msg)
            else:
                logger.error("21點 SetupView.start：_send_game 未回傳訊息，略過自動 BJ 結算 user=%s", inter.user.id)

        @discord.ui.button(label="自訂下注金額", style=discord.ButtonStyle.primary)
        async def custom_bet(self, inter, btn):
            if inter.user.id != self.user.id:
                return
            await inter.response.send_modal(BetModal(self))

    class BlackjackGame(discord.ui.View):
        def __init__(self, user, bet, p_bet, s_bet, upfront_cost=0):
            super().__init__(timeout=90)
            self.user, self.bet, self.p_bet, self.s_bet = user, bet, p_bet, s_bet
            self.total_deducted = upfront_cost
            self.hand_bets = [bet]
            self.deck = get_deck()
            random.shuffle(self.deck)
            self.hands = [[self.deck.pop(), self.deck.pop()]]
            self.d_hand = [self.deck.pop(), self.deck.pop()]
            self.current_hand = 0
            self.hand_results = [None]
            self._action_lock = asyncio.Lock()
            self._auto_settling = False
            self.side_p, self.side_m = check_sidebets(self.hands[0], self.d_hand[0], p_bet, s_bet)
            self.update_buttons()

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.user.id:
                await interaction.response.send_message("這不是你的牌局！", ephemeral=True)
                return False
            if self._action_lock.locked():
                await interaction.response.send_message("⏳ 上一個操作仍在處理中，請稍候。", ephemeral=True)
                return False
            if self._auto_settling:
                await interaction.response.send_message("⏳ 起手 BlackJack 結算中，請稍候。", ephemeral=True)
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
                    await send_game(interaction.channel, self, interaction=interaction, done=done, res=res, profit=profit, animating=animating, extra_msg=extra_msg)
                elif message:
                    await send_game(message.channel, self, message_obj=message, done=done, res=res, profit=profit, animating=animating, extra_msg=extra_msg)
            except Exception:
                logger.exception("21點渲染錯誤 user=%s", self.user.id)

        async def _build_embed_async(self, done=False, res="", profit=0, animating=False, extra_msg="", guild_id=None):
            stats = await get_user_stats_async(self.user.id)
            if stats:
                bal, total, wins, t_prof = stats
            else:
                bal, total, wins, t_prof = 0, 0, 0, 0
            wr = (wins / total * 100) if total > 0 else 0
            embed = discord.Embed(title="🃏 21點大賽", color=0x2B2D31)
            main_ui = f"💰 餘額：{bal} | 🏆 勝場：{wins} | 🎲 總局數：{total} | 📈 勝率：{wr:.1f}% | 💸 總盈虧：{t_prof}\n"
            if extra_msg:
                main_ui += f"**{extra_msg}**\n"
            for i, hand in enumerate(self.hands):
                indicator = "👉 " if i == self.current_hand and not done else ""
                title_text = f"{indicator}👤 {self.user.display_name} 的手牌"
                if len(self.hands) > 1:
                    title_text += f" (第 {i+1} 手)"
                p_cards = " ".join([card_to_emoji(c, guild_id) for c in hand])
                main_ui += f"### {title_text}\n### {p_cards} (點數: **{calculate_score(hand)}**)\n"
            if done or animating:
                d_cards = " ".join([card_to_emoji(c, guild_id) for c in self.d_hand])
                main_ui += f"### 🤖 莊家手牌\n### {d_cards} (點數: **{calculate_score(self.d_hand)}**)\n"
                if done:
                    side_profit = self.side_p
                    total_profit = profit + side_profit
                    res_line = f"### 🏆 {res}\n{self.side_m}\n"
                    side_text = f"+{side_profit}" if side_profit > 0 else str(side_profit)
                    res_line += f"🧾 主局淨損益：`{profit:+d}` | 旁注淨損益：`{side_text}`\n"
                    if total_profit > 0:
                        res_line += f"📈 本局淨損益：`+{total_profit}` | 💰 餘額：`{bal}`\n"
                    elif total_profit < 0:
                        res_line += f"📉 本局淨損益：`{total_profit}` | 💰 餘額：`{bal}`\n"
                    else:
                        res_line += f"➖ 本局淨損益：`0` | 💰 餘額：`{bal}`\n"
                    main_ui += res_line
            else:
                main_ui += f"### 🤖 莊家手牌\n### {card_to_emoji(self.d_hand[0], guild_id)} {card_back_emoji(guild_id)} (點數: **❓**)\n"
            embed.description = main_ui
            return embed

        @property
        def p_hand(self):
            return self.hands[self.current_hand]

        def update_buttons(self):
            values = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10, "J": 10, "Q": 10, "K": 10, "A": 11}
            can_split = len(self.hands) == 1 and len(self.p_hand) == 2 and values[self.p_hand[0]["rank"]] == values[self.p_hand[1]["rank"]]
            can_double = len(self.p_hand) == 2
            for c in self.children:
                if c.label == "分牌":
                    c.disabled = not can_split
                elif c.label == "雙倍":
                    c.disabled = not can_double
                elif c.label == "投降":
                    c.disabled = len(self.p_hand) > 2 or len(self.hands) > 1
                elif c.label == "要牌":
                    c.disabled = calculate_score(self.p_hand) > 21

        def build_embed(self, done=False, res="", profit=0, animating=False, extra_msg="", guild_id=None):
            stats = get_user_stats(self.user.id)
            if stats:
                bal, total, wins, t_prof = stats
            else:
                bal, total, wins, t_prof = 0, 0, 0, 0
            wr = (wins / total * 100) if total > 0 else 0
            embed = discord.Embed(title="🃏 21點大賽", color=0x2B2D31)
            main_ui = f"💰 餘額：{bal} | 🏆 勝場：{wins} | 🎲 總局數：{total} | 📈 勝率：{wr:.1f}% | 💸 總盈虧：{t_prof}\n"
            if extra_msg:
                main_ui += f"**{extra_msg}**\n"
            for i, hand in enumerate(self.hands):
                indicator = "👉 " if i == self.current_hand and not done else ""
                title_text = f"{indicator}👤 {self.user.display_name} 的手牌"
                if len(self.hands) > 1:
                    title_text += f" (第 {i+1} 手)"
                p_cards = " ".join([card_to_emoji(c, guild_id) for c in hand])
                main_ui += f"### {title_text}\n### {p_cards} (點數: **{calculate_score(hand)}**)\n"
            if done or animating:
                d_cards = " ".join([card_to_emoji(c, guild_id) for c in self.d_hand])
                main_ui += f"### 🤖 莊家手牌\n### {d_cards} (點數: **{calculate_score(self.d_hand)}**)\n"
                if done:
                    side_profit = self.side_p
                    total_profit = profit + side_profit
                    res_line = f"### 🏆 {res}\n{self.side_m}\n"
                    side_text = f"+{side_profit}" if side_profit > 0 else str(side_profit)
                    res_line += f"🧾 主局淨損益：`{profit:+d}` | 旁注淨損益：`{side_text}`\n"
                    if total_profit > 0:
                        res_line += f"📈 本局淨損益：`+{total_profit}` | 💰 餘額：`{bal}`\n"
                    elif total_profit < 0:
                        res_line += f"📉 本局淨損益：`{total_profit}` | 💰 餘額：`{bal}`\n"
                    else:
                        res_line += f"➖ 本局淨損益：`0` | 💰 餘額：`{bal}`\n"
                    main_ui += res_line
            else:
                main_ui += f"### 🤖 莊家手牌\n### {card_to_emoji(self.d_hand[0], guild_id)} {card_back_emoji(guild_id)} (點數: **❓**)\n"
            embed.description = main_ui
            return embed

        async def check_auto_bj(self, message):
            if len(self.p_hand) == 2 and calculate_score(self.p_hand) == 21:
                self._auto_settling = True
                for c in self.children:
                    c.disabled = True
                await self._edit(message=message, extra_msg="🌟 起手 BlackJack，正在自動結算...")
                await asyncio.sleep(1.5)
                try:
                    await self.advance_hand(message_obj=message)
                except Exception:
                    logger.exception("21點 check_auto_bj 自動結算失敗 user=%s", self.user.id)
                finally:
                    self._auto_settling = False

        async def end(self, res, prof, win=False, is_push=False, message_obj=None, interaction=None, exp_gain=0, exp_detail=""):
            if getattr(self, "_game_over", False):
                return
            self._game_over = True

            total_p = prof + getattr(self, "side_p", 0)
            settlement_credit = self.total_deducted + total_p
            await update_game_result_async(self.user.id, settlement_credit, total_p, win, is_push)

            if exp_gain > 0:
                await ensure_user_exists_async(self.user.id, 50000)
                exp_result = await add_user_exp_async(self.user.id, exp_gain)
                if exp_result and exp_result[1] > exp_result[0]:
                    old_lv, new_lv = exp_result[0], exp_result[1]
                    if any(old_lv < m <= new_lv for m in level_mile_tiers):
                        asyncio.create_task(process_level_ups(self.user, old_lv, new_lv))
                res = f"{res}\n✨ 經驗值 `+{exp_gain}`"
            else:
                res = f"{res}\n🧊 本局失利，不獲得 EXP"
            if exp_detail:
                res = f"{res}\n{exp_detail}"

            for c in self.children:
                c.disabled = True
            stats = await get_user_stats_async(self.user.id)
            nv = NewGameView(self.user, self.bet, self.p_bet, self.s_bet, stats[0] if stats else 0)
            await send_game(
                message_obj.channel if message_obj else interaction.channel,
                self,
                interaction=interaction,
                message_obj=message_obj,
                view=nv,
                done=True,
                res=res,
                profit=prof,
            )

        async def advance_hand(self, message_obj=None, interaction=None):
            if getattr(self, "_game_over", False):
                return
            if self.current_hand < len(self.hands) - 1:
                self.current_hand += 1
                self.update_buttons()
                await self._edit(message=message_obj, interaction=interaction, extra_msg=f"👉 換第 {self.current_hand+1} 手牌")
                if len(self.p_hand) == 2 and calculate_score(self.p_hand) == 21:
                    await asyncio.sleep(1.5)
                    await self.advance_hand(message_obj=message_obj, interaction=interaction)
            else:
                await self.resolve_dealer(message_obj=message_obj, interaction=interaction)

        async def resolve_dealer(self, message_obj=None, interaction=None):
            if getattr(self, "_game_over", False):
                return
            need_dealer = any(hand is None for hand in self.hand_results)
            for c in self.children:
                c.disabled = True
            await self._edit(message=message_obj, interaction=interaction, animating=True)
            if need_dealer:
                await asyncio.sleep(1.2)
                while calculate_score(self.d_hand) < 17 and len(self.d_hand) < 5:
                    self.d_hand.append(self.deck.pop())
                    await self._edit(message=message_obj, interaction=None, animating=True)
                    await asyncio.sleep(1.2)
            total_prof, final_res_texts = 0, []
            total_exp_gain = 0
            exp_detail_texts = []
            ds = calculate_score(self.d_hand)
            dealer_bj = len(self.d_hand) == 2 and ds == 21
            dealer_5_card = len(self.d_hand) == 5 and ds <= 21
            for i, hand in enumerate(self.hands):
                if self.hand_results[i] is not None:
                    r, p, w = self.hand_results[i]
                    final_res_texts.append(f"第 {i+1} 手: {r}" if len(self.hands) > 1 else r)
                    hand_profit = p
                    total_prof += hand_profit
                    hand_exp_base = roll_gamble_exp_from_bet(self.hand_bets[i])
                    if hand_profit > 0:
                        hand_exp_award = hand_exp_base
                    elif hand_profit == 0:
                        hand_exp_award = max(1, hand_exp_base // 2)
                    else:
                        hand_exp_award = 0
                    total_exp_gain += hand_exp_award
                    if len(self.hands) > 1:
                        exp_detail_texts.append(f"第 {i+1} 手 EXP `+{hand_exp_award}`")
                    continue
                ps = calculate_score(hand)
                player_bj, player_5_card = (len(hand) == 2 and ps == 21), (len(hand) == 5 and ps <= 21)
                if player_5_card and dealer_5_card:
                    final_res_texts.append("🤝 雙方皆過五關！平手")
                    hand_profit = 0
                elif player_5_card:
                    final_res_texts.append("🐉 你過五關啦！爽贏 2.5 倍！")
                    hand_profit = int(self.hand_bets[i] * 2.5)
                elif dealer_5_card:
                    final_res_texts.append("🐉 老子過五關啦！你這低能兒～")
                    hand_profit = -self.hand_bets[i]
                elif player_bj and dealer_bj:
                    final_res_texts.append("🤝 雙方皆為 BlackJack！平手")
                    hand_profit = 0
                elif player_bj:
                    final_res_texts.append("🌟 BlackJack！1.5倍賠率！")
                    hand_profit = int(self.hand_bets[i] * 1.5)
                elif dealer_bj:
                    final_res_texts.append("💀 莊家 BlackJack！你輸啦～雜魚～")
                    hand_profit = -self.hand_bets[i]
                elif ds > 21 or ps > ds:
                    final_res_texts.append("🎉 這次算你贏啦，腦殘！")
                    hand_profit = self.hand_bets[i]
                elif ps < ds:
                    final_res_texts.append("💀 你輸啦～雜魚～")
                    hand_profit = -self.hand_bets[i]
                else:
                    final_res_texts.append("🤝 就這點技術阿腦殘？")
                    hand_profit = 0
                total_prof += hand_profit
                hand_exp_base = roll_gamble_exp_from_bet(self.hand_bets[i])
                if hand_profit > 0:
                    hand_exp_award = hand_exp_base
                elif hand_profit == 0:
                    hand_exp_award = max(1, hand_exp_base // 2)
                else:
                    hand_exp_award = 0
                total_exp_gain += hand_exp_award
                if len(self.hands) > 1:
                    exp_detail_texts.append(f"第 {i+1} 手 EXP `+{hand_exp_award}`")
            final_msg = "\n".join(final_res_texts)
            total_combined = total_prof + getattr(self, "side_p", 0)
            exp_detail = ""
            if len(self.hands) > 1 and exp_detail_texts:
                exp_detail = "🧮 分牌 EXP 明細\n" + "\n".join(exp_detail_texts)
            await self.end(
                final_msg,
                total_prof,
                total_combined > 0,
                total_combined == 0,
                message_obj=message_obj,
                interaction=interaction,
                exp_gain=total_exp_gain,
                exp_detail=exp_detail,
            )

        @discord.ui.button(label="要牌", style=discord.ButtonStyle.success)
        async def hit(self, inter, btn):
            if inter.user.id != self.user.id:
                return
            async with self._action_lock:
                await inter.response.defer()
                self.p_hand.append(self.deck.pop())
                self.update_buttons()
                ps = calculate_score(self.p_hand)
                if ps > 21 or len(self.p_hand) == 5:
                    if ps > 21:
                        self.hand_results[self.current_hand] = ("爆牌輸了", -self.hand_bets[self.current_hand], False)
                    await self.advance_hand(interaction=inter, message_obj=inter.message)
                else:
                    await self._edit(interaction=inter)

        @discord.ui.button(label="停牌", style=discord.ButtonStyle.danger)
        async def stand(self, inter, btn):
            if inter.user.id != self.user.id:
                return
            async with self._action_lock:
                await inter.response.defer()
                await self.advance_hand(interaction=inter, message_obj=inter.message)

        @discord.ui.button(label="投降", style=discord.ButtonStyle.secondary)
        async def surrender(self, inter, btn):
            if inter.user.id != self.user.id:
                return
            async with self._action_lock:
                await inter.response.defer()
                self.hand_results[self.current_hand] = ("這樣就投降了嗎，雜魚～", -(self.hand_bets[self.current_hand] // 2), False)
                await self.advance_hand(interaction=inter, message_obj=inter.message)

        @discord.ui.button(label="雙倍", style=discord.ButtonStyle.primary)
        async def double_down(self, inter, btn):
            if inter.user.id != self.user.id:
                return
            async with self._action_lock:
                await inter.response.defer()
                extra_cost = self.hand_bets[self.current_hand]
                if not await try_deduct_balance_async(self.user.id, extra_cost, "21點雙倍加注"):
                    return await inter.followup.send("餘額不足", ephemeral=True)
                self.total_deducted += extra_cost
                self.hand_bets[self.current_hand] *= 2
                self.p_hand.append(self.deck.pop())
                if calculate_score(self.p_hand) > 21:
                    self.hand_results[self.current_hand] = ("你爆牌囉～小丑～", -self.hand_bets[self.current_hand], False)
                await self.advance_hand(interaction=inter, message_obj=inter.message)

        @discord.ui.button(label="分牌", style=discord.ButtonStyle.primary)
        async def split(self, inter, btn):
            if inter.user.id != self.user.id:
                return
            async with self._action_lock:
                await inter.response.defer()
                if not await try_deduct_balance_async(self.user.id, self.bet, "21點分牌加注"):
                    return await inter.followup.send("餘額不足", ephemeral=True)
                self.total_deducted += self.bet
                self.is_split, c1, c2 = True, self.hands[0][0], self.hands[0][1]
                self.hands, self.hand_results, self.hand_bets = [[c1, self.deck.pop()], [c2, self.deck.pop()]], [None, None], [self.bet, self.bet]
                self.update_buttons()
                await self._edit(interaction=inter, extra_msg="✌️ 你選擇了分牌！")
                if calculate_score(self.p_hand) == 21:
                    await asyncio.sleep(1.5)
                    await self.advance_hand(interaction=None, message_obj=inter.message)

    class ConfirmAllInView(discord.ui.View):
        def __init__(self, user, parent_msg):
            super().__init__(timeout=30)
            self.user, self.parent_msg = user, parent_msg

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.user.id:
                return False
            return True

        @discord.ui.button(label="確定 All In！", style=discord.ButtonStyle.danger)
        async def confirm(self, inter, btn):
            if not feature_toggles.get("bj", True) or not get_is_event_active():
                return await inter.response.send_message("打烊", ephemeral=True)
            stats = await get_user_stats_async(self.user.id)
            all_in_amount = int((stats[0] if stats else 0) or 0)
            if all_in_amount < 100:
                return await inter.response.send_message("去乞討吧雜魚", ephemeral=True)
            if not await try_deduct_balance_async(self.user.id, all_in_amount, "21點 All In 開局扣款"):
                return await inter.response.send_message("餘額不足（可能剛轉帳/變動），請重新開局。", ephemeral=True)
            self.stop()
            await inter.response.edit_message(content="🔥 All In 已確認！正在為你開牌...", view=None)
            try:
                await self.parent_msg.delete()
            except Exception:
                pass
            gv = BlackjackGame(self.user, all_in_amount, 0, 0, upfront_cost=all_in_amount)
            msg = await send_game(inter.channel, gv)
            if msg is not None:
                await gv.check_auto_bj(msg)

    class NewGameView(discord.ui.View):
        def __init__(self, user, last_bet, last_p_bet, last_s_bet, current_bal):
            super().__init__(timeout=90)
            self.user, self.last_bet, self.last_p_bet, self.last_s_bet, self.current_bal = user, last_bet, last_p_bet, last_s_bet, current_bal

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.user.id:
                return False
            return True

        @discord.ui.button(label="再來一局", style=discord.ButtonStyle.success)
        async def again(self, inter, btn):
            if inter.user.id != self.user.id:
                return
            if not feature_toggles.get("bj", True) or not get_is_event_active():
                return await inter.response.send_message("打烊", ephemeral=True)
            await inter.response.defer()
            total_cost = self.last_bet + self.last_p_bet + self.last_s_bet
            if not await try_deduct_balance_async(self.user.id, total_cost, "21點開局扣款"):
                return await inter.followup.send("餘額不足", ephemeral=True)
            self.stop()
            gv = BlackjackGame(self.user, self.last_bet, self.last_p_bet, self.last_s_bet, upfront_cost=total_cost)
            msg = await send_game(inter.channel, gv, interaction=inter)
            if msg is not None:
                await gv.check_auto_bj(msg)
            else:
                logger.error("21點 NewGameView.again：_send_game 未回傳訊息，略過自動 BJ 結算 user=%s", inter.user.id)

        @discord.ui.button(label="雙倍再局 (Double)", style=discord.ButtonStyle.primary)
        async def double_again(self, inter, btn):
            if inter.user.id != self.user.id:
                return
            if not feature_toggles.get("bj", True) or not get_is_event_active():
                return await inter.response.send_message("打烊", ephemeral=True)
            await inter.response.defer()
            new_bet = self.last_bet * 2
            total_cost = new_bet + self.last_p_bet + self.last_s_bet
            if not await try_deduct_balance_async(self.user.id, total_cost, "21點開局扣款"):
                return await inter.followup.send("餘額不足", ephemeral=True)
            self.stop()
            gv = BlackjackGame(self.user, new_bet, self.last_p_bet, self.last_s_bet, upfront_cost=total_cost)
            msg = await send_game(inter.channel, gv, interaction=inter)
            if msg is not None:
                await gv.check_auto_bj(msg)
            else:
                logger.error("21點 NewGameView.double_again：_send_game 未回傳訊息，略過自動 BJ 結算 user=%s", inter.user.id)

        @discord.ui.button(label="修改下注", style=discord.ButtonStyle.secondary)
        async def modify_bet(self, inter, btn):
            self.stop()
            await inter.response.defer()
            try:
                await inter.message.delete()
            except Exception:
                pass
            setup = SetupView(self.user, self.last_bet, self.last_p_bet, self.last_s_bet)
            await inter.channel.send(embed=setup.build_embed(), view=setup)

        @discord.ui.button(label="All In (全押)", style=discord.ButtonStyle.danger)
        async def all_in(self, inter, btn):
            cv = ConfirmAllInView(self.user, inter.message)
            await inter.response.send_message("⚠️ 警告：要全押嗎雜魚？", view=cv, ephemeral=True)

    @bot.tree.command(name="bj", description="開始 21 點")
    @app_commands.describe(bet="注額")
    async def bj(interaction: discord.Interaction, bet: int = 1000):
        if not feature_toggles.get("bj", True):
            return await interaction_send(interaction, "⛔ `/bj` 目前暫時關閉中。", ephemeral=True)
        if not get_is_event_active():
            return await interaction_send(interaction, "打烊", ephemeral=True)
        if bet < 100:
            return await interaction_send(interaction, "低消 100", ephemeral=True)
        await interaction_defer_if_needed(interaction)
        await ensure_user_exists_async(interaction.user.id, 50000)
        sv = SetupView(interaction.user, bet)
        await interaction_send(interaction, embed=await sv._build_embed_async(), view=sv)


__all__ = ["register_blackjack_commands"]
