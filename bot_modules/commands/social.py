import random
import typing

import discord
from discord import app_commands

EIGHT_BALL_ANSWERS = [
    "肯定是的。",
    "絕對沒問題。",
    "可以相信它。",
    "看起來很有希望。",
    "跡象指向是的。",
    "目前說不好，再等等。",
    "現在別問了。",
    "集中精神再問一次。",
    "別抱太大希望。",
    "我的答案是否。",
    "前景不太樂觀。",
    "非常懷疑。",
]


def register_social_commands(bot, ctx: typing.Dict[str, typing.Any]) -> None:
    ensure_user_exists_async = ctx["ensure_user_exists_async"]
    interaction_send = ctx["interaction_send"]
    interaction_defer_if_needed = ctx["interaction_defer_if_needed"]
    resolve_slash_target = ctx["resolve_slash_target"]
    tw_naive_to_discord_ts = ctx["tw_naive_to_discord_ts"]
    build_exp_progress_bar = ctx["build_exp_progress_bar"]
    calc_level_from_exp = ctx["calc_level_from_exp"]
    MAX_LEVEL = ctx["MAX_LEVEL"]
    fetch_user_cooldowns_async = ctx["fetch_user_cooldowns_async"]
    fetch_user_profile_async = ctx["fetch_user_profile_async"]
    fetch_user_ranks_async = ctx["fetch_user_ranks_async"]
    fetch_compare_async = ctx["fetch_compare_async"]

    def _role_label(role: str) -> str:
        return {"cop": "🚔 警察", "criminal": "🔪 搶匪"}.get(role, "👤 平民")

    def _cooldown_line(item: typing.Dict[str, typing.Any]) -> str:
        if item.get("ready"):
            note = item.get("note")
            return f"• {item['name']}：**可立即使用**" + (f"（{note}）" if note else "")
        next_dt = item.get("next_dt")
        if next_dt:
            ts = tw_naive_to_discord_ts(next_dt)
            return f"• {item['name']}：⏳ <t:{ts}:R>（<t:{ts}:F>）"
        return f"• {item['name']}：⏳ 冷卻中"

    @bot.tree.command(name="cooldowns", description="查看自己的指令冷卻狀態")
    async def cooldowns_slash(interaction: discord.Interaction):
        await ensure_user_exists_async(interaction.user.id, 50000)
        data = await fetch_user_cooldowns_async(interaction.user.id)
        lines = [_cooldown_line(x) for x in data.get("items", [])]
        emb = discord.Embed(
            title="⏳ 冷卻總覽",
            description="\n".join(lines)[:3900] if lines else "（無可顯示項目）",
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @bot.tree.command(name="profile", description="查看玩家名片（餘額、等級、陣營、戰績）")
    @app_commands.describe(member="要查看的玩家（留空看自己）", user_id="或填使用者 ID／貼提及")
    async def profile_slash(
        interaction: discord.Interaction,
        member: typing.Optional[discord.Member] = None,
        user_id: typing.Optional[str] = None,
    ):
        target_user, err = await resolve_slash_target(
            interaction, member, user_id, required=False, in_guild_only=False
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        target = target_user or interaction.user
        await ensure_user_exists_async(target.id, 50000)
        profile = await fetch_user_profile_async(target.id)
        if not profile:
            return await interaction.response.send_message("找不到資料。", ephemeral=True)

        calc_lv, cur_exp, need_exp = calc_level_from_exp(profile["exp"])
        level_num = max(int(profile["level"]), int(calc_lv))
        bar = build_exp_progress_bar(cur_exp, need_exp) if level_num < MAX_LEVEL else "▓" * 12 + "  100%"
        emb = discord.Embed(
            title=f"📇 {target.display_name} 的名片",
            color=0x57F287 if profile["balance"] >= 0 else 0xED4245,
        )
        emb.set_thumbnail(url=target.display_avatar.url)
        emb.add_field(name="餘額", value=f"`{profile['balance']:,}` 東雲幣", inline=True)
        emb.add_field(name="等級", value=f"Lv.{level_num}", inline=True)
        emb.add_field(name="陣營", value=_role_label(profile["role"]), inline=True)
        emb.add_field(name="EXP", value=f"{bar}\n`{cur_exp:,}` / `{need_exp:,}`", inline=False)
        emb.add_field(name="餘額排名", value=f"#{profile['balance_rank']}", inline=True)
        emb.add_field(name="等級排名", value=f"#{profile['level_rank']}", inline=True)
        emb.add_field(
            name="賭場戰績",
            value=(
                f"局數 `{profile['total_games']}`｜勝 `{profile['wins']}`｜"
                f"勝率 `{profile['win_rate']:.1f}%`｜累計 `{profile['total_profit']:,}`"
            ),
            inline=False,
        )
        if profile["role"] == "criminal":
            emb.add_field(name="通緝", value="⭐" * min(5, profile["wanted_stars"]), inline=True)
        if profile["in_prison"]:
            emb.add_field(name="監獄", value="🔒 在押", inline=True)
        if profile["role"] == "civilian" and profile["good_citizen_active"]:
            emb.add_field(name="良民證", value="🪪 已啟用", inline=True)
        if profile["bail_debt"] > 0:
            emb.add_field(name="假釋欠款", value=f"`{profile['bail_debt']:,}`", inline=True)
        await interaction.response.send_message(embed=emb)

    @bot.tree.command(name="rank", description="查看自己的全站名次")
    async def rank_slash(interaction: discord.Interaction):
        await ensure_user_exists_async(interaction.user.id, 50000)
        ranks = await fetch_user_ranks_async(interaction.user.id)
        if not ranks:
            return await interaction.response.send_message("找不到資料。", ephemeral=True)
        emb = discord.Embed(title="📍 你的目前名次", color=0x5865F2)
        emb.add_field(
            name="餘額榜",
            value=f"#{ranks['balance_rank']}（`{ranks['balance']:,}` 東雲幣）",
            inline=False,
        )
        emb.add_field(
            name="等級榜",
            value=f"#{ranks['level_rank']}（Lv.{ranks['level']}｜EXP `{ranks['exp']:,}`）",
            inline=False,
        )
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @bot.tree.command(name="compare", description="比較兩位玩家的餘額、等級與陣營")
    @app_commands.describe(
        left="玩家 A（選人）",
        right="玩家 B（選人）",
        left_id="或填 A 的使用者 ID",
        right_id="或填 B 的使用者 ID",
    )
    async def compare_slash(
        interaction: discord.Interaction,
        left: typing.Optional[discord.Member] = None,
        right: typing.Optional[discord.Member] = None,
        left_id: typing.Optional[str] = None,
        right_id: typing.Optional[str] = None,
    ):
        user_a, err_a = await resolve_slash_target(
            interaction, left, left_id, required=True, in_guild_only=False
        )
        if err_a:
            return await interaction.response.send_message(err_a, ephemeral=True)
        user_b, err_b = await resolve_slash_target(
            interaction, right, right_id, required=True, in_guild_only=False
        )
        if err_b:
            return await interaction.response.send_message(err_b, ephemeral=True)
        if user_a.id == user_b.id:
            return await interaction.response.send_message("請選擇兩位不同的玩家。", ephemeral=True)

        await ensure_user_exists_async(user_a.id, 50000)
        await ensure_user_exists_async(user_b.id, 50000)
        data = await fetch_compare_async(user_a.id, user_b.id)
        pa, pb = data.get("a"), data.get("b")
        if not pa or not pb:
            return await interaction.response.send_message("找不到其中一位玩家的資料。", ephemeral=True)

        def _line(label: str, va, vb, fmt=str):
            return f"**{label}**\n{user_a.mention}：`{fmt(va)}`\n{user_b.mention}：`{fmt(vb)}`"

        emb = discord.Embed(title="⚖️ 玩家對照", color=0xFEE75C)
        emb.add_field(
            name="餘額",
            value=_line("餘額", pa["balance"], pb["balance"], lambda x: f"{int(x):,}"),
            inline=False,
        )
        emb.add_field(
            name="等級",
            value=_line("等級", pa["level"], pb["level"], lambda x: f"Lv.{int(x)}"),
            inline=False,
        )
        emb.add_field(
            name="陣營",
            value=_line("陣營", pa["role"], pb["role"], _role_label),
            inline=False,
        )
        emb.add_field(
            name="勝率",
            value=_line("勝率", pa["win_rate"], pb["win_rate"], lambda x: f"{float(x):.1f}%"),
            inline=False,
        )
        await interaction.response.send_message(embed=emb)

    @bot.tree.command(name="8ball", description="向魔法八號球提問")
    @app_commands.describe(question="你想問什麼？")
    async def eight_ball_slash(interaction: discord.Interaction, question: str):
        q = (question or "").strip()
        if not q:
            return await interaction.response.send_message("請輸入問題。", ephemeral=True)
        answer = random.choice(EIGHT_BALL_ANSWERS)
        emb = discord.Embed(title="🎱 魔法八號球", color=0x2b2d31)
        emb.add_field(name="問題", value=q[:1024], inline=False)
        emb.add_field(name="回答", value=answer, inline=False)
        await interaction.response.send_message(embed=emb)

    @bot.tree.command(name="choose", description="從多個選項中隨機選一個")
    @app_commands.describe(options="選項，用逗號、頓號或 | 分隔")
    async def choose_slash(interaction: discord.Interaction, options: str):
        raw = (options or "").strip()
        if not raw:
            return await interaction.response.send_message("請輸入选項。", ephemeral=True)
        for sep in ("|", "，", "、", ";", "；"):
            raw = raw.replace(sep, ",")
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if len(parts) < 2:
            return await interaction.response.send_message("至少需要 2 個選項。", ephemeral=True)
        if len(parts) > 25:
            parts = parts[:25]
        picked = random.choice(parts)
        await interaction.response.send_message(f"🎲 我幫你選了：**{picked}**")
