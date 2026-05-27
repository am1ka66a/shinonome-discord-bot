import asyncio
import json
import time
import typing

import discord
from discord import app_commands


def register_admin_commands(bot: discord.Client, ctx: typing.Dict[str, typing.Any]) -> None:
    allowed_host_ids = ctx["ALLOWED_HOST_IDS"]
    max_level = int(ctx["MAX_LEVEL"])
    level_mile_tiers = ctx["LEVEL_MILE_TIERS"]
    feature_toggles = ctx["FEATURE_TOGGLES"]

    resolve_slash_target = ctx["resolve_slash_target"]
    ensure_user_exists = ctx["ensure_user_exists"]
    ensure_user_exists_async = ctx["ensure_user_exists_async"]
    get_level_stats = ctx["get_level_stats"]
    exp_required_for_level = ctx["exp_required_for_level"]
    process_level_ups = ctx["process_level_ups"]
    get_db_connection = ctx["get_db_connection"]
    log_transaction = ctx["log_transaction"]
    credit_balance_with_log_async = ctx["credit_balance_with_log_async"]
    try_deduct_balance_async = ctx["try_deduct_balance_async"]
    calc_level_from_exp = ctx["calc_level_from_exp"]
    tw_tz = ctx["TW_TZ"]
    logger = ctx["logger"]

    get_is_event_active = ctx["get_is_event_active"]
    set_is_event_active = ctx["set_is_event_active"]
    get_share_enabled = ctx["get_share_enabled"]
    set_share_enabled = ctx["set_share_enabled"]

    def is_slash_host(interaction: discord.Interaction):
        return interaction.user.id in allowed_host_ids

    def chunk_text_lines(lines: typing.List[str], max_len: int = 1900) -> typing.List[str]:
        chunks: typing.List[str] = []
        buf: typing.List[str] = []
        size = 0
        for line in lines:
            add = len(line) + (1 if buf else 0)
            if buf and size + add > max_len:
                chunks.append("\n".join(buf))
                buf = [line]
                size = len(line)
            else:
                if buf:
                    size += 1
                buf.append(line)
                size += len(line)
        if buf:
            chunks.append("\n".join(buf))
        return chunks

    def grant_mass_rewards_sync(coins: int, exp: int, note_text: str) -> typing.Dict[str, int]:
        """批次發放：幣走 set-based SQL；EXP 走 executemany 批次更新並同步 level。"""
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, COALESCE(exp,0), COALESCE(level,1) FROM users")
        rows = c.fetchall()
        target_rows = [(str(r[0]), int(r[1] or 0), int(r[2] or 1)) for r in rows if r and r[0] is not None]
        target_count = len(target_rows)
        if target_count <= 0:
            conn.close()
            return {"target_count": 0, "coins_changed": 0, "exp_changed": 0, "leveled_up_users": 0}

        if coins > 0:
            reason = "管理員活動發幣"
            if note_text:
                reason += f"（備註: {note_text}）"
            c.execute("UPDATE users SET balance=balance+%s", (int(coins),))
            c.execute(
                "INSERT INTO logs (user_id, amount, reason) "
                "SELECT user_id, %s, %s FROM users",
                (int(coins), reason),
            )
            # 全期總金流鏡像：此批次不綁 source_log_id，避免逐筆回填造成額外成本。
            c.execute(
                "INSERT INTO casino_logs (user_id, amount, reason) "
                "SELECT user_id, %s, %s FROM users",
                (int(coins), reason),
            )

        leveled_up_users = 0
        if exp > 0:
            updates: typing.List[typing.Tuple[int, int, str]] = []
            exp_add = int(exp)
            for uid, old_exp, old_lv in target_rows:
                new_exp = old_exp + exp_add
                calc_lv, _cur, _need = calc_level_from_exp(new_exp)
                new_lv = max(old_lv, int(calc_lv))
                if new_lv > old_lv:
                    leveled_up_users += 1
                updates.append((new_exp, new_lv, uid))
            if updates:
                c.executemany("UPDATE users SET exp=%s, level=%s WHERE user_id=%s", updates)

        conn.commit()
        conn.close()
        return {
            "target_count": target_count,
            "coins_changed": int(coins),
            "exp_changed": int(exp),
            "leveled_up_users": leveled_up_users,
        }

    @bot.tree.command(name="setlevel", description="[管理員] 直接設定玩家等級")
    @app_commands.describe(
        level="要設定到幾等（1~100）",
        member="玩家（選人）",
        user_id="或填使用者 ID／貼提及",
    )
    async def setlevel_slash(
        interaction: discord.Interaction,
        level: int,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
    ):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        if level < 1 or level > max_level:
            return await interaction.response.send_message(f"等級需介於 1~{max_level}。", ephemeral=True)
        m_user, err = await resolve_slash_target(
            interaction, member, user_id, required=True, in_guild_only=False
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        member = m_user

        ensure_user_exists(member.id, 0)
        lv_row = get_level_stats(member.id)
        old_exp = int(lv_row[0] or 0) if lv_row else 0
        old_level = int(lv_row[1] or 1) if lv_row else 1
        target_exp = exp_required_for_level(level)

        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET level=%s, exp=%s WHERE user_id=%s", (int(level), int(target_exp), str(member.id)))
        conn.commit()
        conn.close()

        milestone_note = ""
        if level > old_level:
            crossed = [m for m in level_mile_tiers if old_level < m <= level]
            if crossed:
                await process_level_ups(member, old_level, level)
                milestone_note = f"\n🎯 已同步觸發里程碑流程：Lv.{', '.join(map(str, crossed))}"

        await interaction.response.send_message(
            f"✅ 已將 {member.mention} 設定為 **Lv.{level}**\n"
            f"原本：Lv.{old_level} / EXP `{old_exp:,}`\n"
            f"現在：Lv.{level} / EXP `{target_exp:,}`"
            f"{milestone_note}"
        )

    @bot.tree.command(name="give", description="[管理員] 發放東雲幣給玩家")
    @app_commands.describe(
        amount="發放數量",
        member="玩家（選人）",
        user_id="或填使用者 ID／貼提及",
        note="備註（選填）",
    )
    async def give_slash(
        interaction: discord.Interaction,
        amount: int,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
        note: str = "",
    ):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        if amount <= 0:
            return await interaction.response.send_message("數量必須大於 0", ephemeral=True)
        m_user, err = await resolve_slash_target(
            interaction, member, user_id, required=True, in_guild_only=False
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        member = m_user
        ensure_user_exists(member.id, 0)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE user_id=%s", (str(member.id),))
        before_row = c.fetchone()
        before_bal = int((before_row[0] if before_row else 0) or 0)
        c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, str(member.id)))
        after_bal = before_bal + amount
        conn.commit()
        conn.close()
        note_text = (note or "").strip()
        if len(note_text) > 100:
            note_text = note_text[:100]
        reason = f"管理員發放（備註: {note_text}）" if note_text else "管理員發放"
        log_transaction(member.id, amount, reason)
        embed = discord.Embed(title="✅ 發放成功", color=discord.Color.green())
        embed.add_field(name="對象", value=member.mention, inline=False)
        embed.add_field(name="發放金額", value=f"`{amount:,}` 東雲幣", inline=False)
        embed.add_field(name="餘額變化", value=f"`{before_bal:,}` → `{after_bal:,}`", inline=False)
        embed.add_field(name="備註", value=note_text if note_text else "（無）", inline=False)
        embed.set_footer(text=f"操作人：{interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="take", description="[管理員] 扣除玩家東雲幣")
    @app_commands.describe(
        amount="扣除數量",
        member="玩家（選人）",
        user_id="或填使用者 ID／貼提及",
        note="備註（選填）",
    )
    async def take_slash(
        interaction: discord.Interaction,
        amount: int,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
        note: str = "",
    ):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        if amount <= 0:
            return await interaction.response.send_message("數量必須大於 0", ephemeral=True)
        m_user, err = await resolve_slash_target(
            interaction, member, user_id, required=True, in_guild_only=False
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        member = m_user
        ensure_user_exists(member.id, 0)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE user_id=%s", (str(member.id),))
        before_row = c.fetchone()
        before_bal = int((before_row[0] if before_row else 0) or 0)
        c.execute("UPDATE users SET balance=GREATEST(0, balance-%s) WHERE user_id=%s", (amount, str(member.id)))
        after_bal = max(0, before_bal - amount)
        conn.commit()
        conn.close()
        note_text = (note or "").strip()
        if len(note_text) > 100:
            note_text = note_text[:100]
        reason = f"管理員扣除（備註: {note_text}）" if note_text else "管理員扣除"
        log_transaction(member.id, -amount, reason)
        embed = discord.Embed(title="✅ 扣款成功", color=discord.Color.green())
        embed.add_field(name="對象", value=member.mention, inline=False)
        embed.add_field(name="扣除金額", value=f"`{amount:,}` 東雲幣", inline=False)
        embed.add_field(name="餘額變化", value=f"`{before_bal:,}` → `{after_bal:,}`", inline=False)
        embed.add_field(name="備註", value=note_text if note_text else "（無）", inline=False)
        embed.set_footer(text=f"操作人：{interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="ban", description="[管理員] 將玩家加入黑名單")
    @app_commands.describe(member="玩家（選人）", user_id="或填使用者 ID／貼提及")
    async def ban_slash(
        interaction: discord.Interaction,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
    ):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        m_user, err = await resolve_slash_target(
            interaction, member, user_id, required=True, in_guild_only=False
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        member = m_user
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT IGNORE INTO blacklist (user_id) VALUES (%s)", (str(member.id),))
        conn.commit()
        conn.close()
        await interaction.response.send_message(f"{member.mention} 已加入黑名單。")

    @bot.tree.command(name="unban", description="[管理員] 將玩家移出黑名單")
    @app_commands.describe(member="玩家（選人，未必在伺服器）", user_id="或填使用者 ID／貼提及")
    async def unban_slash(
        interaction: discord.Interaction,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
    ):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        m_user, err = await resolve_slash_target(
            interaction, member, user_id, required=True, in_guild_only=False
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        member = m_user
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM blacklist WHERE user_id=%s", (str(member.id),))
        conn.commit()
        conn.close()
        await interaction.response.send_message(f"{member.mention} 已解除黑名單。")

    @bot.tree.command(name="resetall_zero", description="[管理員] 全伺服器餘額清零")
    async def resetall_zero_slash(interaction: discord.Interaction):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET balance=0")
        conn.commit()
        conn.close()
        await interaction.response.send_message("💥 全伺服器帳戶餘額已清零。")

    @bot.tree.command(name="resetall_default", description="[管理員] 全伺服器重置為 50,000")
    async def resetall_default_slash(interaction: discord.Interaction):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET balance=50000, rescue_count=0, total_games=0, wins=0, total_profit=0")
        conn.commit()
        conn.close()
        await interaction.response.send_message("🔄 全服已重置為 50,000，並重置統計。")

    @bot.tree.command(name="lock", description="[管理員] 開關賭場營業狀態")
    async def lock_slash(interaction: discord.Interaction):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        set_is_event_active(not bool(get_is_event_active()))
        await interaction.response.send_message(f"賭場狀態已切換：`{get_is_event_active()}`")

    @bot.tree.command(name="adminhelp", description="[管理員] 查看管理指令清單")
    async def adminhelp_slash(interaction: discord.Interaction):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        help_text = """**👑 管理員 Slash 指令清單（主機白名單）**
一般玩家說明請用：`/help`

`/setlevel` - 直接設定玩家等級（1~100）
`/give` - 發放東雲幣給玩家
`/take` - 扣除玩家東雲幣（最低到 0）
`/ban` - 將玩家加入黑名單
`/unban` - 將玩家移出黑名單
`/lock` - 開關賭場營業狀態
`/resetall_zero` - 全服餘額清零（含統計重置）
`/resetall_default` - 全服重置為 50,000（含統計重置）
`/say` - 指定機器人到頻道發言
`/share_stats` - 查看賭場回收分潤統計
`/admin_balance_set` - 直接設定玩家餘額
`/admin_user_flags` - 查看玩家關鍵狀態（通緝/監獄/良民證/黑名單等）
`/admin_unjail` - 強制釋放玩家（可選清債/清通緝）
`/admin_revert_tx` - 依流水 ID 進行反向沖正
`/admin_logs` - 查詢含流水 ID 的最近帳務紀錄
`/admin_reward_grant` - 全體活動獎勵（可同時發幣 + 發 EXP）
`/admin_feature_toggle` - 線上開關功能（rob/bj/duel/redpacket/share）"""
        await interaction.response.send_message(help_text, ephemeral=True)

    @bot.tree.command(name="admin_reward_grant", description="[管理員] 全體發放活動獎勵（幣 + EXP）")
    @app_commands.describe(
        coins="要發放的東雲幣（可為 0）",
        exp="要發放的 EXP（可為 0）",
        note="備註（選填）",
    )
    async def admin_reward_grant_slash(
        interaction: discord.Interaction,
        coins: int = 0,
        exp: int = 0,
        note: str = "",
    ):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        if coins < 0 or exp < 0:
            return await interaction.response.send_message("coins / exp 不可為負數。", ephemeral=True)
        if coins == 0 and exp == 0:
            return await interaction.response.send_message("請至少發放一種獎勵（coins 或 exp）。", ephemeral=True)
        note_text = (note or "").strip()[:100]
        await interaction.response.defer(ephemeral=True, thinking=True)
        t0 = time.perf_counter()
        result = await asyncio.to_thread(grant_mass_rewards_sync, int(coins), int(exp), note_text)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        target_count = int(result.get("target_count") or 0)
        leveled_up_users = int(result.get("leveled_up_users") or 0)
        if target_count <= 0:
            return await interaction.followup.send("目前沒有可發放的玩家資料。", ephemeral=True)

        reward_lines: typing.List[str] = []
        if coins > 0:
            reward_lines.append(f"💰 東雲幣 `+{int(coins):,}`（每人）")

        if exp > 0:
            reward_lines.append(f"✨ EXP `+{int(exp):,}`（每人）")
            reward_lines.append(f"📈 升級人數 `+{leveled_up_users:,}`（僅資料更新，不逐一公告）")

        title = "✅ 獎勵發放完成"
        embed = discord.Embed(title=title, color=discord.Color.green())
        embed.add_field(name="對象", value=f"全體玩家（共 `{target_count:,}` 人）", inline=False)
        embed.add_field(name="內容", value="\n".join(reward_lines), inline=False)
        if exp > 0:
            embed.add_field(name="等級公告", value="全體發放不逐一推播里程碑公告（僅更新資料）", inline=False)
        embed.add_field(name="耗時", value=f"`{elapsed_ms:,}` ms", inline=False)
        embed.add_field(name="備註", value=note_text if note_text else "（無）", inline=False)
        embed.set_footer(text=f"操作人：{interaction.user.display_name}")
        logger.info(
            "admin_reward_grant: operator=%s target_count=%s coins=%s exp=%s leveled_up=%s elapsed_ms=%s",
            interaction.user.id,
            target_count,
            int(coins),
            int(exp),
            leveled_up_users,
            elapsed_ms,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(name="admin_balance_set", description="[管理員] 直接設定玩家餘額")
    @app_commands.describe(
        amount="要設定成的餘額（不可小於 0）",
        member="玩家（選人）",
        user_id="或填使用者 ID／貼提及",
        note="備註（選填）",
    )
    async def admin_balance_set_slash(
        interaction: discord.Interaction,
        amount: int,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
        note: str = "",
    ):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        if amount < 0:
            return await interaction.response.send_message("餘額不可小於 0。", ephemeral=True)
        target_user, err = await resolve_slash_target(
            interaction, member, user_id, required=True, in_guild_only=False
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        await ensure_user_exists_async(target_user.id, 0)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COALESCE(balance,0) FROM users WHERE user_id=%s", (str(target_user.id),))
        row = c.fetchone()
        before_bal = int((row[0] if row else 0) or 0)
        c.execute("UPDATE users SET balance=%s WHERE user_id=%s", (int(amount), str(target_user.id)))
        conn.commit()
        conn.close()

        delta = int(amount) - before_bal
        note_text = (note or "").strip()[:100]
        reason = f"管理員設定餘額（{before_bal:,}→{int(amount):,}）"
        if note_text:
            reason += f"（備註: {note_text}）"
        if delta != 0:
            log_transaction(target_user.id, delta, reason)

        embed = discord.Embed(title="✅ 餘額已設定", color=discord.Color.green())
        embed.add_field(name="對象", value=target_user.mention, inline=False)
        embed.add_field(name="原餘額", value=f"`{before_bal:,}`", inline=True)
        embed.add_field(name="新餘額", value=f"`{int(amount):,}`", inline=True)
        embed.add_field(name="變動", value=f"`{delta:+,}`", inline=True)
        embed.add_field(name="備註", value=note_text if note_text else "（無）", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="admin_user_flags", description="[管理員] 查看玩家關鍵狀態")
    @app_commands.describe(member="玩家（選人）", user_id="或填使用者 ID／貼提及")
    async def admin_user_flags_slash(
        interaction: discord.Interaction,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
    ):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        target_user, err = await resolve_slash_target(
            interaction, member, user_id, required=True, in_guild_only=False
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "SELECT COALESCE(balance,0), COALESCE(role,'civilian'), COALESCE(wanted_stars,0), COALESCE(wanted_hunted_count,0), "
            "COALESCE(in_prison,0), prison_start, COALESCE(bail_debt,0), COALESCE(good_citizen_cert_active,0), "
            "good_citizen_cert_broken_until, last_five_robs "
            "FROM users WHERE user_id=%s",
            (str(target_user.id),),
        )
        row = c.fetchone()
        c.execute("SELECT 1 FROM blacklist WHERE user_id=%s", (str(target_user.id),))
        blacklisted = c.fetchone() is not None
        conn.close()
        if not row:
            return await interaction.response.send_message("找不到玩家資料。", ephemeral=True)

        bal, role, stars, hunted, in_pr, prison_start, bail_debt, cert_active, cert_broken_until, raw_hist = row
        role_disp = {"cop": "🚔 警察", "criminal": "🔪 搶匪"}.get(str(role or "civilian"), "👤 平民")
        hist_count = 0
        hist_total = 0
        if raw_hist:
            try:
                h = json.loads(raw_hist)
                if isinstance(h, list):
                    hist_count = len(h)
                    hist_total = sum(int((it or {}).get("amount", 0) or 0) for it in h if isinstance(it, dict))
            except Exception:
                pass

        emb = discord.Embed(title="🛠️ 玩家狀態總覽", color=0x5865F2)
        emb.add_field(name="對象", value=f"{target_user.mention} (`{target_user.id}`)", inline=False)
        emb.add_field(name="餘額", value=f"`{int(bal or 0):,}`", inline=True)
        emb.add_field(name="身分", value=role_disp, inline=True)
        emb.add_field(name="黑名單", value="是" if blacklisted else "否", inline=True)
        emb.add_field(name="通緝", value=f"{int(stars or 0)}★｜本輪已追捕: {'是' if int(hunted or 0) else '否'}", inline=True)
        emb.add_field(name="監獄", value="在押" if int(in_pr or 0) else "否", inline=True)
        emb.add_field(name="假釋欠款", value=f"`{int(bail_debt or 0):,}`", inline=True)
        broken_text = ""
        if cert_broken_until:
            try:
                broken_ts = int(cert_broken_until.replace(tzinfo=tw_tz).timestamp())
                broken_text = f"\n禁用至: <t:{broken_ts}:F>"
            except Exception:
                broken_text = f"\n禁用至: {cert_broken_until}"
        emb.add_field(
            name="良民證",
            value=("啟用中" if int(cert_active or 0) else "未啟用") + broken_text,
            inline=False,
        )
        emb.add_field(name="最近搶劫紀錄", value=f"{hist_count} 筆｜總額 `{hist_total:,}`", inline=False)
        if prison_start:
            emb.set_footer(text=f"prison_start: {prison_start}")
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @bot.tree.command(name="admin_unjail", description="[管理員] 強制釋放玩家")
    @app_commands.describe(
        member="玩家（選人）",
        user_id="或填使用者 ID／貼提及",
        clear_debt="是否一併清除假釋欠款",
        clear_wanted="是否一併清除通緝星",
    )
    async def admin_unjail_slash(
        interaction: discord.Interaction,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
        clear_debt: bool = False,
        clear_wanted: bool = True,
    ):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        target_user, err = await resolve_slash_target(
            interaction, member, user_id, required=True, in_guild_only=False
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COALESCE(in_prison,0), COALESCE(bail_debt,0), COALESCE(wanted_stars,0) FROM users WHERE user_id=%s", (str(target_user.id),))
        row = c.fetchone()
        if not row:
            conn.close()
            return await interaction.response.send_message("找不到玩家資料。", ephemeral=True)
        was_in_prison, debt_before, stars_before = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
        if not was_in_prison:
            conn.close()
            return await interaction.response.send_message("該玩家目前不在監獄。", ephemeral=True)
        if clear_debt and clear_wanted:
            c.execute(
                "UPDATE users SET in_prison=0, prison_start=NULL, bail_debt=0, wanted_stars=0, wanted_hunted_count=0 WHERE user_id=%s",
                (str(target_user.id),),
            )
        elif clear_debt:
            c.execute(
                "UPDATE users SET in_prison=0, prison_start=NULL, bail_debt=0 WHERE user_id=%s",
                (str(target_user.id),),
            )
        elif clear_wanted:
            c.execute(
                "UPDATE users SET in_prison=0, prison_start=NULL, wanted_stars=0, wanted_hunted_count=0 WHERE user_id=%s",
                (str(target_user.id),),
            )
        else:
            c.execute(
                "UPDATE users SET in_prison=0, prison_start=NULL WHERE user_id=%s",
                (str(target_user.id),),
            )
        conn.commit()
        conn.close()
        await interaction.response.send_message(
            f"✅ 已釋放 {target_user.mention}。\n"
            f"清債: {'是' if clear_debt else '否'}（原 `{debt_before:,}`）｜"
            f"清通緝: {'是' if clear_wanted else '否'}（原 {stars_before}★）",
            ephemeral=True,
        )

    @bot.tree.command(name="admin_revert_tx", description="[管理員] 依流水 ID 做反向沖正")
    @app_commands.describe(tx_id="logs 的流水 ID", note="備註（選填）")
    async def admin_revert_tx_slash(interaction: discord.Interaction, tx_id: int, note: str = ""):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        if tx_id <= 0:
            return await interaction.response.send_message("tx_id 必須大於 0。", ephemeral=True)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, user_id, amount, reason FROM logs WHERE id=%s", (int(tx_id),))
        row = c.fetchone()
        if not row:
            conn.close()
            return await interaction.response.send_message("找不到該流水 ID。", ephemeral=True)
        _id, uid, amt, rs = int(row[0]), str(row[1]), int(row[2] or 0), str(row[3] or "")
        c.execute(
            "SELECT 1 FROM logs WHERE user_id=%s AND reason LIKE %s LIMIT 1",
            (uid, f"管理員沖正 tx#{_id}%"),
        )
        already = c.fetchone() is not None
        conn.close()
        if already:
            return await interaction.response.send_message("這筆流水看起來已經沖正過了。", ephemeral=True)
        reverse = -amt
        if reverse > 0:
            await credit_balance_with_log_async(uid, reverse, f"管理員沖正 tx#{_id}（原: {rs}）")
        else:
            ok = await try_deduct_balance_async(uid, abs(reverse), f"管理員沖正 tx#{_id}（原: {rs}）")
            if not ok:
                return await interaction.response.send_message(
                    "沖正需要扣款，但目標餘額不足，已中止。可先手動調整餘額後再沖正。",
                    ephemeral=True,
                )
        note_text = (note or "").strip()
        await interaction.response.send_message(
            f"✅ 已沖正 tx#{_id}\n對象：<@{int(uid)}>\n原金額：`{amt:+,}` → 沖正：`{reverse:+,}`"
            + (f"\n備註：{note_text}" if note_text else ""),
            ephemeral=True,
        )

    @bot.tree.command(name="admin_logs", description="[管理員] 查詢含流水 ID 的最近帳務紀錄")
    @app_commands.describe(
        member="要篩選的玩家（選填）",
        user_id="或填使用者 ID／貼提及（選填）",
        limit="顯示筆數（1~50，預設 20）",
    )
    async def admin_logs_slash(
        interaction: discord.Interaction,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
        limit: int = 20,
    ):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        limit = max(1, min(50, int(limit)))
        target_user, err = await resolve_slash_target(
            interaction, member, user_id, required=False, in_guild_only=False
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)

        conn = get_db_connection()
        c = conn.cursor()
        if target_user is not None:
            c.execute(
                "SELECT id, user_id, amount, reason, created_at FROM logs WHERE user_id=%s ORDER BY id DESC LIMIT %s",
                (str(target_user.id), limit),
            )
        else:
            c.execute(
                "SELECT id, user_id, amount, reason, created_at FROM logs ORDER BY id DESC LIMIT %s",
                (limit,),
            )
        rows = c.fetchall()
        conn.close()
        if not rows:
            return await interaction.response.send_message("查無流水資料。", ephemeral=True)

        lines: typing.List[str] = []
        for rid, uid, amt, reason, created_at in rows:
            try:
                t = created_at.strftime("%m/%d %H:%M:%S") if created_at else "N/A"
            except Exception:
                t = "N/A"
            lines.append(
                f"`#{int(rid)}` [{t}] <@{int(uid)}> `{int(amt):+,}` | {str(reason or '')[:80]}"
            )

        header = (
            f"📒 最近流水（{len(rows)} 筆）"
            + (f"｜對象：<@{target_user.id}>" if target_user is not None else "")
        )
        chunks = chunk_text_lines([header, ""] + lines, 1900)
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for ch in chunks[1:]:
            await interaction.followup.send(ch, ephemeral=True)

    @bot.tree.command(name="admin_feature_toggle", description="[管理員] 線上開關功能（免重啟）")
    @app_commands.describe(feature="功能名稱", enabled="是否啟用")
    @app_commands.choices(
        feature=[
            app_commands.Choice(name="rob", value="rob"),
            app_commands.Choice(name="bj", value="bj"),
            app_commands.Choice(name="duel", value="duel"),
            app_commands.Choice(name="redpacket", value="redpacket"),
            app_commands.Choice(name="share", value="share"),
        ]
    )
    async def admin_feature_toggle_slash(interaction: discord.Interaction, feature: str, enabled: bool):
        if not is_slash_host(interaction):
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        feature = (feature or "").strip().lower()
        if feature == "share":
            set_share_enabled(bool(enabled))
        else:
            if feature not in feature_toggles:
                return await interaction.response.send_message("不支援的功能名稱。", ephemeral=True)
            feature_toggles[feature] = bool(enabled)
        status = "啟用" if enabled else "停用"
        state_lines = [
            f"rob: {'on' if feature_toggles.get('rob', True) else 'off'}",
            f"bj: {'on' if feature_toggles.get('bj', True) else 'off'}",
            f"duel: {'on' if feature_toggles.get('duel', True) else 'off'}",
            f"redpacket: {'on' if feature_toggles.get('redpacket', True) else 'off'}",
            f"share: {'on' if get_share_enabled() else 'off'}",
        ]
        await interaction.response.send_message(
            f"✅ 功能 `{feature}` 已設為 **{status}**\n目前狀態：\n" + "\n".join(state_lines),
            ephemeral=True,
        )
