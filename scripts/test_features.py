"""Controller tests with immediate mocked I/O; no event-loop sockets or login."""
import asyncio
import inspect
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discord
from discord.ext import commands
from bot_modules import admin_commands, bot_core, state_repo
from bot_modules.commands import blackjack, duel, economy, fun
from bot_modules.runtime import policy
from test_regressions import context, interaction, nested_class, ROOT


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        self.ctx = context()
        self.ctx["ALLOWED_HOST_IDS"] = [2]
        self.ctx["get_user_stats_async"] = AsyncMock(return_value=(1000, 0, 0, 0))
        self.ctx["roll_gamble_exp_from_bet"] = lambda bet: 0
        # Discord View only needs a completion Future during these unit tests.
        # Scheduling/concurrency is tested separately by CI's real event loop.
        self.loop = Mock()
        self.loop.get_debug.return_value = False
        self.loop.create_future.side_effect = lambda: asyncio.Future(loop=self.loop)

    def run_now(self, coroutine):
        try:
            with patch("asyncio.get_running_loop", return_value=self.loop):
                try:
                    yielded = coroutine.send(None)
                except StopIteration as done:
                    return done.value
                self.fail(f"Unexpected unmocked scheduling: {yielded!r}")
        finally:
            coroutine.close()

    def make_view(self, cls, *args, **kwargs):
        with patch("asyncio.get_running_loop", return_value=self.loop):
            view = cls(*args, **kwargs)
        self.addCleanup(view.stop)
        return view

    def test_blackjack_failed_payout_keeps_result_for_retry(self):
        self.bot.game_store = SimpleNamespace(finish=AsyncMock(side_effect=[RuntimeError("DB failed"),
            {"results": {"1": [True, 100, False]}, "credits": {"1": 200}}]), call=AsyncMock())
        blackjack.register_blackjack_commands(self.bot, self.ctx)
        setup = nested_class(self.bot, "bj", "SetupView")
        game_cls = inspect.getclosurevars(setup.start.__wrapped__).nonlocals["BlackjackGame"]
        game = self.make_view(game_cls, SimpleNamespace(id=1, display_name="a"), 100, 0, 0,
                              upfront_cost=100, session_id="bj:1")
        game.message = SimpleNamespace(channel=SimpleNamespace(guild=None), edit=AsyncMock())
        self.run_now(game.end("贏", 100, True, False, message_obj=game.message))
        self.assertFalse(game._game_over)
        self.run_now(game.end("不可覆蓋原結果", 999, False, message_obj=game.message))
        self.assertTrue(game._game_over)
        self.assertEqual(self.bot.game_store.finish.await_args_list[0], self.bot.game_store.finish.await_args_list[1])

    def test_duel_failed_payout_timeout_retries_payout_without_refund(self):
        self.bot.game_store = SimpleNamespace(finish=AsyncMock(side_effect=[RuntimeError("DB failed"),
            {"credits": {"1": 150, "2": 50}, "reason": "分配"}]))
        duel.register_duel_commands(self.bot, self.ctx)
        invite = nested_class(self.bot, "duel", "EDuelInviteView")
        match_cls = inspect.getclosurevars(invite.accept.__wrapped__).nonlocals["EDuelMatch"]
        match = match_cls(SimpleNamespace(id=1), SimpleNamespace(id=2), 100, "duel:1")
        match.scores = {1: 3, 2: 1}
        match.pending_outcome = "payout"
        match.view = SimpleNamespace(stop=Mock())
        with self.assertRaises(RuntimeError):
            self.run_now(match.finish_pending())
        self.assertFalse(match.settled)
        self.run_now(match.handle_timeout(match.view))
        self.assertTrue(match.settled)
        self.assertEqual(self.bot.game_store.finish.await_args_list[0], self.bot.game_store.finish.await_args_list[1])
        self.assertNotIn("refund", self.bot.game_store.finish.call_args.kwargs)

    def test_banned_user_cannot_claim_existing_packet(self):
        self.bot.blacklist_checker = AsyncMock(return_value=True)
        economy.register_economy_commands(self.bot, self.ctx)
        packet = self.make_view(nested_class(self.bot, "redpacket", "RedPacketView"), 1, 100, 1)
        self.run_now(packet.children[0].callback(interaction()))
        self.ctx["credit_balance_with_log_async"].assert_not_awaited()
        self.assertEqual(packet.left_amount, 100)

    def test_banned_user_cannot_use_old_blackjack_start_button(self):
        self.bot.blacklist_checker = AsyncMock(return_value=True)
        blackjack.register_blackjack_commands(self.bot, self.ctx)
        view = self.make_view(nested_class(self.bot, "bj", "SetupView"), SimpleNamespace(id=2), 100)
        self.run_now(view.children[0].callback(interaction()))
        self.ctx["try_deduct_balance_async"].assert_not_awaited()

    def test_say_preview_does_not_send_until_owner_confirms(self):
        fun.register_fun_commands(self.bot, self.ctx)
        inter = interaction(command="say")
        inter.guild = SimpleNamespace(id=111111111111111111)
        channel = Mock(spec=discord.TextChannel)
        channel.guild = inter.guild
        channel.id = 222222222222222222
        channel.mention = "<#222222222222222222>"
        channel.send = AsyncMock(return_value=SimpleNamespace(id=30, jump_url="https://discord.com/channels/1/2/30"))
        inter.channel = channel
        self.run_now(self.bot.tree.get_command("say").callback(inter, text="公告"))
        channel.send.assert_not_awaited()
        preview = inter.followup.send.call_args.kwargs["view"]
        self.addCleanup(preview.stop)
        confirmation = interaction(iid=101, command="say")
        confirmation.guild, confirmation.channel = inter.guild, channel
        self.run_now(preview.children[0].callback(confirmation))
        self.run_now(preview.children[0].callback(interaction(iid=102)))
        channel.send.assert_awaited_once_with("公告")

    def test_say_cancel_removes_preview_without_sending(self):
        fun.register_fun_commands(self.bot, self.ctx)
        inter = interaction(command="say")
        inter.guild = SimpleNamespace(id=1)
        channel = Mock(spec=discord.TextChannel)
        channel.guild, channel.id, channel.mention = inter.guild, 2, "<#2>"
        channel.send = AsyncMock()
        inter.channel = channel
        self.run_now(self.bot.tree.get_command("say").callback(inter, text="取消"))
        preview = inter.followup.send.call_args.kwargs["view"]
        self.run_now(preview.children[1].callback(interaction(iid=101)))
        channel.send.assert_not_awaited()
        self.assertTrue(preview.used)

    def audit_case(self, audit):
        self.bot.message_audit = audit
        fun.register_fun_commands(self.bot, self.ctx)
        inter = interaction(command="say")
        inter.guild = SimpleNamespace(id=1)
        channel = Mock(spec=discord.TextChannel)
        channel.guild, channel.id, channel.mention = inter.guild, 2, "<#2>"
        channel.send = AsyncMock(return_value=SimpleNamespace(id=30, jump_url="https://discord.com/channels/1/2/30"))
        inter.channel = channel
        async def direct(function, *args):
            return function(*args)
        with patch.object(fun.asyncio, "to_thread", new=direct):
            self.run_now(self.bot.tree.get_command("say").callback(inter, text="公告", preview=False))
        return inter, channel

    def test_say_audit_unavailable_prevents_unrecorded_send(self):
        with self.assertLogs("bot_modules.commands.fun", level="ERROR"):
            inter, channel = self.audit_case(Mock(side_effect=RuntimeError("audit unavailable")))
        channel.send.assert_not_awaited()
        self.assertIn("尚未發送", inter.followup.send.call_args.args[0])

    def test_say_sent_but_final_audit_failed_is_not_sent_again(self):
        with self.assertLogs("bot_modules.commands.fun", level="ERROR"):
            inter, channel = self.audit_case(Mock(side_effect=[None, RuntimeError("audit unavailable")]))
        channel.send.assert_awaited_once()
        self.assertIn("已發送", inter.followup.send.call_args.args[0])
        self.assertIn("保留此訊息連結", inter.followup.send.call_args.args[0])

    def test_feature_setting_is_saved_before_memory_changes(self):
        self.bot.settings_lock = asyncio.Lock()
        self.ctx["FEATURE_TOGGLES"] = {"bj": True}
        admin_commands.register_admin_commands(self.bot, self.ctx)
        async def direct(function, *args):
            return function(*args)
        with patch.object(state_repo, "save_setting", side_effect=RuntimeError("DB unavailable")), \
                patch.object(admin_commands.asyncio, "to_thread", new=direct), self.assertRaises(RuntimeError):
            self.run_now(self.bot.tree.get_command("admin_feature_toggle").callback(interaction(), "bj", False))
        self.assertTrue(self.ctx["FEATURE_TOGGLES"]["bj"])

    def test_full_registration_installs_durable_services_without_connecting(self):
        source = (ROOT / "bot.py").read_text(encoding="utf-8").split("MEMBERS_INTENT_FALLBACK_FLAG =")[0]
        namespace = {"__name__": "offline_registration_test", "__file__": str(ROOT / "bot.py")}
        with patch("dotenv.load_dotenv"), patch.object(bot_core, "configure_stdio_line_buffering"), \
                patch("bot_modules.db.get_db_connection", side_effect=AssertionError("unexpected DB access")):
            exec(compile(source, str(ROOT / "bot.py"), "exec"), namespace)
        bot = namespace["bot"]
        self.assertIsNotNone(bot.game_store)
        self.assertIsNotNone(bot.blacklist_checker)
        self.assertIsNotNone(bot.message_audit)
        self.assertIn("game_recovery_task", namespace["_event_tasks"])

    def test_policy_blocks_financial_command_but_allows_stats(self):
        self.bot.tree.interaction_check = AsyncMock(return_value=True)
        previous = self.bot.tree.interaction_check
        policy.install_policy(self.bot, [], Mock())
        self.bot.blacklist_checker = AsyncMock(return_value=True)
        async def immediate(awaitable, timeout):
            return await awaitable
        inter = interaction()
        inter.command.qualified_name = "transfer"
        with patch.object(policy.asyncio, "wait_for", new=immediate):
            self.assertFalse(self.run_now(self.bot.tree.interaction_check(inter)))
            previous.assert_not_awaited()
            inter.command.qualified_name = "russian_roulette stats me"
            self.assertTrue(self.run_now(self.bot.tree.interaction_check(inter)))
            previous.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
