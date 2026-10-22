import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discord
from bot_modules.runtime import relay
from bot_modules.discord_helpers import split_discord_message_chunks


class RelayTests(unittest.TestCase):
    def setUp(self):
        relay._relay_forward_meta.clear()
        self.bot = SimpleNamespace(user=SimpleNamespace(id=999), message_audit=None)
        self.target = Mock(spec=discord.TextChannel)
        self.target.guild = SimpleNamespace(id=111111111111111111)
        self.target.id = 222222222222222222
        self.target.send = AsyncMock(return_value=SimpleNamespace(id=444444444444444444))
        self.target.fetch_message = AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message"))
        self.bot.get_channel = Mock(return_value=self.target)
        self.bot.fetch_channel = AsyncMock(return_value=self.target)
        self.bot.fetch_user = AsyncMock(return_value=SimpleNamespace(id=333333333333333333, send=AsyncMock()))
        self.source = SimpleNamespace(id=10)
        self.message = SimpleNamespace(id=20, author=SimpleNamespace(id=2), channel=self.source,
            reference=SimpleNamespace(message_id=30), content="", attachments=[], reply=AsyncMock(), add_reaction=AsyncMock())
        relay._relay_forward_meta[30] = (333333333333333333, False, self.target.guild.id, self.target.id, 555555555555555555)
        handlers = relay.register_relay(self.bot, dict(logger=Mock(), DM_RELAY_CHANNEL_ID=10,
            ALLOWED_HOST_IDS=[2], DM_RELAY_NOTIFY_USER_ID=2, DISCORD_MESSAGE_CAP=2000,
            split_discord_message_chunks=split_discord_message_chunks))
        self.handler = handlers["relay_staff_reply_to_dm_user"]

    def invoke(self):
        coroutine = self.handler(self.message)
        try:
            coroutine.send(None)
        except StopIteration as done:
            return done.value
        finally:
            coroutine.close()
        self.fail("Unexpected unmocked I/O")

    def test_long_reply_with_missing_original_preserves_every_character(self):
        self.message.content = "甲乙🙂" * 1500
        self.assertTrue(self.invoke())
        parts = [call.kwargs["content"] for call in self.target.send.await_args_list]
        self.assertEqual("".join(parts), "<@333333333333333333> " + self.message.content)
        self.assertTrue(all(len(part) <= 2000 for part in parts))
        self.message.add_reaction.assert_awaited_once_with("✅")

    def test_non_host_in_relay_channel_cannot_send(self):
        self.message.author.id = 3
        self.message.content = "代發"
        self.invoke()
        self.target.send.assert_not_awaited()
        self.bot.fetch_user.assert_not_awaited()
        self.assertIn("白名單", self.message.reply.call_args.args[0])

    def test_failed_group_attachment_is_reported_without_partial_send(self):
        self.message.content = "有附件"
        self.message.attachments = [SimpleNamespace(filename="cat.png", read=AsyncMock(side_effect=RuntimeError("download failed")))]
        self.invoke()
        self.target.send.assert_not_awaited()
        self.message.add_reaction.assert_not_awaited()
        self.assertIn("cat.png", self.message.reply.call_args.args[0])

    def test_failed_dm_attachment_does_not_silently_send_url(self):
        relay._relay_forward_meta[30] = (333333333333333333, True, 0, 0, 0)
        self.message.content = "有附件"
        self.message.attachments = [SimpleNamespace(filename="cat.png", is_spoiler=lambda: False,
            to_file=AsyncMock(side_effect=RuntimeError("download failed")))]
        self.invoke()
        self.bot.fetch_user.return_value.send.assert_not_awaited()
        self.message.add_reaction.assert_not_awaited()
        self.assertIn("附件", self.message.reply.call_args.args[0])

    def test_wrong_guild_is_rejected(self):
        self.target.guild.id += 1
        self.message.content = "代發"
        self.invoke()
        self.target.send.assert_not_awaited()
        self.assertIn("伺服器", self.message.reply.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
