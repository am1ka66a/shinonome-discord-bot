import asyncio
import logging
import time
import typing

import discord
from discord import app_commands
from bot_modules.discord_helpers import interaction_send

DEFAULT_READ_ONLY_APP_COMMANDS: typing.Set[str] = {
    "help",
    "kill",  # Sends a Minecraft-style message without changing player data.
    "balance",
    "level",
    "record",
    "leaderboard",
    "lvleaderboard",
    "casino_stats",
    "share_stats",
    "wanted_status",
    "wanted_list",
    "good_citizen_list",
    "admin_user_flags",
    "admin_logs",
    "dev_list_guilds",
    "admin_audit account", "admin_audit resets", "admin_audit pending",
    "admin_audit health", "admin_audit reconcile", "admin_audit ai",
    "profile", "cooldowns", "rank", "compare", "lottery status",
    "russian_roulette stats me", "russian_roulette stats wins", "russian_roulette stats rate",
}


def register_app_command_lock(
    bot,
    logger: logging.Logger,
    *,
    read_only_commands: typing.Optional[typing.Set[str]] = None,
    timeout_seconds: float = 180.0,
) -> None:
    """同一使用者的 mutating Slash 互斥；查詢類指令放行。"""
    read_only = read_only_commands if read_only_commands is not None else DEFAULT_READ_ONLY_APP_COMMANDS
    locks: typing.Dict[int, typing.Tuple[int, float]] = {}
    guard = asyncio.Lock()
    bot._active_mutating_commands = locks

    async def enforce_single_active_app_command(interaction: discord.Interaction) -> bool:
        # Suggestions never dispatch app_command_completion, so they must not acquire a lock.
        if getattr(interaction, "type", None) is discord.InteractionType.autocomplete:
            return True
        user = getattr(interaction, "user", None)
        if user is None:
            return True
        command = getattr(interaction, "command", None)
        command_name = getattr(command, "qualified_name", None) or getattr(command, "name", "") or ""
        if command_name in read_only:
            return True
        uid = int(user.id)
        now_ts = time.monotonic()
        async with guard:
            active = locks.get(uid)
            started_ts = active[1] if active else None
            if started_ts is not None and (now_ts - started_ts) < timeout_seconds:
                await interaction.response.send_message(
                    "⏳ 你有一個會變更資料的指令仍在執行中，請稍候再試。",
                    ephemeral=True,
                )
                return False
            locks[uid] = (interaction.id, now_ts)
        return True

    # CommandTree.interaction_check 是一般的 coroutine 方法，不是裝飾器；
    # 用 @bot.tree.interaction_check 只會產生一個沒人 await 的 coroutine，鎖等於沒裝上。
    bot.tree.interaction_check = enforce_single_active_app_command

    async def release_lock(interaction: discord.Interaction) -> None:
        user = getattr(interaction, "user", None)
        if user is None:
            return
        async with guard:
            uid = int(user.id)
            active = locks.get(uid)
            if active and active[0] == interaction.id:
                locks.pop(uid, None)

    @bot.event
    async def on_app_command_completion(interaction: discord.Interaction, command: app_commands.Command):
        await release_lock(interaction)

    # 指令失敗時 CommandTree 只會呼叫 tree.on_error，不會派送 client 事件，
    # 所以釋放鎖必須掛在這裡，否則出錯一次就會把使用者鎖到逾時為止。
    previous_on_error = bot.tree.on_error

    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await release_lock(interaction)
        logger.error("app command 錯誤 interaction=%s: %s", interaction.id, error,
                     exc_info=(type(error), error, error.__traceback__))
        await previous_on_error(interaction, error)
        command = getattr(interaction, "command", None)
        has_handler = getattr(command, "_has_any_error_handlers", None)
        if callable(has_handler) and has_handler():
            return
        try:
            await interaction_send(interaction,
                f"處理未能完成，請先查詢結果再重試。參考編號：`{interaction.id}`", ephemeral=True)
        except discord.HTTPException:
            logger.warning("無法送出指令錯誤通知 interaction=%s", interaction.id)

    bot.tree.on_error = on_app_command_error
