import asyncio
import logging
import os
import typing

import discord
from discord.ext import commands

_installed = False


class DiscordLogHandler(logging.Handler):
    """將 logging 轉成非同步發送到 Discord 文字頻道（避免阻塞 logging）。"""

    def __init__(self, client: commands.Bot, channel_id: int):
        super().__init__()
        self.client = client
        self.channel_id = channel_id

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            if len(msg) > 1900:
                msg = msg[:1900] + "…"
            loop = self.client.loop
            if loop is None or not loop.is_running():
                return
            fut = asyncio.run_coroutine_threadsafe(self._send(msg), loop)

            def _discard_future_result(f: asyncio.Future) -> None:
                try:
                    f.result()
                except Exception:
                    pass

            fut.add_done_callback(_discard_future_result)
        except Exception:
            self.handleError(record)

    async def _send(self, text: str) -> None:
        try:
            ch = self.client.get_channel(self.channel_id)
            if ch is None:
                ch = await self.client.fetch_channel(self.channel_id)
            if not isinstance(ch, discord.abc.Messageable):
                return
            chunk = text[:1990]
            await ch.send(f"```\n{chunk}\n```")
        except Exception:
            pass


def register_discord_log_handler(
    client: commands.Bot,
    logger: logging.Logger,
    *,
    default_channel_id: int,
) -> None:
    """啟動後掛上 Discord 頻道日誌；可用 LOG_DISCORD_CHANNEL_ID 覆寫 default_channel_id。"""
    global _installed
    if _installed:
        return
    raw = (os.getenv("LOG_DISCORD_CHANNEL_ID") or "").strip()
    ch_id = int(raw) if raw else default_channel_id
    if not ch_id:
        return
    try:
        h = DiscordLogHandler(client, ch_id)
        h.setLevel(logger.level)
        h.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(h)
        _installed = True
        logger.info("已啟用 Discord 頻道日誌（頻道 ID %s）", ch_id)
    except Exception as e:
        logger.warning("無法註冊 Discord 日誌 handler: %s", e)
