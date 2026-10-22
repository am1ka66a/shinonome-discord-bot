"""One policy for Slash commands, components and passive rewards."""
import asyncio
import logging

from bot_modules.user_repo import is_blacklisted
from bot_modules.discord_helpers import interaction_defer_if_needed, interaction_send

READ_COMMANDS = {
    "help", "adminhelp", "balance", "level", "record", "leaderboard", "lvleaderboard",
    "casino_stats", "share_stats", "wanted_status", "wanted_list", "good_citizen_list",
    "profile", "cooldowns", "compare", "rank",
}


async def allowed(bot, uid):
    # Tests can inject a checker; production installs it before command registration.
    checker = getattr(bot, "blacklist_checker", None)
    if checker is None:
        return True
    try:
        return not await checker(uid)
    except Exception:
        logging.getLogger(__name__).exception("黑名單查詢失敗 user=%s", uid)
        return False


def install_policy(bot, host_ids, logger):
    async def checker(uid):
        return await asyncio.to_thread(is_blacklisted, uid)

    bot.blacklist_checker = checker
    previous = bot.tree.interaction_check

    async def check(interaction):
        ready = getattr(bot, "_db_ready_event", None)
        if ready is not None and not ready.is_set():
            await interaction_send(interaction, "機器人正在初始化，請稍後再試。", ephemeral=True)
            return False
        command = interaction.command
        name = getattr(command, "qualified_name", "")
        read_only = name in READ_COMMANDS or name in {
            "lottery status", "russian_roulette stats me", "russian_roulette stats wins", "russian_roulette stats rate"}
        admin_operation = name.startswith("admin_") or name in {
            "say", "ban", "unban", "give", "take", "setlevel", "lock", "resetall_zero", "resetall_default", "dev_list_guilds"}
        if not read_only and not (admin_operation and interaction.user.id in host_ids):
            try:
                permit = await asyncio.wait_for(allowed(bot, interaction.user.id), timeout=1.5)
            except Exception:
                logger.exception("黑名單檢查失敗")
                permit = False
            if not permit:
                await interaction_send(interaction, "目前無法使用遊戲或經濟操作；查詢與客服仍可使用。", ephemeral=True)
                return False
        return await previous(interaction)

    bot.tree.interaction_check = check
    # Existing paid games may finish/refund. New debits/claims check again at
    # their economic entry points, including buttons sent before /ban.


async def require_allowed(bot, interaction):
    await interaction_defer_if_needed(interaction, ephemeral=True, thinking=False)
    if not await allowed(bot, interaction.user.id):
        await interaction_send(interaction, "你目前無法進行遊戲或經濟操作。", ephemeral=True)
        return False
    return True
