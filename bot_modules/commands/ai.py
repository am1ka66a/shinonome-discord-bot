import asyncio
import logging
import os
from urllib.parse import urlsplit

import aiohttp
import discord
from discord import app_commands

from bot_modules.discord_helpers import interaction_defer_if_needed, interaction_send

logger = logging.getLogger(__name__)


async def request_answer(endpoint: str, token: str, question: str) -> str:
    timeout = aiohttp.ClientTimeout(total=45, connect=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            endpoint,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": "openclaw/default",
                "messages": [
                    {"role": "system", "content": "你是 Shinonome 的文字助手。請使用繁體中文回答，盡量在 500 字內。"},
                    {"role": "user", "content": question},
                ],
            },
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                logger.warning("Shinonome AI Gateway 回傳 HTTP %s", response.status)
                return ""
            data = await response.json()

    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("Shinonome AI Gateway 回傳非預期格式")
        return ""
    return answer.strip() if isinstance(answer, str) else ""


def register_ai_commands(bot) -> None:
    if os.getenv("SHINONOME_AI_ENABLED", "").strip().lower() != "true":
        return

    try:
        guild_id = int(os.getenv("SHINONOME_AI_GUILD_ID", ""))
        channel_id = int(os.getenv("SHINONOME_AI_CHANNEL_ID", ""))
        user_id = int(os.getenv("SHINONOME_AI_USER_ID", ""))
        gateway = urlsplit(os.getenv("SHINONOME_AI_GATEWAY_URL", "").strip())
        token = os.getenv("SHINONOME_AI_GATEWAY_TOKEN", "").strip()
        if (
            min(guild_id, channel_id, user_id) <= 0
            or gateway.scheme not in {"http", "https"}
            or not gateway.hostname
            or gateway.username is not None
            or gateway.path not in {"", "/"}
            or gateway.query
            or gateway.fragment
            or not token
        ):
            raise ValueError
        # Validate the configured port before registering the command.
        gateway.port
    except ValueError:
        logger.warning("Shinonome AI 設定不完整或無效，略過 /ask 註冊")
        return

    endpoint = f"{gateway.scheme}://{gateway.netloc}/v1/chat/completions"
    request_lock = asyncio.Lock()

    @bot.tree.command(name="ask", description="向 Shinonome 的 AI 提問", guild=discord.Object(id=guild_id))
    @app_commands.describe(question="想問的問題")
    @app_commands.checks.cooldown(1, 15.0, key=lambda interaction: interaction.user.id)
    async def ask(interaction: discord.Interaction, question: app_commands.Range[str, 1, 1500]):
        if interaction.user.id != user_id or interaction.channel_id != channel_id:
            await interaction_send(interaction, "這個指令目前只開放給指定使用者及測試頻道。", ephemeral=True)
            return

        question = question.strip()
        if not question:
            await interaction_send(interaction, "請輸入問題。", ephemeral=True)
            return

        if request_lock.locked():
            await interaction_send(interaction, "上一個問題仍在處理中，請稍後再試。", ephemeral=True)
            return

        async with request_lock:
            await interaction_defer_if_needed(interaction, ephemeral=True)
            try:
                answer = await request_answer(endpoint, token, question)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                logger.warning("Shinonome AI Gateway 請求失敗 (%s)", type(error).__name__)
                answer = ""

        reply = answer or "AI 暫時無法回覆，請稍後再試。"
        if len(reply) > 1900:
            reply = reply[:1899] + "…"
        await interaction.edit_original_response(
            content=reply,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ask.error
    async def ask_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction_send(interaction, "提問太頻繁，請稍後再試。", ephemeral=True)
            return
        raise error
