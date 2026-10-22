"""Offline /say tests: all Discord I/O completes through mocks, without a login."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord
from discord.ext import commands
from bot_modules.commands.fun import parse_say_reply_target, register_fun_commands
from test_regressions import context, interaction

GUILD_ID = 111111111111111111
CHANNEL_ID = 222222222222222222
MESSAGE_ID = 333333333333333333
LINK = f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/{MESSAGE_ID}"


class SayTests(unittest.TestCase):
    def setUp(self):
        # No Bot login or HTTP session is created by command registration.
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        ctx = context()
        ctx["ALLOWED_HOST_IDS"] = [2]
        register_fun_commands(self.bot, ctx)
        self.command = self.bot.tree.get_command("say")
        self.guild = SimpleNamespace(id=GUILD_ID, filesize_limit=10_000_000)
        self.channel = self.make_channel()
        self.inter = interaction(command="say")
        self.inter.guild = self.guild
        self.inter.channel = self.channel

    def make_channel(self, channel_id=CHANNEL_ID, channel_type=discord.TextChannel):
        channel = Mock(spec=channel_type)
        channel.id = channel_id
        channel.guild = self.guild
        channel.mention = f"<#{channel_id}>"
        channel.send = AsyncMock(return_value=SimpleNamespace(jump_url=LINK))
        original = Mock(spec=discord.Message)
        original.to_reference.return_value = discord.MessageReference(
            message_id=MESSAGE_ID, channel_id=channel_id, guild_id=GUILD_ID,
            fail_if_not_exists=True,
        )
        channel.fetch_message = AsyncMock(return_value=original)
        return channel

    def image(self, **overrides):
        image = Mock(spec=discord.Attachment)
        image.filename = "SPOILER_cat.png"
        image.content_type = "image/png"
        image.size = 20
        image.is_spoiler.return_value = True
        image.to_file = AsyncMock(return_value=Mock(spec=discord.File))
        for key, value in overrides.items():
            setattr(image, key, value)
        return image

    def invoke(self, **kwargs):
        # Every awaited I/O is mocked and returns immediately. Advancing this
        # coroutine directly avoids opening even the event loop's local sockets.
        kwargs.setdefault("preview", False)
        coroutine = self.command.callback(self.inter, **kwargs)
        try:
            with self.assertRaises(StopIteration):
                coroutine.send(None)
        finally:
            coroutine.close()

    def feedback(self):
        mock = self.inter.followup.send
        if not mock.call_count:
            mock = self.inter.response.send_message
        self.assertTrue(mock.call_args.kwargs["ephemeral"])
        return mock.call_args.args[0]

    def test_command_exposes_optional_image_and_reply(self):
        options = {p.name: p for p in self.command.parameters}
        self.assertEqual(set(options), {"text", "channel", "image", "reply_to", "preview"})
        self.assertTrue(all(not p.required for p in options.values()))
        self.assertEqual(options["image"].type, discord.AppCommandOptionType.attachment)
        self.assertIn(discord.ChannelType.public_thread, options["channel"].channel_types)
        self.assertTrue(self.command.guild_only)
        self.assertTrue(self.command.default_permissions.manage_messages)

    def test_original_text_flow_and_private_confirmation(self):
        self.invoke(text="公告")
        self.channel.send.assert_awaited_once_with("公告")
        self.inter.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.assertIn(LINK, self.feedback())

    def test_image_only_preserves_spoiler_and_closes_upload(self):
        image = self.image()
        self.invoke(image=image)
        image.to_file.assert_awaited_once_with(spoiler=True)
        self.channel.send.assert_awaited_once_with(None, file=image.to_file.return_value)
        image.to_file.return_value.close.assert_called_once_with()

    def test_text_image_and_message_id_reply_in_selected_thread(self):
        thread = self.make_channel(channel_type=discord.Thread)
        image = self.image()
        self.invoke(text="收到", channel=thread, image=image, reply_to=str(MESSAGE_ID))
        thread.fetch_message.assert_awaited_once_with(MESSAGE_ID)
        original = thread.fetch_message.return_value
        original.to_reference.assert_called_once_with(fail_if_not_exists=True)
        thread.send.assert_awaited_once_with(
            "收到", file=image.to_file.return_value,
            reference=original.to_reference.return_value, mention_author=False,
        )
        self.assertIn("已回覆", self.feedback())

    def test_link_selects_channel_and_fetches_when_not_cached(self):
        other = self.make_channel()
        self.inter.channel = self.make_channel(CHANNEL_ID + 1)
        with patch.object(self.bot, "get_channel", return_value=None), patch.object(
            self.bot, "fetch_channel", new=AsyncMock(return_value=other)
        ) as fetch:
            self.invoke(text="回覆", reply_to=LINK)
        fetch.assert_awaited_once_with(CHANNEL_ID)
        other.fetch_message.assert_awaited_once_with(MESSAGE_ID)
        other.send.assert_awaited_once()
        self.inter.channel.send.assert_not_awaited()

    def test_cached_link_target_does_not_fetch_channel(self):
        with patch.object(self.bot, "get_channel", return_value=self.channel), patch.object(
            self.bot, "fetch_channel", new=AsyncMock()
        ) as fetch:
            self.invoke(text="回覆", reply_to=LINK)
        fetch.assert_not_awaited()
        self.channel.fetch_message.assert_awaited_once_with(MESSAGE_ID)

    def test_unauthorized_user_cannot_upload_or_send(self):
        self.inter.user.id = 999
        image = self.image()
        self.invoke(image=image)
        image.to_file.assert_not_awaited()
        self.channel.send.assert_not_awaited()
        self.inter.response.defer.assert_not_awaited()
        self.assertIn("沒有權限", self.feedback())

    def test_empty_or_too_long_content_is_rejected(self):
        for text in (None, "  \n", "x" * 2001):
            with self.subTest(text_length=len(text or "")):
                self.invoke(text=text)
        self.channel.send.assert_not_awaited()

    def test_nonimage_and_oversize_uploads_are_rejected_before_download(self):
        for overrides in ({"content_type": "application/pdf"}, {"size": self.guild.filesize_limit + 1}):
            with self.subTest(overrides=overrides):
                image = self.image(**overrides)
                self.invoke(image=image)
                image.to_file.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    def test_missing_mime_can_use_image_filename(self):
        image = self.image(content_type=None)
        self.invoke(image=image)
        self.channel.send.assert_awaited_once()

    def test_malformed_cross_guild_and_conflicting_links_never_send(self):
        invalid = ("https://example.com/image.png", LINK.replace(str(GUILD_ID), str(GUILD_ID + 1)))
        for link in invalid:
            with self.subTest(link=link):
                self.invoke(text="回覆", reply_to=link)
        other = self.make_channel(CHANNEL_ID + 1)
        self.invoke(text="回覆", channel=other, reply_to=LINK)
        self.channel.send.assert_not_awaited()
        self.channel.fetch_message.assert_not_awaited()
        other.send.assert_not_awaited()
        self.assertIn("指定頻道不同", self.feedback())

    def test_deleted_reference_does_not_fall_back_to_standalone_send(self):
        self.channel.fetch_message.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Unknown Message"
        )
        self.invoke(text="回覆", reply_to=str(MESSAGE_ID))
        self.channel.send.assert_not_awaited()
        self.assertIn("找不到", self.feedback())

    def test_forbidden_send_closes_file_and_explains_permissions(self):
        image = self.image()
        self.channel.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"
        )
        self.invoke(image=image)
        image.to_file.return_value.close.assert_called_once_with()
        self.assertIn("權限", self.feedback())

    def test_http_error_does_not_retry_or_report_success(self):
        self.channel.send.side_effect = discord.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable"), "unavailable"
        )
        self.invoke(text="公告")
        self.channel.send.assert_awaited_once()
        self.assertIn("確認目標頻道是否已收到", self.feedback())


class ReplyTargetParsingTests(unittest.TestCase):
    def test_accepts_id_and_supported_discord_message_links(self):
        self.assertEqual(parse_say_reply_target(str(MESSAGE_ID)), (None, None, MESSAGE_ID))
        for link in (LINK, f"<{LINK}>", LINK + "?jump=true", LINK.replace("discord.com", "canary.discord.com")):
            with self.subTest(link=link):
                self.assertEqual(parse_say_reply_target(link), (GUILD_ID, CHANNEL_ID, MESSAGE_ID))

    def test_rejects_foreign_domains_dm_links_and_partial_ids(self):
        for value in ("", "123", LINK + "/unexpected", LINK.replace("discord.com", "discord.com.evil.test"), LINK.replace(str(GUILD_ID), "@me")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_say_reply_target(value)


if __name__ == "__main__":
    unittest.main()
