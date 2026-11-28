"""AI chat: answers @-mentions in character, within the cooldown and daily cap."""
import asyncio
import logging
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_modules import ai_chat

BOT = SimpleNamespace(id=999)


def response(text="喵～", stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0),
    )


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeChannel:
    def __init__(self, history=(), channel_id=1):
        self.id = channel_id
        self._history = list(history)  # newest first, like discord.py

    def typing(self):
        return Typing()

    async def history(self, limit, before):
        for m in self._history[:limit]:
            yield m


def fake_message(content="小咪你好", author_id=1, mentions=(BOT,), history=(), reference=None):
    return SimpleNamespace(
        author=SimpleNamespace(id=author_id, display_name=f"玩家{author_id}"),
        clean_content=content,
        mentions=list(mentions),
        reference=reference,
        channel=FakeChannel(history),
        reply=AsyncMock(),
        add_reaction=AsyncMock(),
    )


def make_chat(client_response=None, **overrides):
    settings = ai_chat.AIChatSettings(api_key="test", persona="你是小咪。", **overrides)
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=client_response or response())))
    return ai_chat.AIChat(settings, logger=logging.getLogger("test"), client=client), client


class LimiterTests(unittest.TestCase):
    def test_cooldown_daily_cap_and_midnight_reset(self):
        now, day = [0.0], ["2026-11-28"]
        limiter = ai_chat.ChatLimiter(2, 30, clock=lambda: now[0], today=lambda: day[0])
        self.assertIsNone(limiter.try_acquire(1))
        self.assertEqual(limiter.try_acquire(1), "cooldown")
        self.assertIsNone(limiter.try_acquire(2))
        self.assertEqual(limiter.try_acquire(3), "daily")
        now[0] = 31
        self.assertEqual(limiter.try_acquire(1), "daily")
        day[0] = "2026-11-29"
        self.assertIsNone(limiter.try_acquire(1))


class PromptTests(unittest.TestCase):
    def test_persona_comes_before_rules_and_is_cached(self):
        system = ai_chat.build_system("你是小咪。")
        self.assertTrue(system[0]["text"].startswith("你是小咪。"))
        self.assertIn("繁體中文", system[0]["text"])
        self.assertEqual(system[0]["cache_control"], {"type": "ephemeral"})

    def test_user_turn_has_history_then_the_question(self):
        text = ai_chat.build_user_turn([("阿明", "早安"), ("你", "早喵"), ("阿華", " ")], "阿明", "在幹嘛")
        self.assertIn("阿明：早安\n你：早喵", text)
        self.assertNotIn("阿華", text)
        self.assertTrue(text.endswith("阿明 剛剛對你說：在幹嘛"))

    def test_refusal_and_long_reply(self):
        self.assertEqual(ai_chat.reply_text(response(stop_reason="refusal")), ai_chat.REFUSAL_REPLY)
        self.assertEqual(len(ai_chat.reply_text(response("喵" * 3000))), 2000)

    def test_mention_or_reply_to_bot_triggers(self):
        self.assertTrue(ai_chat.is_addressed_to_bot(fake_message(), BOT))
        self.assertFalse(ai_chat.is_addressed_to_bot(fake_message(mentions=()), BOT))
        reply = SimpleNamespace(resolved=SimpleNamespace(author=BOT))
        self.assertTrue(ai_chat.is_addressed_to_bot(fake_message(mentions=(), reference=reply), BOT))

    def test_disabled_without_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertIsNone(ai_chat.AIChatSettings.from_env())
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "AI_CHAT_ENABLED": "false"}):
            self.assertIsNone(ai_chat.AIChatSettings.from_env())

    def test_env_settings(self):
        env = {"ANTHROPIC_API_KEY": "k", "AI_CHAT_DAILY_LIMIT": "50", "AI_CHAT_CHANNEL_IDS": "1, 2,x",
               "AI_CHAT_PERSONA": "角色", "AI_CHAT_HISTORY_MESSAGES": "500"}
        with patch.dict(os.environ, env):
            s = ai_chat.AIChatSettings.from_env()
        self.assertEqual((s.model, s.daily_limit, s.channel_ids, s.persona, s.history_messages),
                         ("claude-haiku-5-5", 50, frozenset({1, 2}), "角色", 100))


class HandleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_uses_history_and_blocks_pings(self):
        chat, client = make_chat()
        older = SimpleNamespace(author=SimpleNamespace(id=2, display_name="阿華"), clean_content="小咪在嗎")
        mine = SimpleNamespace(author=BOT, clean_content="在喵")
        msg = fake_message(history=[mine, older])
        await chat.handle(msg, BOT)
        kwargs = client.messages.create.await_args.kwargs
        self.assertEqual(kwargs["model"], "claude-haiku-5-5")
        self.assertEqual(kwargs["output_config"], {"effort": "medium"})
        self.assertIn("阿華：小咪在嗎\n你：在喵", kwargs["messages"][0]["content"])
        msg.reply.assert_awaited_once()
        self.assertEqual(msg.reply.await_args.args[0], "喵～")
        self.assertFalse(msg.reply.await_args.kwargs["allowed_mentions"].everyone)

    async def test_cooldown_reacts_instead_of_calling_api(self):
        chat, client = make_chat()
        await chat.handle(fake_message(), BOT)
        second = fake_message()
        await chat.handle(second, BOT)
        self.assertEqual(client.messages.create.await_count, 1)
        second.add_reaction.assert_awaited_once_with("⏳")

    async def test_api_failure_reacts_and_does_not_raise(self):
        chat, client = make_chat()
        client.messages.create.side_effect = RuntimeError("down")
        msg = fake_message()
        await chat.handle(msg, BOT)
        msg.reply.assert_not_awaited()
        msg.add_reaction.assert_awaited_once_with("⚠️")

    async def test_channel_allowlist(self):
        chat, _ = make_chat(channel_ids=frozenset({5}))
        self.assertFalse(chat.wants(fake_message(), BOT))

    async def test_schedule_runs_in_background(self):
        chat, client = make_chat()
        chat.schedule(fake_message(), BOT)
        await asyncio.gather(*chat.tasks)
        client.messages.create.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
