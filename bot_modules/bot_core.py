import asyncio
import datetime
import logging
import os
import sys
import typing

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.db import is_transient_db_error


def setup_bot_logging() -> logging.Logger:
    """主程序日誌：主控台必出；若設定 BOT_LOG_FILE 或 LOG_FILE 則同步寫入檔案。"""
    name = "shinonome_bot"
    log = logging.getLogger(name)
    if log.handlers:
        return log
    level_raw = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_raw, logging.INFO)
    log.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _log_tz = datetime.timezone(datetime.timedelta(hours=8))
    fmt.converter = lambda ts: datetime.datetime.fromtimestamp(ts, _log_tz).timetuple()
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)
    log.addHandler(ch)
    log.propagate = False
    log_path = (os.getenv("BOT_LOG_FILE") or os.getenv("LOG_FILE") or "").strip()
    if log_path:
        try:
            log_dir = os.path.dirname(log_path)
            if log_dir and not os.path.isdir(log_dir):
                os.makedirs(log_dir, exist_ok=True)
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setLevel(level)
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError as e:
            log.warning("無法建立日誌檔 %s: %s", log_path, e)
    return log


def configure_stdio_line_buffering() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(line_buffering=True)
            except Exception:
                pass


def members_intent_enabled() -> bool:
    return str(os.getenv("DISCORD_MEMBERS_INTENT", "true")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True
    if hasattr(intents, "dm_messages"):
        intents.dm_messages = True
    # 沒有 members 時 guild.get_member() 幾乎只在對方位於語音頻道時才有值。
    intents.members = members_intent_enabled()
    return intents


def create_shinonome_bot(
    *,
    logger: logging.Logger,
    allowed_host_ids: typing.Sequence[int],
    dm_relay_channel_id: int,
    chunk_text_lines,
    discord_message_cap: int,
    intents: typing.Optional[discord.Intents] = None,
) -> commands.Bot:
    intents = intents or build_intents()

    @app_commands.command(
        name="dev_list_guilds",
        description="[開發者] 列出機器人所在的所有伺服器（名稱、成員數、ID）",
    )
    async def dev_list_guilds_command(interaction: discord.Interaction):
        if interaction.user.id not in allowed_host_ids:
            return await interaction.response.send_message("❌ 僅限開發者使用。", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        lines: typing.List[str] = []
        for g in sorted(interaction.client.guilds, key=lambda x: (x.name or "").lower()):
            name = g.name or "(無名稱)"
            name_safe = discord.utils.escape_markdown(name)
            try:
                mc = g.member_count
            except Exception:
                mc = None
            if mc is None:
                mc = len(g.members)
            lines.append(f"• **{name_safe}** — 成員 `{mc:,}` ｜ ID `{g.id}`")
        if not lines:
            return await interaction.followup.send("目前沒有任何伺服器。", ephemeral=True)
        header = f"**服務中伺服器**（共 `{len(interaction.client.guilds)}` 個）\n"
        parts = chunk_text_lines(lines)
        first_combined = header + parts[0]
        if len(first_combined) <= discord_message_cap:
            await interaction.followup.send(first_combined, ephemeral=True)
            for p in parts[1:]:
                await interaction.followup.send(p, ephemeral=True)
        else:
            await interaction.followup.send(header.rstrip(), ephemeral=True)
            for p in parts:
                await interaction.followup.send(p, ephemeral=True)
        logger.info(
            "dev_list_guilds: user=%s listed %s guilds",
            interaction.user.id,
            len(interaction.client.guilds),
        )

    class ShinonomeBot(commands.Bot):
        async def setup_hook(self) -> None:
            env_override = (os.getenv("DEV_RELAY_GUILD_ID") or "").strip()
            gid: typing.Optional[int] = int(env_override) if env_override else None
            if gid is None:
                try:
                    ch = await self.fetch_channel(dm_relay_channel_id)
                    if ch and getattr(ch, "guild", None):
                        gid = ch.guild.id
                except Exception as e:
                    logger.warning(
                        "無法自動取得轉接頻道所在伺服器（dev_list_guilds 可能以全域註冊）: %s", e
                    )
            try:
                if gid:
                    self.tree.add_command(dev_list_guilds_command, guild=discord.Object(id=gid))
                    logger.info("dev_list_guilds 僅註冊於伺服器 %s（其他伺服器不會出現此指令）", gid)
                else:
                    self.tree.add_command(dev_list_guilds_command)
                    logger.warning(
                        "dev_list_guilds 以全域註冊。若要隱藏，請設 DEV_RELAY_GUILD_ID 或確認 bot 能讀取 DM_RELAY_CHANNEL_ID"
                    )
            except Exception as e:
                logger.exception("註冊 dev_list_guilds 失敗，改為全域: %s", e)
                self.tree.add_command(dev_list_guilds_command)

    return ShinonomeBot(command_prefix="!", intents=intents)


def register_on_ready(
    bot,
    *,
    logger: logging.Logger,
    dm_relay_channel_id: int,
    init_db,
    discord_log_register,
    event_tasks: typing.Dict[str, typing.Any],
    snapshot_cache_tasks: typing.Dict[str, typing.Any],
) -> None:
    background_tasks: typing.Dict[str, asyncio.Task] = {}
    ready_lock = asyncio.Lock()
    initialized = False
    bot._db_ready_event = asyncio.Event()

    @bot.event
    async def on_ready():
        async with ready_lock:
            await initialize_ready()

    async def initialize_ready():
        nonlocal initialized
        discord_log_register(bot, logger, default_channel_id=dm_relay_channel_id)
        retry_delay = 5
        while not initialized and not bot.is_closed():
            try:
                await asyncio.to_thread(init_db)
                initialized = True
                bot._db_ready_event.set()
                logger.info("資料庫初始化完成")
            except Exception as e:
                if not is_transient_db_error(e):
                    logger.exception("init_db 失敗: %s", e)
                    return
                logger.warning("資料庫暫時無法連線，%s 秒後重試初始化（%s）", retry_delay, type(e).__name__)
                await asyncio.sleep(retry_delay)
                retry_delay = min(30, retry_delay * 2)
        if not initialized:
            return
        try:
            synced = await bot.tree.sync()
            logger.info("Slash 指令同步完成: %s 個", len(synced))
        except Exception as e:
            logger.exception("Slash 同步失敗: %s", e)
        for guild in bot.guilds:
            try:
                gsynced = await bot.tree.sync(guild=guild)
                logger.info("Guild 同步完成 %s: %s 個指令", guild.id, len(gsynced))
            except Exception as e:
                logger.exception("Guild 同步失敗 %s: %s", guild.id, e)
        for name, factory in {**event_tasks, **snapshot_cache_tasks}.items():
            if not name.endswith("_task"):
                continue
            existing = background_tasks.get(name)
            if existing is None or existing.done():
                if existing is not None and not existing.cancelled():
                    error = existing.exception()
                    if error is not None:
                        logger.error("背景任務 %s 已停止: %s", name, error)
                background_tasks[name] = asyncio.create_task(factory(), name=name)
        logger.info("機器人已啟動: %s（伺服器數 %s）", bot.user, len(bot.guilds))
