import typing

import discord
from discord import app_commands

from bot_modules.runtime import snapshot_cache

LEADERBOARD_POOL = 400
LEADERBOARD_RANK_SCAN = 800


async def _fetch_balance_leaderboard_core(user_id):
    rows = await snapshot_cache.get_balance_leaderboard_rows_cached()
    uid = str(user_id)
    my_bal = 0
    global_rank = len(rows) + 1
    for idx, (rid, bal) in enumerate(rows):
        if str(rid) == uid:
            my_bal = int(bal or 0)
            global_rank = idx + 1
            break
    pool = rows[:LEADERBOARD_POOL]
    richer = [(r[0],) for r in rows[: max(0, min(global_rank - 1, LEADERBOARD_RANK_SCAN))]]
    top10 = rows[:10]
    return my_bal, pool, richer, top10, global_rank


async def _fetch_level_leaderboard_core(user_id):
    rows = await snapshot_cache.get_level_leaderboard_rows_cached()
    uid = str(user_id)
    my_level, my_exp = 1, 0
    global_rank = len(rows) + 1
    for idx, (rid, lv, exp) in enumerate(rows):
        if str(rid) == uid:
            my_level, my_exp = int(lv or 1), int(exp or 0)
            global_rank = idx + 1
            break
    pool = rows[:LEADERBOARD_POOL]
    richer_lv = [(r[0],) for r in rows[: max(0, min(global_rank - 1, LEADERBOARD_RANK_SCAN))]]
    top10 = rows[:10]
    return my_level, my_exp, pool, richer_lv, top10, global_rank


def register_stats_commands(bot, ctx: typing.Dict[str, typing.Any]) -> None:
    ALLOWED_HOST_IDS = ctx["ALLOWED_HOST_IDS"]
    get_share_enabled = ctx["get_share_enabled"]
    CASINO_RECOVERY_SHARE_RATE = ctx["CASINO_RECOVERY_SHARE_RATE"]
    CASINO_RECOVERY_SHARE_TARGET_ID = ctx["CASINO_RECOVERY_SHARE_TARGET_ID"]
    ensure_user_exists_async = ctx["ensure_user_exists_async"]
    interaction_send = ctx["interaction_send"]
    interaction_defer_if_needed = ctx["interaction_defer_if_needed"]
    fetch_casino_share_stats_rows_async = ctx["fetch_casino_share_stats_rows_async"]

    @bot.tree.command(name="leaderboard", description="前 10 名")
    async def leaderboard(interaction: discord.Interaction):
        await interaction_defer_if_needed(interaction)
        await ensure_user_exists_async(interaction.user.id, 50000)
        my_bal, pool, richer, top10, global_rank = await _fetch_balance_leaderboard_core(interaction.user.id)
        data = top10
        my_rank = global_rank
        title = "🏆 排行榜（全站）"
        note = "\n\n※ 已改為全站榜單，不再限制當前伺服器成員。"

        lines = [f"{i+1}. <@{uid}>: {int(bal):,}" for i, (uid, bal) in enumerate(data)]
        msg = "\n".join(lines) if lines else "（尚無符合條件的成員）"
        msg += f"\n\n📍 你的目前名次：**#{my_rank}**（餘額 `{my_bal:,}`）{note}"
        emb = discord.Embed(title=title, description=msg)
        await interaction_send(interaction, embed=emb)

    @bot.tree.command(name="casino_stats", description="查看經濟總金流統計（回收率/總發幣量/流通量）")
    async def casino_stats(interaction: discord.Interaction):
        await interaction_defer_if_needed(interaction)
        total_issued, total_recovered, circulation = await snapshot_cache.get_casino_stats_rows_cached()

        recovery_rate = (total_recovered / total_issued * 100) if total_issued > 0 else 0.0
        net_issued = total_issued - total_recovered
        embed = discord.Embed(title="🏦 經濟總金流統計", color=0x2b2d31)
        embed.add_field(name="金錢回收率", value=f"`{recovery_rate:.2f}%`", inline=False)
        embed.add_field(name="總發幣量", value=f"`{total_issued:,}` 東雲幣", inline=False)
        embed.add_field(name="總回收量", value=f"`{total_recovered:,}` 東雲幣", inline=False)
        embed.add_field(name="淨發行量", value=f"`{net_issued:,}` 東雲幣", inline=False)
        embed.add_field(name="目前流通量", value=f"`{circulation:,}` 東雲幣", inline=False)
        embed.set_footer(text="計算基準：casino_logs（logs 全期鏡像總帳）與 users.balance")
        await interaction_send(interaction, embed=embed)

    @bot.tree.command(name="share_stats", description="查看賭場回收分潤統計（管理）")
    @app_commands.describe(days="近幾天統計（預設 7 天）")
    async def share_stats(interaction: discord.Interaction, days: int = 7):
        if interaction.user.id not in ALLOWED_HOST_IDS:
            return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
        await interaction_defer_if_needed(interaction, ephemeral=True)
        total, recent, by_reason = await fetch_casino_share_stats_rows_async(days)
        embed = discord.Embed(title="📊 賭場回收分潤統計", color=0x5865F2)
        embed.add_field(name="分潤功能", value="啟用" if get_share_enabled() else "停用", inline=True)
        embed.add_field(name="分潤比例", value=f"`{CASINO_RECOVERY_SHARE_RATE * 100:.2f}%`", inline=True)
        embed.add_field(name="分潤目標", value=f"<@{CASINO_RECOVERY_SHARE_TARGET_ID}>", inline=True)
        embed.add_field(name="累計分潤總額", value=f"`{total:,}` 東雲幣", inline=False)
        embed.add_field(name=f"近 {max(1, int(days))} 天分潤", value=f"`{recent:,}` 東雲幣", inline=False)
        if by_reason:
            lines: typing.List[str] = []
            for reason, amt in by_reason:
                amt_i = int(amt or 0)
                rs = str(reason or "未知來源")
                lines.append(f"• {rs}：`{amt_i:,}`")
            embed.add_field(name="來源明細（累計）", value="\n".join(lines)[:1024], inline=False)
        await interaction_send(interaction, embed=embed, ephemeral=True)

    @bot.tree.command(name="lvleaderboard", description="等級排行榜前 10 名")
    async def lvleaderboard(interaction: discord.Interaction):
        await interaction_defer_if_needed(interaction)
        await ensure_user_exists_async(interaction.user.id, 50000)
        my_level, my_exp, pool, richer_lv, top10, global_rank = await _fetch_level_leaderboard_core(interaction.user.id)
        data = top10
        my_rank = global_rank
        title = "🧠 Lv 排行榜（全站）"
        note = "\n\n※ 已改為全站榜單，不再限制當前伺服器成員。"

        if not data:
            return await interaction_send(interaction, "目前沒有符合條件的等級資料。", ephemeral=True)

        msg = "\n".join(
            [f"{i+1}. <@{uid}>: Lv.{int(lv)} | EXP {int(exp):,}" for i, (uid, lv, exp) in enumerate(data)]
        )
        msg += f"\n\n📍 你的目前名次：**#{my_rank}**（Lv.{my_level} | EXP {my_exp:,}）{note}"
        emb = discord.Embed(title=title, description=msg)
        await interaction_send(interaction, embed=emb)
