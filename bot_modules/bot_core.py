import datetime
import logging
import os
import sys
import typing

import discord
from discord import app_commands
from discord.ext import commands


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


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True
    if hasattr(intents, "dm_messages"):
        intents.dm_messages = True
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
    @bot.event
    async def on_ready():
        discord_log_register(bot, logger, default_channel_id=dm_relay_channel_id)
        try:
            init_db()
            logger.info("資料庫初始化完成")
        except Exception as e:
            logger.exception("init_db 失敗: %s", e)
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
        bot.loop.create_task(event_tasks["vc_reward_task"]())
        bot.loop.create_task(event_tasks["logs_retention_task"]())
        bot.loop.create_task(event_tasks["cache_cleanup_task"]())
        bot.loop.create_task(snapshot_cache_tasks["emit_cache_metrics_log_task"]())
        bot.loop.create_task(snapshot_cache_tasks["refresh_leaderboard_snapshots_task"]())
        bot.loop.create_task(snapshot_cache_tasks["refresh_casino_stats_snapshot_task"]())
        logger.info("機器人已啟動: %s（伺服器數 %s）", bot.user, len(bot.guilds))
