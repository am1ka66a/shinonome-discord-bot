import asyncio
import logging
import time
import typing

import discord
from discord import app_commands

DEFAULT_READ_ONLY_APP_COMMANDS: typing.Set[str] = {
    "help",
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
    locks: typing.Dict[int, typing.Tuple[str, float]] = {}
    guard = asyncio.Lock()

    async def enforce_single_active_app_command(interaction: discord.Interaction) -> bool:
        user = getattr(interaction, "user", None)
        if user is None:
            return True
        command_name = getattr(getattr(interaction, "command", None), "name", "") or ""
        if command_name in read_only:
            return True
        uid = int(user.id)
        now_ts = time.time()
        async with guard:
            active = locks.get(uid)
            started_ts = active[1] if active else None
            if started_ts is not None and (now_ts - started_ts) < timeout_seconds:
                await interaction.response.send_message(
                    "⏳ 你有一個會變更資料的指令仍在執行中，請稍候再試。",
                    ephemeral=True,
                )
                return False
            locks[uid] = ("mutating", now_ts)
        return True

    # CommandTree.interaction_check 是一般的 coroutine 方法，不是裝飾器；
    # 用 @bot.tree.interaction_check 只會產生一個沒人 await 的 coroutine，鎖等於沒裝上。
    bot.tree.interaction_check = enforce_single_active_app_command

    async def release_lock(interaction: discord.Interaction) -> None:
        user = getattr(interaction, "user", None)
        if user is None:
            return
        async with guard:
            locks.pop(int(user.id), None)

    @bot.event
    async def on_app_command_completion(interaction: discord.Interaction, command: app_commands.Command):
        await release_lock(interaction)

    # 指令失敗時 CommandTree 只會呼叫 tree.on_error，不會派送 client 事件，
    # 所以釋放鎖必須掛在這裡，否則出錯一次就會把使用者鎖到逾時為止。
    previous_on_error = bot.tree.on_error

    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await release_lock(interaction)
        logger.exception("app command 錯誤: %s", error)
        await previous_on_error(interaction, error)

    bot.tree.on_error = on_app_command_error
