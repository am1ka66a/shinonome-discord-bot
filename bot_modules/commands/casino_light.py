import typing

import discord
from discord import app_commands


def register_casino_light_commands(bot, ctx: typing.Dict[str, typing.Any]) -> None:
    FEATURE_TOGGLES = ctx["FEATURE_TOGGLES"]
    get_is_event_active = ctx["get_is_event_active"]
    COINFLIP_MIN_BET = ctx["COINFLIP_MIN_BET"]
    COINFLIP_MAX_BET = ctx["COINFLIP_MAX_BET"]
    LOTTERY_TICKET_COST = ctx["LOTTERY_TICKET_COST"]
    LOTTERY_MAX_TICKETS_PER_BUY = ctx["LOTTERY_MAX_TICKETS_PER_BUY"]
    ensure_user_exists_async = ctx["ensure_user_exists_async"]
    interaction_send = ctx["interaction_send"]
    interaction_defer_if_needed = ctx["interaction_defer_if_needed"]
    tw_naive_to_discord_ts = ctx["tw_naive_to_discord_ts"]
    settle_coinflip_async = ctx["settle_coinflip_async"]
    buy_lottery_tickets_async = ctx["buy_lottery_tickets_async"]
    fetch_lottery_status_async = ctx["fetch_lottery_status_async"]

    @bot.tree.command(name="coinflip", description="拋硬幣猜正反面（50% 勝率，贏得 2 倍下注）")
    @app_commands.describe(
        amount="下注金額",
        side="猜測結果",
    )
    @app_commands.choices(
        side=[
            app_commands.Choice(name="正面", value="正面"),
            app_commands.Choice(name="反面", value="反面"),
        ]
    )
    async def coinflip_slash(
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 10_000_000],
        side: str,
    ):
        if not FEATURE_TOGGLES.get("coinflip", True) or not get_is_event_active():
            return await interaction.response.send_message("🚫 賭場目前休息中。", ephemeral=True)
        bet = int(amount)
        if bet < COINFLIP_MIN_BET or bet > COINFLIP_MAX_BET:
            return await interaction.response.send_message(
                f"下注須介於 `{COINFLIP_MIN_BET:,}`～`{COINFLIP_MAX_BET:,}` 東雲幣。",
                ephemeral=True,
            )
        await ensure_user_exists_async(interaction.user.id, 50000)
        result = await settle_coinflip_async(interaction.user.id, bet, side)
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "insufficient":
                bal = int(result.get("balance") or 0)
                return await interaction.response.send_message(
                    f"餘額不足（目前 `{bal:,}`，需要 `{bet:,}`）。",
                    ephemeral=True,
                )
            if reason == "bad_amount":
                return await interaction.response.send_message(
                    f"下注須介於 `{COINFLIP_MIN_BET:,}`～`{COINFLIP_MAX_BET:,}` 東雲幣。",
                    ephemeral=True,
                )
            return await interaction.response.send_message("下注失敗，請稍後再試。", ephemeral=True)

        win = bool(result.get("win"))
        outcome = str(result.get("outcome") or "")
        new_bal = int(result.get("balance") or 0)
        color = 0x57F287 if win else 0xED4245
        title = "✅ 拋硬幣獲勝" if win else "❌ 拋硬幣落敗"
        profit = int(result.get("profit") or 0)
        emb = discord.Embed(title=title, color=color)
        emb.add_field(name="你的猜測", value=side, inline=True)
        emb.add_field(name="實際結果", value=outcome, inline=True)
        emb.add_field(name="下注", value=f"`{bet:,}` 東雲幣", inline=True)
        emb.add_field(
            name="本次盈虧",
            value=f"`{profit:+,}` 東雲幣",
            inline=False,
        )
        emb.add_field(name="目前餘額", value=f"`{new_bal:,}` 東雲幣", inline=False)
        await interaction.response.send_message(embed=emb)

    lottery_group = app_commands.Group(name="lottery", description="日彩池：購票參加、查看狀態")

    @lottery_group.command(name="status", description="查看今日彩池與你的購票")
    async def lottery_status_slash(interaction: discord.Interaction):
        if not FEATURE_TOGGLES.get("lottery", True):
            return await interaction.response.send_message("🚫 日彩池目前關閉。", ephemeral=True)
        await ensure_user_exists_async(interaction.user.id, 50000)
        status = await fetch_lottery_status_async(interaction.user.id)
        draw_ts = tw_naive_to_discord_ts(status["draw_dt"])
        emb = discord.Embed(title="🎟️ 日彩池狀態", color=0x5865F2)
        emb.add_field(name="今日期別", value=str(status["day_key"]), inline=True)
        emb.add_field(name="彩池", value=f"`{int(status['pool']):,}` 東雲幣", inline=True)
        emb.add_field(name="總票數", value=str(int(status["total_tickets"])), inline=True)
        emb.add_field(name="你的票數", value=str(int(status["my_tickets"])), inline=True)
        emb.add_field(name="你已投入", value=f"`{int(status['my_paid']):,}` 東雲幣", inline=True)
        if status.get("closed") and status.get("winner_id"):
            emb.add_field(name="今日得主", value=f"<@{status['winner_id']}>", inline=False)
        else:
            emb.add_field(name="預計開獎", value=f"<t:{draw_ts}:F>（<t:{draw_ts}:R>）", inline=False)
        emb.set_footer(text=f"每張票 `{LOTTERY_TICKET_COST:,}` 東雲幣｜每日 00:00 開獎｜/lottery buy 購票")
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @lottery_group.command(name="buy", description="購買今日彩池彩券")
    @app_commands.describe(tickets=f"購買張數（1～{LOTTERY_MAX_TICKETS_PER_BUY}）")
    async def lottery_buy_slash(
        interaction: discord.Interaction,
        tickets: app_commands.Range[int, 1, 50] = 1,
    ):
        if not FEATURE_TOGGLES.get("lottery", True) or not get_is_event_active():
            return await interaction.response.send_message("🚫 日彩池目前關閉。", ephemeral=True)
        await ensure_user_exists_async(interaction.user.id, 50000)
        result = await buy_lottery_tickets_async(interaction.user.id, int(tickets))
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "insufficient":
                bal = int(result.get("balance") or 0)
                cost = int(result.get("cost") or 0)
                return await interaction.response.send_message(
                    f"餘額不足（目前 `{bal:,}`，需要 `{cost:,}`）。",
                    ephemeral=True,
                )
            if reason == "round_closed":
                return await interaction.response.send_message("今日彩池已結算，請等待明日。", ephemeral=True)
            return await interaction.response.send_message("購票失敗，請稍後再試。", ephemeral=True)

        emb = discord.Embed(title="✅ 購票成功", color=0x57F287)
        emb.add_field(name="今日期別", value=str(result["day_key"]), inline=True)
        emb.add_field(name="本次購買", value=f"`{int(result['tickets_bought'])}` 張", inline=True)
        emb.add_field(name="你的總票數", value=f"`{int(result['my_tickets'])}` 張", inline=True)
        emb.add_field(name="目前彩池", value=f"`{int(result['pool']):,}` 東雲幣", inline=False)
        emb.add_field(name="目前餘額", value=f"`{int(result['balance']):,}` 東雲幣", inline=False)
        await interaction.response.send_message(embed=emb, ephemeral=True)

    bot.tree.add_command(lottery_group)
