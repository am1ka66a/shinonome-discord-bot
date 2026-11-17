import asyncio
import collections
import hashlib
import threading
import logging
import os
import typing

import discord
from discord.ext import commands

_installed = False


class DiscordLogHandler(logging.Handler):
    """Bounded, coalescing Discord delivery; full logs remain in the console."""

    def __init__(self, client, channel_id, capacity=200, interval=5):
        super().__init__()
        self.client, self.channel_id = client, channel_id
        self.capacity, self.interval = capacity, interval
        self.pending = collections.OrderedDict()
        self.guard = threading.Lock()
        self.scheduled = False
        self.stopped = False
        self.task = None
        self.dropped = 0

    def emit(self, record):
        try:
            loop = self.client.loop
            if not loop.is_running():
                return
            key = (record.name, record.levelno, hashlib.sha256(record.getMessage().encode()).digest())
            text = self.format(record).replace("```", "` ` `")[:1700]
            with self.guard:
                if self.stopped:
                    return
                if key in self.pending:
                    self.pending[key][1] += 1
                elif len(self.pending) < self.capacity:
                    self.pending[key] = [text, 1]
                else:
                    self.dropped += 1
                if not self.scheduled:
                    self.scheduled = True
                    loop.call_soon_threadsafe(self._start)
        except Exception:
            self.handleError(record)

    def _start(self):
        if not self.stopped:
            self.task = asyncio.create_task(self._drain(), name="discord-log-delivery")

    def take_batch(self):
        with self.guard:
            lines = []
            if self.dropped:
                lines.append(f"有 {self.dropped} 筆 Discord 日誌因佇列容量限制省略；完整紀錄請查主控台。")
                self.dropped = 0
            size = sum(map(len, lines))
            while self.pending:
                key, (text, count) = next(iter(self.pending.items()))
                line = text + (f" [重複 {count} 次]" if count > 1 else "")
                if lines and size + len(line) + 1 > 1900:
                    break
                self.pending.pop(key)
                lines.append(line)
                size += len(line) + 1
            return "\n".join(lines)

    async def _drain(self):
        try:
            while not self.stopped:
                await asyncio.sleep(self.interval)
                text = self.take_batch()
                if text:
                    await self._send(text)
                with self.guard:
                    if not self.pending and not self.dropped:
                        self.scheduled = False
                        return
        except BaseException:
            with self.guard:
                self.scheduled = False
            raise

    async def _send(self, text):
        try:
            ch = self.client.get_channel(self.channel_id)
            if ch is None:
                ch = await self.client.fetch_channel(self.channel_id)
            if isinstance(ch, discord.abc.Messageable):
                await asyncio.wait_for(ch.send(f"```\n{text}\n```",
                    allowed_mentions=discord.AllowedMentions.none()), timeout=20)
        except Exception:
            # Delivery failure must not recursively emit more Discord logs.
            pass

    def close(self):
        with self.guard:
            self.stopped = True
        try:
            if self.task and not self.task.done():
                self.client.loop.call_soon_threadsafe(self.task.cancel)
        except RuntimeError:
            pass
        super().close()


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
        raw_level = os.getenv("LOG_DISCORD_LEVEL", "WARNING").upper()
        h.setLevel(getattr(logging, raw_level, logging.WARNING))
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
