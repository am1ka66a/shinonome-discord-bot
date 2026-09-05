import asyncio
import json
import random
import typing

import discord
from discord import app_commands

from bot_modules.db import db_cursor
from bot_modules.runtime import snapshot_cache


class _AbortWithMessage(Exception):
    """在持有 DB 連線期間中止流程；訊息等連線歸還之後才回覆，避免佔著連線 await。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _invalidate_wanted_caches(*user_ids: int) -> None:
    fn = snapshot_cache.invalidate_wanted_caches
    if fn is not None:
        fn(*user_ids)


async def _resolve_display_names(
    guild: discord.Guild,
    user_ids: typing.Sequence[int],
) -> typing.Dict[int, str]:
    """併發解析多位成員的顯示名稱；查不到的一律回「未知成員」。"""
    resolver = snapshot_cache.resolve_guild_member_cached

    async def _one(uid: int):
        if resolver is not None:
            return await resolver(guild, uid)
        member = guild.get_member(uid)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(uid)
        except Exception:
            return None

    members = await asyncio.gather(*(_one(uid) for uid in user_ids), return_exceptions=True)
    out: typing.Dict[int, str] = {}
    for uid, member in zip(user_ids, members):
        out[uid] = member.display_name if isinstance(member, discord.Member) else "未知成員"
    return out


def register_wanted_commands(bot, ctx: typing.Dict[str, typing.Any]) -> None:
    FEATURE_TOGGLES = ctx["FEATURE_TOGGLES"]
    ROB_COOLDOWN_SECONDS = ctx["ROB_COOLDOWN_SECONDS"]
    ROB_VICTIM_PROTECT_SECONDS = ctx["ROB_VICTIM_PROTECT_SECONDS"]
    ROB_BASE_SUCCESS_RATE = ctx["ROB_BASE_SUCCESS_RATE"]
    COP_HUNT_FEE = ctx["COP_HUNT_FEE"]
    COP_HUNT_CAPTURE_BASE_PCT = ctx["COP_HUNT_CAPTURE_BASE_PCT"]
    COP_HUNT_CAPTURE_PER_STAR_PCT = ctx["COP_HUNT_CAPTURE_PER_STAR_PCT"]
    BAIL_COST = ctx["BAIL_COST"]
    WANTED_BUYOUT_COST = ctx["WANTED_BUYOUT_COST"]
    GOOD_CITIZEN_CERT_COST = ctx["GOOD_CITIZEN_CERT_COST"]
    GOOD_CITIZEN_DESTROY_COST = ctx["GOOD_CITIZEN_DESTROY_COST"]
    resolve_slash_target = ctx["resolve_slash_target"]
    ensure_user_exists_async = ctx["ensure_user_exists_async"]
    load_rob_context_async = ctx["load_rob_context_async"]
    apply_rob_success_db_async = ctx["apply_rob_success_db_async"]
    apply_rob_fail_db_async = ctx["apply_rob_fail_db_async"]
    db_to_thread = ctx["db_to_thread"]
    log_transaction = ctx["log_transaction"]
    interaction_send = ctx["interaction_send"]
    interaction_defer_if_needed = ctx["interaction_defer_if_needed"]
    choose_role_sync_async = ctx["choose_role_sync_async"]
    now_tw_naive = ctx["now_tw_naive"]
    tw_naive_to_discord_ts = ctx["tw_naive_to_discord_ts"]
    wanted_buyout_sync_async = ctx["wanted_buyout_sync_async"]
    toggle_good_citizen_sync_async = ctx["toggle_good_citizen_sync_async"]
    fetch_good_citizen_rows_sync_async = ctx["fetch_good_citizen_rows_sync_async"]
    break_citizen_sync_async = ctx["break_citizen_sync_async"]
    fetch_wanted_status_row_sync_async = ctx["fetch_wanted_status_row_sync_async"]
    fetch_wanted_list_rows_sync_async = ctx["fetch_wanted_list_rows_sync_async"]
    rob_history_total_from_raw = ctx["rob_history_total_from_raw"]
    get_last_five_robs_total = ctx["get_last_five_robs_total"]
    pay_bail_sync_async = ctx["pay_bail_sync_async"]
    get_db_connection = ctx["get_db_connection"]
    lock_user_rows = ctx["lock_user_rows"]
    log_transaction_in_tx = ctx["log_transaction_in_tx"]

    @bot.tree.command(name="rob", description="搶劫其他玩家（僅搶匪；高風險高報酬）")
    @app_commands.describe(member="要搶劫的對象（選人）", user_id="或填使用者 ID／貼提及")
    async def rob(
        interaction: discord.Interaction,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
    ):
        if not FEATURE_TOGGLES.get("rob", True):
            return await interaction.response.send_message("⛔ `/rob` 目前暫時關閉中。", ephemeral=True)
        m_user, err = await resolve_slash_target(
            interaction, member, user_id, required=True, in_guild_only=True
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        if not isinstance(m_user, discord.Member):
            return await interaction.response.send_message("搶劫目標必須是此伺服器成員。", ephemeral=True)
        member = m_user
        if member.bot:
            return await interaction.response.send_message("不能搶劫機器人。", ephemeral=True)
        if member.id == interaction.user.id:
            return await interaction.response.send_message("你不能搶劫自己。", ephemeral=True)

        await ensure_user_exists_async(interaction.user.id, 50000)
        await ensure_user_exists_async(member.id, 0)

        ctx = await load_rob_context_async(interaction.user.id, member.id)
        if ctx["in_prison"]:
            return await interaction.response.send_message("🔒 你在監獄裡無法搶劫。", ephemeral=True)
        if ctx["robber_role"] != "criminal":
            return await interaction.response.send_message(
                "❌ 只有**搶匪**可以搶劫。請先用 `/role_choose` 選擇搶匪（criminal）。",
                ephemeral=True,
            )
        robber_balance = int(ctx["robber_balance"])
        last_rob = ctx["last_rob"]
        robber_level = int(ctx["robber_level"])
        target_balance = int(ctx["target_balance"])
        target_level = int(ctx["target_level"])
        target_last_robbed = ctx["target_last_robbed"]
        target_good_cert = int(ctx["target_good_cert"])
        now = now_tw_naive()

        if last_rob and (now - last_rob).total_seconds() < ROB_COOLDOWN_SECONDS:
            remain = ROB_COOLDOWN_SECONDS - int((now - last_rob).total_seconds())
            mins = max(1, remain // 60)
            return await interaction.response.send_message(f"⏳ 你剛搶過，請再等 `{mins}` 分鐘。", ephemeral=True)

        if target_balance < 50000:
            return await interaction.response.send_message("對方太窮了，沒有東西可以搶。", ephemeral=True)
        if robber_balance < 50000:
            return await interaction.response.send_message("你的餘額低於 50,000，無法發起搶劫。", ephemeral=True)
        if target_good_cert:
            return await interaction.response.send_message(
                "🪪 對方已啟用良民證，無法被搶劫。",
                ephemeral=True,
            )
        if target_last_robbed and (now - target_last_robbed).total_seconds() < ROB_VICTIM_PROTECT_SECONDS:
            remain = ROB_VICTIM_PROTECT_SECONDS - int((now - target_last_robbed).total_seconds())
            mins = max(1, remain // 60)
            return await interaction.response.send_message(
                f"對方目前有保護，請 `{mins}` 分鐘後再試。",
                ephemeral=True,
            )

        await interaction_defer_if_needed(interaction)

        # /rob 基礎成功率 ROB_BASE_SUCCESS_RATE；每差 1 等調整 1%
        level_gap = robber_level - target_level
        success_rate = ROB_BASE_SUCCESS_RATE + (level_gap * 0.01)
        success_rate = max(0.05, min(0.95, success_rate))
        success_rate_pct = int(round(success_rate * 100))
        success = random.random() < success_rate
        robber_name = interaction.user.display_name
        victim_name = member.display_name

        if success:
            success_result = await apply_rob_success_db_async(
                interaction.user.id,
                member.id,
                now,
                success_rate_pct,
            )
            if not success_result.get("ok"):
                return await interaction_send(interaction, "對方及時把錢藏好了，這次搶劫失敗。", ephemeral=True)
            _invalidate_wanted_caches(interaction.user.id, member.id)
            steal_amount = int(success_result["steal_amount"])
            wanted_info = success_result["wanted_info"]
            counter_note = success_result.get("counter_note", "")
            await db_to_thread(log_transaction, interaction.user.id, steal_amount, f"搶劫成功（目標:{member.id}）")
            await db_to_thread(log_transaction, member.id, -steal_amount, f"被搶劫（搶匪:{interaction.user.id}）")
            return await interaction_send(
                interaction,
                f"{robber_name}搶了{victim_name}`{steal_amount:,}`東雲幣!!（本次成功率約 {success_rate_pct}%）{wanted_info}{counter_note}"
            )

        fail_result = await apply_rob_fail_db_async(interaction.user.id, member.id, now)
        fail_penalty = int(fail_result["fail_penalty"])
        deducted = bool(fail_result["deducted"])
        if deducted:
            await db_to_thread(log_transaction, interaction.user.id, -fail_penalty, f"搶劫失敗反噬（目標:{member.id}）")
            await db_to_thread(log_transaction, member.id, fail_penalty, f"反制搶劫獲賠（搶匪:{interaction.user.id}）")
            return await interaction_send(
                interaction,
                f"{robber_name}失手了! 反而被{victim_name}搶了`{fail_penalty:,}`東雲幣!（本次成功率約 {success_rate_pct}%）"
            )
        return await interaction_send(
            interaction,
            f"{robber_name}失手了! 反而被{victim_name}搶了`{fail_penalty:,}`東雲幣!（本次成功率約 {success_rate_pct}%）"
        )


    @bot.tree.command(name="role_choose", description="切換陣營：警察／搶匪／平民（24 小時冷卻）")
    @app_commands.describe(role="要切換的陣營")
    @app_commands.choices(
        role=[
            app_commands.Choice(name="警察", value="cop"),
            app_commands.Choice(name="搶匪", value="criminal"),
            app_commands.Choice(name="平民", value="civilian"),
        ]
    )
    async def role_choose_slash(interaction: discord.Interaction, role: str):
        if role not in ("cop", "criminal", "civilian"):
            return await interaction.response.send_message(
                "❌ 請從選單選擇 **警察**、**搶匪** 或 **平民**。",
                ephemeral=True,
            )
        now = now_tw_naive()
        role_result = await choose_role_sync_async(interaction.user.id, role, now)
        if not role_result.get("ok"):
            reason = role_result.get("reason")
            if reason == "cert_active":
                return await interaction.response.send_message(
                    "❌ 你目前已啟用良民證，無法切換身分。請先使用 `/good_citizen` 解除後再轉職。",
                    ephemeral=True,
                )
            if reason == "cooldown":
                ts = tw_naive_to_discord_ts(role_result["next_dt"])
                return await interaction.response.send_message(
                    f"⏳ 轉職冷卻中，下次可於 <t:{ts}:F>（<t:{ts}:R>）再切換陣營。",
                    ephemeral=True,
                )
            if reason == "already_civilian":
                return await interaction.response.send_message("ℹ️ 你目前已是**平民**。", ephemeral=True)
            if reason == "wanted_block":
                wanted_now = int(role_result.get("wanted_now") or 0)
                return await interaction.response.send_message(
                    f"❌ 搶匪轉為警察或平民須 **通緝 0 星**（目前 {wanted_now} 星）。請先透過追捕／入獄等流程歸零後再切換。",
                    ephemeral=True,
                )
            return await interaction.response.send_message("❌ 轉職失敗，請稍後再試。", ephemeral=True)
        old_role = role_result.get("old_role", "civilian")

        role_name = (
            "🚔 警察"
            if role == "cop"
            else ("🔪 搶匪" if role == "criminal" else "👤 平民")
        )
        old_role_name = (
            "🚔 警察"
            if old_role == "cop"
            else ("🔪 搶匪" if old_role == "criminal" else "👤 平民")
        )
        emb = discord.Embed(
            title="✅ 角色選擇成功",
            description=f"從 {old_role_name} 切換為 {role_name}",
            color=0x57F287,
        )
        if role == "cop":
            emb.add_field(
                name="🚔 警察",
                value=(
                    "• 使用 `/cop_hunt` 選擇通緝犯並嘗試逮捕\n"
                    f"• 每次追捕會先扣 `{COP_HUNT_FEE:,}`（成敗皆扣）\n"
                    f"• 成功率：通緝星級每星 +{COP_HUNT_CAPTURE_PER_STAR_PCT}%，並受雙方等級差影響（每級 ±1%，保底 5%、上限 95%）\n"
                    "• 成功可獲得對方最近五次搶劫總額獎金（另依規則沒收）\n"
                    "• 每次切換陣營皆有 24 小時冷卻"
                ),
                inline=False,
            )
        elif role == "criminal":
            emb.add_field(
                name="🔪 搶匪",
                value=(
                    "• `/rob` 搶劫成功會累積通緝星（最高 5）\n"
                    "• 通緝星級越高，且你等級越低於警察時，遭追捕成功率越高\n"
                    "• 入獄後可用 `/bail`：基礎假釋金 + 累計欠款（沒收／反搶不足皆會併入）\n"
                    "• 轉回警察或平民前，通緝必須先歸零"
                ),
                inline=False,
            )
        else:
            emb.add_field(
                name="👤 平民",
                value=(
                    "• 不再以警察／搶匪身分參與通緝與追捕\n"
                    "• 可用 `/good_citizen` 啟用／解除良民證（需付費，且有 24 小時冷卻）\n"
                    "• 可隨時再用 `/role_choose` 重新選擇陣營（24 小時冷卻）"
                ),
                inline=False,
            )
        _invalidate_wanted_caches(interaction.user.id)
        await interaction_send(interaction, embed=emb)


    @bot.tree.command(
        name="cop_hunt",
        description=f"警察追捕通緝犯（每次須付 {COP_HUNT_FEE:,} 東雲幣；成功可獲贓款、對方入獄）",
    )
    @app_commands.describe(
        member="通緝犯（選人）",
        user_id="或填使用者 ID／貼提及",
    )
    async def cop_hunt_slash(
        interaction: discord.Interaction,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
    ):
        criminal_user, err = await resolve_slash_target(
            interaction, member, user_id, required=True, in_guild_only=False
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        await interaction_defer_if_needed(interaction)

        await ensure_user_exists_async(interaction.user.id, 50000)
        await ensure_user_exists_async(criminal_user.id, 0)
        criminal_id = str(criminal_user.id)
        cop_id = str(interaction.user.id)

        try:
            with db_cursor(commit=True, connect=get_db_connection) as c:
                # 固定順序鎖定警察/罪犯兩列，避免多人同時追捕造成讀寫交錯
                lock_user_rows(c, [cop_id, criminal_id])

                c.execute(
                    "SELECT COALESCE(role,'civilian'), COALESCE(level,1), COALESCE(balance,0) "
                    "FROM users WHERE user_id=%s FOR UPDATE",
                    (cop_id,),
                )
                cop_row = c.fetchone()
                if not cop_row or cop_row[0] != "cop":
                    raise _AbortWithMessage("❌ 只有**警察**可以追捕。請先用 `/role_choose` 選擇警察。")
                cop_level = int(cop_row[1] or 1)
                cop_balance = int(cop_row[2] or 0)

                c.execute(
                    "SELECT COALESCE(wanted_stars,0), COALESCE(wanted_hunted_count,0), COALESCE(balance,0), "
                    "COALESCE(in_prison,0), COALESCE(level,1), last_five_robs, COALESCE(bail_debt,0) "
                    "FROM users WHERE user_id=%s FOR UPDATE",
                    (criminal_id,),
                )
                criminal_row = c.fetchone()
                if not criminal_row:
                    raise _AbortWithMessage("❌ 找不到該玩家資料。")

                wanted_stars = int(criminal_row[0] or 0)
                hunted_count = int(criminal_row[1] or 0)
                criminal_balance = int(criminal_row[2] or 0)
                in_prison = int(criminal_row[3] or 0)
                criminal_level = int(criminal_row[4] or 1)
                criminal_last_five_raw = criminal_row[5]
                bail_debt_before = int(criminal_row[6] or 0)

                if in_prison:
                    raise _AbortWithMessage(f"ℹ️ {criminal_user.mention} 已在監獄中，無法追捕。")
                if wanted_stars <= 0:
                    raise _AbortWithMessage(f"ℹ️ {criminal_user.mention} 目前沒有通緝度。")
                if criminal_id == cop_id:
                    raise _AbortWithMessage("❌ 不能追捕自己。")

                can_hunt = hunted_count == 0
                if wanted_stars <= 4:
                    hunt_rule = f"{wanted_stars}★：本星級僅能追捕一次（失敗或成功後需再升星或滿星規則）。"
                else:
                    hunt_rule = "5★：每次搶劫成功後可追捕一次（本輪若已追捕過則需等對方再搶劫成功）。"

                if not can_hunt:
                    raise _AbortWithMessage(f"❌ 目前無法追捕。\n{hunt_rule}")

                if cop_balance < COP_HUNT_FEE:
                    raise _AbortWithMessage(
                        f"❌ 每次追捕須支付 **`{COP_HUNT_FEE:,}`** 東雲幣，你的餘額不足。"
                    )
                c.execute(
                    "UPDATE users SET balance=balance-%s WHERE user_id=%s",
                    (COP_HUNT_FEE, cop_id),
                )

                capture_chance_raw = (
                    COP_HUNT_CAPTURE_BASE_PCT
                    + wanted_stars * COP_HUNT_CAPTURE_PER_STAR_PCT
                    + (cop_level - criminal_level)
                )
                capture_chance = max(5, min(95, capture_chance_raw))
                is_caught = random.random() * 100.0 < float(capture_chance)
                now = now_tw_naive()

                c.execute(
                    "INSERT INTO wanted_log (criminal_id, cop_id, wanted_stars, caught) VALUES (%s, %s, %s, %s)",
                    (criminal_id, cop_id, wanted_stars, 1 if is_caught else 0),
                )
                log_transaction_in_tx(c, cop_id, -COP_HUNT_FEE, "追捕行動費用")

                if is_caught:
                    last_five_total, rob_count = rob_history_total_from_raw(criminal_last_five_raw)
                    rob_history: typing.List[typing.Any] = []
                    if criminal_last_five_raw:
                        try:
                            parsed = json.loads(criminal_last_five_raw)
                            if isinstance(parsed, list):
                                rob_history = parsed
                        except Exception:
                            rob_history = []
                    cop_reward = int(last_five_total)
                    confiscated_base = int(last_five_total * 0.6)
                    confiscated_amount = min(confiscated_base, criminal_balance)
                    conf_shortfall = max(0, confiscated_base - confiscated_amount)
                    remaining_bal = max(0, criminal_balance - confiscated_amount)
                    bail_debt_after = bail_debt_before + conf_shortfall
                    total_bail_needed = BAIL_COST + bail_debt_after

                    c.execute(
                        """UPDATE users SET in_prison=1, prison_start=%s,
                           balance=GREATEST(0, balance-%s), arrest_count=arrest_count+1,
                           wanted_stars=0, wanted_hunted_count=0, last_five_robs=NULL,
                           bail_debt=COALESCE(bail_debt,0)+%s
                           WHERE user_id=%s""",
                        (now, confiscated_amount, conf_shortfall, criminal_id),
                    )
                    c.execute(
                        "UPDATE users SET balance=balance+%s WHERE user_id=%s",
                        (cop_reward, cop_id),
                    )
                    log_transaction_in_tx(c, criminal_id, -confiscated_amount, f"被警察逮捕沒收 {confiscated_amount:,}")
                    log_transaction_in_tx(c, cop_id, cop_reward, f"逮捕通緝犯 {criminal_user.id} 贓款")
                    c.execute(
                        """INSERT INTO prison_records
                           (criminal_id, cop_id, wanted_stars, confiscated_amount, cop_reward, bail_cost, arrested_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (criminal_id, cop_id, wanted_stars, confiscated_amount, cop_reward, BAIL_COST, now),
                    )
                else:
                    c.execute(
                        "UPDATE users SET wanted_hunted_count=1 WHERE user_id=%s",
                        (criminal_id,),
                    )
        except _AbortWithMessage as e:
            return await interaction_send(interaction, e.message, ephemeral=True)

        _invalidate_wanted_caches(int(criminal_id))

        if is_caught:

            rob_detail = ""
            if rob_history:
                rob_detail = "\n**最近搶劫紀錄：**\n"
                for i, rob in enumerate(rob_history, 1):
                    if isinstance(rob, dict):
                        rob_detail += f"{i}. `{int(rob.get('amount',0)):,}` 幣（{rob.get('time','')}）\n"

            emb = discord.Embed(
                title="✅ 追捕成功",
                description=f"🚔 {interaction.user.mention} 逮捕了 🔪 {criminal_user.mention}",
                color=0x57F287,
            )
            emb.add_field(name="通緝星級", value="⭐" * wanted_stars, inline=True)
            emb.add_field(name="追捕成功率（本輪）", value=f"`{capture_chance}%`", inline=True)
            emb.add_field(
                name="追捕費用（已扣）",
                value=f"`{COP_HUNT_FEE:,}` 東雲幣",
                inline=True,
            )
            emb.add_field(
                name="警察獲得（最近五次搶劫成功總額）",
                value=f"`{cop_reward:,}` 東雲幣（{rob_count} 筆）",
                inline=False,
            )
            emb.add_field(
                name="沒收（近五次贓款總和 60%）",
                value=f"應沒收 `{confiscated_base:,}`｜實扣 `{confiscated_amount:,}` 東雲幣",
                inline=False,
            )
            if conf_shortfall > 0:
                emb.add_field(
                    name="未沒收差額（併入假釋債務）",
                    value=f"`{conf_shortfall:,}` 東雲幣",
                    inline=True,
                )
            emb.add_field(name="罪犯剩餘餘額", value=f"`{remaining_bal:,}` 東雲幣", inline=True)
            if rob_detail:
                emb.add_field(name="搶劫紀錄", value=rob_detail[:1000], inline=False)
            _debt_txt = ""
            if bail_debt_after > 0:
                _debt_txt = f"累計假釋欠款：`{bail_debt_after:,}` 幣"
                if conf_shortfall > 0:
                    _debt_txt += f"（本次未沒收 `{conf_shortfall:,}`）"
                _debt_txt += "。\n"
            if bail_debt_after > 0:
                _out_txt = (
                    f"出獄請繳：基礎 `{BAIL_COST:,}` + 欠款 `{bail_debt_after:,}` "
                    f"= **合計 `{total_bail_needed:,}`** 幣（`/bail`）"
                )
            else:
                _out_txt = f"出獄請繳：`{BAIL_COST:,}` 幣（`/bail`）"
            emb.add_field(
                name="入獄／出獄",
                value="通緝歸零、搶劫紀錄清空。\n" + _debt_txt + _out_txt,
                inline=False,
            )
            await interaction_send(interaction, embed=emb)
            return

        last_five_total, rob_count, rob_history = get_last_five_robs_total(criminal_id)
        rob_detail = ""
        if rob_history:
            rob_detail = "\n**最近搶劫紀錄：**\n"
            for i, rob in enumerate(rob_history, 1):
                if isinstance(rob, dict):
                    rob_detail += f"{i}. `{int(rob.get('amount',0)):,}` 幣\n"

        emb = discord.Embed(
            title="❌ 追捕失敗",
            description=f"🔪 {criminal_user.mention} 逃過了 🚔 {interaction.user.mention} 的追捕",
            color=0xED4245,
        )
        emb.add_field(name="通緝星級", value="⭐" * wanted_stars, inline=True)
        emb.add_field(name="本次追捕成功率", value=f"`{capture_chance}%`", inline=True)
        emb.add_field(
            name="追捕費用（已扣）",
            value=f"`{COP_HUNT_FEE:,}` 東雲幣",
            inline=True,
        )
        emb.add_field(
            name="若成功可獲（最近五次搶劫總額）",
            value=f"`{last_five_total:,}` 東雲幣（{rob_count} 筆）",
            inline=False,
        )
        if rob_detail:
            emb.add_field(name="搶劫紀錄", value=rob_detail[:1000], inline=False)
        emb.add_field(name="規則", value=hunt_rule, inline=False)
        if wanted_stars >= 5:
            emb.set_footer(text="對方若再次搶劫成功，你可再追捕一次。")
        else:
            emb.set_footer(text="對方通緝升星後，你可再嘗試追捕。")
        await interaction_send(interaction, embed=emb)


    @bot.tree.command(
        name="wanted_buyout",
        description=f"[搶匪] 支付 {WANTED_BUYOUT_COST:,} 東雲幣消除通緝並清空最近搶劫紀錄（24 小時僅能一次）",
    )
    async def wanted_buyout_slash(interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("請在伺服器頻道使用。", ephemeral=True)
        # 先以個人可見 defer，避免冷卻/錯誤提示被公開。
        await interaction_defer_if_needed(interaction, ephemeral=True)
        now = now_tw_naive()
        result = await wanted_buyout_sync_async(interaction.user.id, now)
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "not_found":
                return await interaction_send(interaction, "找不到帳號資料。", ephemeral=True)
            if reason == "not_criminal":
                return await interaction_send(interaction, "❌ 僅**搶匪**可使用此指令。", ephemeral=True)
            if reason == "in_prison":
                return await interaction_send(interaction, "❌ 你在監獄中，無法消除通緝。", ephemeral=True)
            if reason == "no_stars":
                return await interaction_send(interaction, "ℹ️ 你目前沒有通緝星。", ephemeral=True)
            if reason == "insufficient":
                bal = int(result.get("balance") or 0)
                return await interaction_send(
                    interaction,
                    f"❌ 需要 `{WANTED_BUYOUT_COST:,}` 東雲幣，你的餘額不足（目前 `{bal:,}`）。",
                    ephemeral=True,
                )
            if reason == "cooldown":
                ts = tw_naive_to_discord_ts(result["next_dt"])
                return await interaction_send(
                    interaction,
                    f"⏳ 通緝買斷冷卻中，下次可於 <t:{ts}:F>（<t:{ts}:R>）再使用。",
                    ephemeral=True,
                )
            return await interaction_send(interaction, "扣款失敗（餘額不足）。", ephemeral=True)
        _invalidate_wanted_caches(interaction.user.id)
        new_bal = int(result["new_balance"])
        stars_was = int(result["stars_was"])
        cost = int(WANTED_BUYOUT_COST)
        emb = discord.Embed(
            title="✅ 通緝買斷成功（頻道公告）",
            description=(
                f"{interaction.user.mention} 支付 **`{cost:,}`** 東雲幣，"
                f"原通緝 **{stars_was}** 星已消除，追捕計數已歸零，**最近搶劫紀錄已清空**。"
            ),
            color=0x57F287,
        )
        emb.add_field(name="目前餘額", value=f"`{new_bal:,}` 東雲幣", inline=False)
        _am = discord.AllowedMentions(users=[discord.Object(id=interaction.user.id)])
        await interaction_send(interaction, embed=emb, ephemeral=False, allowed_mentions=_am)


    @bot.tree.command(name="good_citizen", description="良民證：支付 5,000 萬啟用防搶；再支付 5,000 萬解除（兩者皆 24h 冷卻）")
    async def good_citizen_slash(interaction: discord.Interaction):
        now = now_tw_naive()
        result = await toggle_good_citizen_sync_async(interaction.user.id, now)
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "not_found":
                return await interaction.response.send_message("找不到帳號資料。", ephemeral=True)
            if reason == "not_civilian":
                return await interaction.response.send_message(
                    "❌ 良民證僅限 **平民** 使用；請先用 `/role_choose` 切換為平民。",
                    ephemeral=True,
                )
            if reason == "broken_lock":
                ts = tw_naive_to_discord_ts(result["until"])
                return await interaction.response.send_message(
                    f"❌ 你的良民證已被摧毀，需等到 <t:{ts}:F>（<t:{ts}:R>）後才能再次啟用。",
                    ephemeral=True,
                )
            if reason == "cooldown":
                ts = tw_naive_to_discord_ts(result["next_dt"])
                return await interaction.response.send_message(
                    f"⏳ 良民證冷卻中，下次可於 <t:{ts}:F>（<t:{ts}:R>）再操作。",
                    ephemeral=True,
                )
            if reason == "insufficient":
                bal = int(result.get("balance") or 0)
                return await interaction.response.send_message(
                    f"❌ 需要 `{GOOD_CITIZEN_CERT_COST:,}` 東雲幣，你的餘額不足（目前 `{bal:,}`）。",
                    ephemeral=True,
                )
            if reason == "deduct_failed":
                return await interaction.response.send_message("扣款失敗（餘額不足）。", ephemeral=True)
            return await interaction.response.send_message("❌ 良民證操作失敗，請稍後再試。", ephemeral=True)

        _invalidate_wanted_caches(interaction.user.id)
        next_active = int(result["next_active"])
        new_bal = int(result["new_balance"])
        title = "✅ 良民證已啟用" if next_active else "✅ 良民證已解除"
        status_txt = "已啟用（不可被搶劫）" if next_active else "已解除（可被搶劫）"
        emb = discord.Embed(title=title, color=0x57F287 if next_active else 0xFEE75C)
        emb.add_field(name="本次花費", value=f"`{GOOD_CITIZEN_CERT_COST:,}` 東雲幣", inline=False)
        emb.add_field(name="目前狀態", value=status_txt, inline=False)
        emb.add_field(name="目前餘額", value=f"`{new_bal:,}` 東雲幣", inline=False)
        emb.set_footer(text="啟用與解除皆有 24 小時冷卻")
        await interaction.response.send_message(embed=emb, ephemeral=True)


    @bot.tree.command(name="good_citizen_list", description="查看目前啟用良民證的玩家清單")
    async def good_citizen_list_slash(interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("請在伺服器頻道使用。", ephemeral=True)
        rows = await fetch_good_citizen_rows_sync_async()
        if not rows:
            return await interaction.response.send_message("目前沒有啟用良民證的玩家。", ephemeral=True)
        guild = interaction.guild
        entries: typing.List[typing.Tuple[int, int]] = []
        for uid_str, bal_raw, _last_action in rows:
            try:
                mid = int(uid_str)
            except (TypeError, ValueError):
                continue
            entries.append((mid, int(bal_raw or 0)))
        names = await _resolve_display_names(guild, [mid for mid, _bal in entries])
        lines: typing.List[str] = []
        for mid, bal in entries:
            disp_safe = discord.utils.escape_markdown(names[mid])
            lines.append(f"• {disp_safe}（<@{mid}>）｜餘額 `{bal:,}`")
        if not lines:
            return await interaction.response.send_message("目前沒有啟用良民證的玩家。", ephemeral=True)
        emb = discord.Embed(
            title="🪪 良民證持有者名單",
            description="\n".join(lines)[:3900],
            color=0x57F287,
        )
        emb.set_footer(text=f"共 {len(lines)} 人")
        await interaction.response.send_message(embed=emb, ephemeral=False)


    @bot.tree.command(name="break_citizen", description="摧毀目標良民證（花費 5 億；目標 10 天內無法再取得）")
    @app_commands.describe(member="目標玩家（選人）", user_id="或填使用者 ID／貼提及")
    async def break_citizen_slash(
        interaction: discord.Interaction,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
    ):
        target_user, err = await resolve_slash_target(
            interaction, member, user_id, required=True, in_guild_only=False
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        if target_user.id == interaction.user.id:
            return await interaction.response.send_message("❌ 不能對自己使用。", ephemeral=True)
        now = now_tw_naive()
        result = await break_citizen_sync_async(interaction.user.id, target_user.id, now)
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "insufficient":
                attacker_bal = int(result.get("balance") or 0)
                return await interaction.response.send_message(
                    f"❌ 需要 `{GOOD_CITIZEN_DESTROY_COST:,}` 東雲幣，你的餘額不足（目前 `{attacker_bal:,}`）。",
                    ephemeral=True,
                )
            if reason == "target_not_found":
                return await interaction.response.send_message("找不到目標資料。", ephemeral=True)
            if reason == "target_not_active":
                target_broken_until = result.get("target_broken_until")
                if target_broken_until and now < target_broken_until:
                    ts = tw_naive_to_discord_ts(target_broken_until)
                    return await interaction.response.send_message(
                        f"ℹ️ 目標目前未啟用良民證，且已被封鎖至 <t:{ts}:F>（<t:{ts}:R>）。",
                        ephemeral=True,
                    )
                return await interaction.response.send_message("ℹ️ 目標目前沒有啟用良民證。", ephemeral=True)
            return await interaction.response.send_message("扣款失敗（餘額不足）。", ephemeral=True)
        _invalidate_wanted_caches(target_user.id)
        broken_until = result["broken_until"]
        ts = tw_naive_to_discord_ts(broken_until)
        emb = discord.Embed(
            title="💥 良民證已摧毀",
            description=(
                f"{interaction.user.mention} 花費 **`{GOOD_CITIZEN_DESTROY_COST:,}`** 東雲幣，"
                f"摧毀了 {target_user.mention} 的良民證。"
            ),
            color=0xED4245,
        )
        emb.add_field(
            name="封鎖時間",
            value=f"目標於 <t:{ts}:F>（<t:{ts}:R>）前無法再啟用良民證",
            inline=False,
        )
        _am = discord.AllowedMentions(users=[discord.Object(id=interaction.user.id), discord.Object(id=target_user.id)])
        await interaction.response.send_message(embed=emb, ephemeral=False, allowed_mentions=_am)


    @bot.tree.command(name="wanted_status", description="查看自己的陣營、通緝、監獄狀態與最近搶劫紀錄")
    async def wanted_status_slash(interaction: discord.Interaction):
        row = await fetch_wanted_status_row_sync_async(interaction.user.id)
        if not row:
            return await interaction.response.send_message("找不到資料。", ephemeral=True)
        role, stars, hunted, in_pr, raw_hist, arrests, rev_pend, rev_amt, bail_debt_u, cert_active = row
        role_s = role or "civilian"
        stars_i = int(stars or 0)
        hunted_i = int(hunted or 0)
        in_pr_i = int(in_pr or 0)
        arrests_i = int(arrests or 0)

        role_disp = {"cop": "🚔 警察", "criminal": "🔪 搶匪"}.get(role_s, "👤 平民")
        emb = discord.Embed(title="📋 通緝／監獄狀態", color=0x5865F2)
        emb.add_field(name="陣營", value=role_disp, inline=True)
        emb.add_field(name="通緝星", value=("⭐" * stars_i + "☆" * (5 - stars_i)) if stars_i <= 5 else str(stars_i), inline=True)
        emb.add_field(name="本輪可追捕", value="否（已嘗試）" if hunted_i else "是", inline=True)
        emb.add_field(name="監獄", value="🔒 在押" if in_pr_i else "否", inline=True)
        emb.add_field(name="累計被捕次數", value=str(arrests_i), inline=True)
        if role_s == "civilian":
            emb.add_field(
                name="良民證",
                value="🪪 已啟用（不可被搶）" if int(cert_active or 0) else "未啟用（可被搶）",
                inline=True,
            )
        if int(rev_pend or 0) and (role or "civilian") == "civilian":
            emb.add_field(
                name="加倍搶回",
                value=(
                    "已改為**自動反制**：平民被搶成功後會立即自動判定，不需手動輸入指令。"
                ),
                inline=False,
            )

        hist_lines = ""
        if raw_hist:
            try:
                h = json.loads(raw_hist)
                if isinstance(h, list) and h:
                    for i, item in enumerate(h[-5:], 1):
                        if isinstance(item, dict):
                            hist_lines += f"{i}. `{int(item.get('amount',0)):,}` — {item.get('time','')}\n"
            except Exception:
                hist_lines = "（紀錄格式異常）"
        emb.add_field(
            name="最近搶劫成功紀錄（最多五筆）",
            value=hist_lines[:1000] if hist_lines else "（無）",
            inline=False,
        )
        _bd = int(bail_debt_u or 0)
        _total_out = BAIL_COST + _bd
        emb.set_footer(
            text=(
                f"出獄須繳：基礎 `{BAIL_COST:,}`"
                + (f" + 欠款 `{_bd:,}` = 合計 `{_total_out:,}`" if _bd else "")
                + " 幣｜/bail"
            )
        )
        await interaction.response.send_message(embed=emb, ephemeral=True)


    @bot.tree.command(name="wanted_list", description="列出目前通緝中玩家（不含 0 星），並顯示可否被追捕")
    async def wanted_list_slash(interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("請在伺服器頻道使用。", ephemeral=True)
        rows = await fetch_wanted_list_rows_sync_async()
        if not rows:
            return await interaction.response.send_message(
                "目前沒有通緝中的玩家（僅顯示通緝星 1～5 星）。",
                ephemeral=True,
            )
        guild = interaction.guild
        entries: typing.List[typing.Tuple[int, int, int, int, typing.Any]] = []
        for uid_str, stars, hunted, in_pr, raw_hist in rows:
            stars_i = int(stars or 0)
            if stars_i <= 0:
                continue
            try:
                mid = int(uid_str)
            except (TypeError, ValueError):
                continue
            entries.append((mid, stars_i, int(hunted or 0), int(in_pr or 0), raw_hist))
        names = await _resolve_display_names(guild, [e[0] for e in entries])
        lines: typing.List[str] = []
        for mid, stars_i, hunted_i, in_pr_i, raw_hist in entries:
            disp_safe = discord.utils.escape_markdown(names[mid])
            star_s = "⭐" * min(stars_i, 5)
            bounty, bounty_count = rob_history_total_from_raw(raw_hist)
            bounty_txt = f"`{bounty:,}` 東雲幣（{bounty_count} 筆）"
            if in_pr_i:
                hunt_txt = "在獄中（無法被追捕）"
            elif hunted_i:
                hunt_txt = "本輪已追捕（待升星或搶匪再搶成功後才可再追）"
            else:
                hunt_txt = "**可追捕**"
            lines.append(f"• {disp_safe}（<@{mid}>）｜{star_s}｜可獲獎金 {bounty_txt}｜{hunt_txt}")
        if not lines:
            return await interaction.response.send_message(
                "目前沒有通緝中的玩家（僅顯示通緝星 1～5 星）。",
                ephemeral=True,
            )
        body = "\n".join(lines)[:3900]
        emb = discord.Embed(
            title="📣 通緝名單",
            description=body,
            color=0xED4245,
        )
        emb.set_footer(text="警察請用 /cop_hunt 指定對象｜0 星不會出現在此清單")
        await interaction.response.send_message(embed=emb, ephemeral=False)


    @bot.tree.command(
        name="counter_rob",
        description="（相容保留）平民反制已改為被搶成功後自動結算",
    )
    async def counter_rob_slash(interaction: discord.Interaction):
        return await interaction.response.send_message(
            "ℹ️ 平民反制已改為被搶成功後**自動觸發**，不需手動使用 `/counter_rob`。",
            ephemeral=True,
        )


    @bot.tree.command(name="bail", description=f"繳納假釋金（基礎 {BAIL_COST:,} + 累計欠款）出獄")
    async def bail_slash(interaction: discord.Interaction):
        now = now_tw_naive()
        result = await pay_bail_sync_async(interaction.user.id, now)
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "not_in_prison":
                return await interaction.response.send_message("你不在監獄裡。", ephemeral=True)
            if reason == "insufficient":
                debt = int(result.get("debt") or 0)
                total_bail = int(result.get("total_bail") or BAIL_COST)
                return await interaction.response.send_message(
                    f"假釋須繳 **基礎 `{BAIL_COST:,}`**"
                    + (f" + **欠款 `{debt:,}`**" if debt else "")
                    + f" = **合計 `{total_bail:,}`** 東雲幣，你的餘額不足。",
                    ephemeral=True,
                )
            return await interaction.response.send_message("扣款失敗（餘額不足）。", ephemeral=True)
        _invalidate_wanted_caches(interaction.user.id)
        debt = int(result["debt"])
        total_bail = int(result["total_bail"])
        await interaction.response.send_message(
            f"✅ 已繳納 **`{total_bail:,}`** 東雲幣（基礎 `{BAIL_COST:,}`"
            + (f" + 清償欠款 `{debt:,}`" if debt else "")
            + "），你已出獄。",
            ephemeral=True,
        )
