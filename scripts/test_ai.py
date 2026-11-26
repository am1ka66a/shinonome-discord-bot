import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from aiohttp import web
from aiohttp.test_utils import TestServer
from discord import app_commands

from bot_modules.commands.ai import register_ai_commands, request_answer


class AITest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = discord.Client(intents=discord.Intents.none())
        self.bot.tree = app_commands.CommandTree(self.bot)
        self.guild = discord.Object(id=101)
        self.env = {
            "SHINONOME_AI_ENABLED": "true",
            "SHINONOME_AI_GUILD_ID": "101",
            "SHINONOME_AI_CHANNEL_ID": "102",
            "SHINONOME_AI_USER_ID": "103",
            "SHINONOME_AI_GATEWAY_URL": "http://localhost:18789",
            "SHINONOME_AI_GATEWAY_TOKEN": "test-only",
        }

    def register(self, env):
        with patch.dict(os.environ, env, clear=True):
            register_ai_commands(self.bot)
        return self.bot.tree.get_command("ask", guild=self.guild)

    def interaction(self, *, user_id=103, channel_id=102):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            channel_id=channel_id,
            response=SimpleNamespace(
                is_done=lambda: False,
                send_message=AsyncMock(),
                defer=AsyncMock(),
            ),
            edit_original_response=AsyncMock(),
        )

    async def test_command_only_registers_when_configured(self):
        self.assertIsNone(self.register({}))
        with self.assertLogs("bot_modules.commands.ai", level="WARNING"):
            self.assertIsNone(self.register({"SHINONOME_AI_ENABLED": "true"}))
        self.assertIsNotNone(self.register(self.env))
        self.assertIsNone(self.bot.tree.get_command("ask"))

    async def test_only_configured_user_and_channel_can_reach_gateway(self):
        command = self.register(self.env)
        with patch("bot_modules.commands.ai.request_answer", new_callable=AsyncMock) as gateway:
            for interaction in (self.interaction(user_id=999), self.interaction(channel_id=999)):
                await command.callback(interaction, "你好")
                interaction.response.send_message.assert_awaited_once()
                self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
            gateway.assert_not_awaited()

    async def test_answer_is_private_and_cannot_ping_users(self):
        command = self.register(self.env)
        interaction = self.interaction()
        with patch("bot_modules.commands.ai.request_answer", return_value="@everyone 你好") as gateway:
            await command.callback(interaction, "  你好  ")
        gateway.assert_awaited_once_with(
            "http://localhost:18789/v1/chat/completions", "test-only", "你好"
        )
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        response = interaction.edit_original_response.await_args.kwargs
        self.assertEqual(response["content"], "@everyone 你好")
        self.assertEqual(response["allowed_mentions"].to_dict(), {"parse": []})

    async def test_gateway_timeout_finishes_the_interaction(self):
        command = self.register(self.env)
        interaction = self.interaction()
        with patch("bot_modules.commands.ai.request_answer", side_effect=TimeoutError):
            with self.assertLogs("bot_modules.commands.ai", level="WARNING"):
                await command.callback(interaction, "你好")
        self.assertEqual(
            interaction.edit_original_response.await_args.kwargs["content"],
            "AI 暫時無法回覆，請稍後再試。",
        )

    async def test_gateway_receives_only_chat_input_and_returns_text(self):
        received = {}

        async def complete(request):
            received["auth"] = request.headers.get("Authorization")
            received["body"] = await request.json()
            return web.json_response({"choices": [{"message": {"content": "  測試成功  "}}]})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", complete)
        async with TestServer(app) as server:
            reply = await request_answer(str(server.make_url("/v1/chat/completions")), "test-only", "你好")

        self.assertEqual(reply, "測試成功")
        self.assertEqual(received["auth"], "Bearer test-only")
        self.assertEqual(received["body"]["model"], "openclaw/default")
        self.assertNotIn("user", received["body"])
        self.assertEqual(received["body"]["messages"][-1], {"role": "user", "content": "你好"})
