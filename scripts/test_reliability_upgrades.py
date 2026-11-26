"""Failure-path checks for supervision, error replies and early acknowledgements."""
import asyncio
import io
import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import discord
from discord import app_commands
from discord.ext import commands
from bot_modules import bot_core
from bot_modules.commands import economy, fun, wanted, russian_roulette
from bot_modules.discord_helpers import resolve_slash_target
from bot_modules.runtime.app_lock import register_app_command_lock
from bot_modules.runtime.tasks import supervise_task
from test_regressions import context, interaction


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot=commands.Bot(command_prefix='!',intents=discord.Intents.none())
        self.ctx=context()
        self.ctx['ALLOWED_HOST_IDS']=[2]
        self.ctx.update(COP_HUNT_FEE=1000,WANTED_BUYOUT_COST=1000,BAIL_COST=1000)

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_failed_background_worker_restarts_without_gateway_reconnect(self):
        closed=False
        bot=SimpleNamespace(is_closed=lambda:closed)
        calls=0
        async def worker():
            nonlocal calls,closed
            calls+=1
            if calls==1:
                raise RuntimeError('temporary worker failure')
            closed=True
        states={}
        with patch('bot_modules.runtime.tasks.asyncio.sleep',new=AsyncMock()) as sleep:
            await supervise_task(bot,'recovery_task',worker,Mock(),states)
        self.assertEqual(calls,2)
        sleep.assert_awaited_once_with(5)
        self.assertEqual(states['recovery_task']['restarts'],1)
        self.assertEqual(states['recovery_task']['state'],'stopped')

    async def test_background_cancel_does_not_restart(self):
        entered=asyncio.Event()
        async def worker():
            entered.set()
            await asyncio.Future()
        states={}
        task=asyncio.create_task(supervise_task(SimpleNamespace(is_closed=lambda:False),'worker',worker,Mock(),states))
        await asyncio.wait_for(entered.wait(),timeout=3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(states['worker']['restarts'],0)
        self.assertEqual(states['worker']['state'],'stopped')

    async def test_command_failure_releases_lock_and_shows_reference_without_exception(self):
        previous=AsyncMock()
        self.bot.tree.on_error=previous
        register_app_command_lock(self.bot,Mock())
        inter=interaction(iid=123)
        await self.bot.tree.interaction_check(inter)
        await inter.response.defer()
        error=app_commands.AppCommandError('private database detail')
        await self.bot.tree.on_error(inter,error)
        self.assertEqual(self.bot._active_mutating_commands,{})
        sent=inter.followup.send.call_args
        self.assertIn('123',sent.args[0])
        self.assertNotIn('private database detail',sent.args[0])
        self.assertTrue(sent.kwargs['ephemeral'])
        previous.assert_awaited_once_with(inter,error)

    async def test_target_lookup_is_acknowledged_before_slow_external_lookup(self):
        economy.register_economy_commands(self.bot,self.ctx)
        fun.register_fun_commands(self.bot,self.ctx)
        wanted.register_wanted_commands(self.bot,self.ctx)
        russian_roulette.register_russian_roulette_commands(self.bot,self.ctx)
        async def slow_lookup(inter,*args,**kwargs):
            self.assertTrue(inter.response.is_done())
            return None,'lookup unavailable'
        self.ctx['resolve_slash_target'].side_effect=slow_lookup
        commands_to_check=[self.bot.tree.get_command(name) for name in
            ('balance','level','record','kill','rob','cop_hunt','break_citizen')]
        commands_to_check.append(self.bot.tree.get_command('russian_roulette').get_command('stats').get_command('me'))
        for cmd in commands_to_check:
            with self.subTest(command=cmd.qualified_name):
                inter=interaction()
                await cmd.callback(inter,user_id='531308526262550528')
                self.assertEqual(inter.followup.send.call_args.args[0],'lookup unavailable')

    async def test_invalid_optional_user_id_is_not_silently_replaced_with_self(self):
        target,error=await resolve_slash_target(interaction(),None,'bad-id',required=False)
        self.assertIsNone(target)
        self.assertIsNotNone(error)
        self.assertEqual(await resolve_slash_target(interaction(),None,None,required=False),(None,None))

    async def test_ai_question_does_not_block_financial_commands(self):
        register_app_command_lock(self.bot,Mock())
        ask=interaction(iid=1,command='ask')
        self.assertTrue(await self.bot.tree.interaction_check(ask))
        self.assertEqual(self.bot._active_mutating_commands,{})
        self.assertTrue(await self.bot.tree.interaction_check(interaction(iid=2,command='transfer')))

    async def test_large_red_packet_summary_stays_below_discord_message_limit(self):
        import inspect
        economy.register_economy_commands(self.bot,self.ctx)
        view_class=inspect.getclosurevars(self.bot.tree.get_command('redpacket').callback).nonlocals['RedPacketView']
        view=view_class(1,1000000,1000)
        self.addCleanup(view.stop)
        view.claim_results=[(10**18+n,10**18) for n in range(1000)]
        content=view.summary_text()+'\n'+view.winners_text()
        self.assertLess(len(content),2000)
        self.assertIn('980',content)


class ConsoleLogTests(unittest.TestCase):
    def test_discord_default_null_handler_does_not_hide_connection_errors(self):
        output,errors=io.StringIO(),io.StringIO()
        application=logging.getLogger('shinonome_bot')
        discord_log=logging.getLogger('discord')
        old=(application.level,application.propagate,discord_log.level,discord_log.propagate)
        try:
            with patch.object(application,'handlers',[]),patch.object(discord_log,'handlers',[logging.NullHandler()]), \
                    patch.object(bot_core.sys,'stdout',output),patch.object(bot_core.sys,'stderr',errors), \
                    patch.dict('os.environ',{'LOG_LEVEL':'INFO','BOT_LOG_FILE':'','LOG_FILE':''}):
                bot_core.setup_bot_logging()
                discord_log.info('gateway connected')
                discord_log.error('gateway failure')
                self.assertIn('gateway connected',output.getvalue())
                self.assertIn('gateway failure',errors.getvalue())
                for handler in [*application.handlers,*discord_log.handlers]:
                    handler.close()
        finally:
            application.setLevel(old[0])
            application.propagate=old[1]
            discord_log.setLevel(old[2])
            discord_log.propagate=old[3]

    def test_info_goes_to_stdout_and_actual_errors_to_stderr(self):
        output,errors=io.StringIO(),io.StringIO()
        logger=logging.getLogger('console-routing-regression')
        logger.setLevel(logging.INFO)
        logger.propagate=False
        with patch.object(bot_core.sys,'stdout',output),patch.object(bot_core.sys,'stderr',errors):
            handlers=bot_core._console_handlers(logging.Formatter('%(levelname)s:%(message)s'),logging.INFO)
            for handler in handlers:
                logger.addHandler(handler)
            try:
                logger.info('normal startup')
                logger.error('actual failure')
            finally:
                for handler in handlers:
                    logger.removeHandler(handler)
                    handler.close()
        self.assertIn('normal startup',output.getvalue())
        self.assertNotIn('normal startup',errors.getvalue())
        self.assertIn('actual failure',errors.getvalue())
        self.assertNotIn('actual failure',output.getvalue())
