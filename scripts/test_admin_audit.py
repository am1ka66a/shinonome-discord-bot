"""Read-only accounting queries, bounded pagination and private host-only UI."""
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discord
from discord.ext import commands
from bot_modules import admin_audit_repo as repo
from bot_modules.commands import admin_audit as ui
from bot_modules.runtime import app_lock, policy
from test_regressions import SqliteConnection, context, interaction


class AuditRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = SqliteConnection()
        self.addCleanup(self.conn.db.close)
        self.conn.fetchall = lambda: self.conn.cur.fetchall()
        self.connect = lambda: self.conn
        self.conn.db.executescript("""
            ALTER TABLE users ADD COLUMN exp INTEGER DEFAULT 0;
            INSERT INTO users (user_id,balance) VALUES ('1',1234),('2',9000);
            CREATE TABLE blacklist (user_id TEXT PRIMARY KEY);
            INSERT INTO blacklist VALUES ('1');
            DROP TABLE casino_logs;
            CREATE TABLE casino_logs (id INTEGER PRIMARY KEY,user_id TEXT,amount INTEGER,reason TEXT,created_at TEXT,source_log_id INTEGER);
            CREATE TABLE bot_admin_operations (request_id TEXT PRIMARY KEY,actor_id TEXT,action TEXT,result_json TEXT,created_at TEXT);
            CREATE TABLE bot_admin_snapshots (request_id TEXT,user_id TEXT,snapshot_json TEXT,PRIMARY KEY(request_id,user_id));
            CREATE TABLE bot_game_sessions (session_id TEXT PRIMARY KEY,kind TEXT,phase TEXT,expires_at INTEGER);
        """)
        self.conn.db.executemany("INSERT INTO casino_logs VALUES (?,'1',10,'test','2026-11-17',?)",
                                [(n, n+1000) for n in range(1, 26)])
        self.conn.db.commit()

    def test_cursor_pages_exclude_new_entries_and_other_players(self):
        snapshot = repo.account_snapshot(1, self.connect)
        self.assertEqual(snapshot['account'][0], 1234)
        self.assertTrue(snapshot['blacklisted'])
        page = snapshot['page']
        self.assertEqual([r[0] for r in page.rows], list(range(25, 15, -1)))
        self.conn.db.execute("INSERT INTO casino_logs VALUES (26,'1',999,'new','today',99)")
        self.conn.db.execute("INSERT INTO casino_logs VALUES (27,'2',999,'other','today',98)")
        self.conn.db.commit()
        middle = repo.ledger_page(1, page.upper_id, page.rows[-1][0], self.connect)
        last = repo.ledger_page(1, page.upper_id, middle.rows[-1][0], self.connect)
        self.assertEqual([r[0] for r in middle.rows+last.rows], list(range(15, 0, -1)))
        self.assertFalse(last.has_more)
        self.assertEqual(repo.ledger_page(1, page.upper_id, connect=self.connect), page)
        self.assertEqual(repo.account_snapshot(1, self.connect)['page'].upper_id, 26)

    def test_all_audit_queries_only_select_and_missing_user_stays_missing(self):
        sql = []
        self.conn.db.set_trace_callback(sql.append)
        self.assertIsNone(repo.account_snapshot(999, self.connect))
        repo.ledger_page(1, 25, connect=self.connect)
        repo.reset_operations(connect=self.connect)
        self.assertIsNone(repo.reset_details('missing', connect=self.connect))
        repo.pending_settlements(self.connect)
        self.assertTrue(all(q.lstrip().upper().startswith('SELECT') for q in sql), sql)
        self.assertEqual(self.conn.db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)

    def test_reset_snapshot_filters_preserve_before_and_current_balance(self):
        for op, player in [('first', '1'), ('second', '2')]:
            self.conn.db.execute("INSERT INTO bot_admin_operations VALUES (?,'admin','reset_zero',?,'2026-11-17')",
                (op, json.dumps(dict(accounts=1, delta=-500, target_balance=0))))
            self.conn.db.execute("INSERT INTO bot_admin_snapshots VALUES (?,?,?)",
                (op, player, json.dumps(dict(balance=500, wins=2))))
        self.conn.db.commit()
        self.assertEqual([r[0] for r in repo.reset_operations(1, self.connect)], ['first'])
        operation, rows = repo.reset_details('first', 1, self.connect)
        self.assertEqual((json.loads(rows[0][1])['balance'], rows[0][2]), (500, 1234))
        self.assertEqual(repo.reset_details('first', 2, self.connect)[1], [])

    def test_pending_excludes_healthy_active_and_already_settled_games(self):
        now = int(time.time())
        self.conn.db.executemany("INSERT INTO bot_game_sessions VALUES (?,'bj',?,?)",
            [('pending','settling',now+100), ('expired','active',now-100),
             ('healthy','active',now+100), ('done','settled',now-100)])
        self.conn.db.execute("INSERT INTO russian_roulette_active_matches VALUES ('rr','playing','{}')")
        self.conn.db.commit()
        data = repo.pending_settlements(self.connect)
        self.assertEqual(data['total'], 2)
        self.assertEqual({r[0] for r in data['rows']}, {'pending', 'expired'})
        self.assertEqual(data['roulette_count'], 1)


class AuditUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        self.ctx = context()
        self.ctx['ALLOWED_HOST_IDS'] = {2, 3}
        self.ctx['resolve_slash_target'] = AsyncMock(return_value=(SimpleNamespace(id=1), None))
        ui.register_admin_audit_commands(self.bot, self.ctx)
        self.snapshot = dict(account=(1234,5,1,4,2,10), blacklisted=False, queried_at=100,
            page=repo.LedgerPage([(i,10,'test','today',i+1000) for i in range(25,15,-1)],25,True))

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_every_command_rejects_nonhost_before_database_or_user_lookup(self):
        with patch.object(ui.asyncio, 'to_thread', new=AsyncMock()) as worker:
            for cmd in self.bot.tree.get_command('admin_audit').commands:
                inter = interaction(uid=99)
                await cmd.callback(inter)
                self.assertTrue(inter.response.send_message.call_args.kwargs['ephemeral'])
            worker.assert_not_awaited()
        self.ctx['resolve_slash_target'].assert_not_awaited()

    async def test_ai_panel_reports_disabled_or_usage_without_database(self):
        group = self.bot.tree.get_command('admin_audit')
        with patch.object(ui.asyncio, 'to_thread', new=AsyncMock()) as worker:
            inter = interaction()
            await group.get_command('ai').callback(inter)
            self.assertIn('未啟用', inter.followup.send.call_args.kwargs['embed'].description)
            self.bot.ai_chat = SimpleNamespace(status_lines=lambda: ['今日次數 3 / 300'])
            inter = interaction()
            await group.get_command('ai').callback(inter)
            worker.assert_not_awaited()
        sent = inter.followup.send.call_args.kwargs
        self.assertTrue(sent['ephemeral'])
        self.assertIn('今日次數 3 / 300', sent['embed'].description)

    async def test_health_is_private_and_available_without_database_queries(self):
        import asyncio
        self.bot._db_ready_event=asyncio.Event()
        self.bot.background_task_status={'recovery_task':dict(state='waiting',restarts=2)}
        inter=interaction()
        with patch.object(ui.asyncio,'to_thread',new=AsyncMock()) as worker:
            await self.bot.tree.get_command('admin_audit').get_command('health').callback(inter)
            worker.assert_not_awaited()
        sent=inter.followup.send.call_args.kwargs
        self.assertTrue(sent['ephemeral'])
        self.assertIn('尚未完成',sent['embed'].description)
        self.assertIn('等待恢復',sent['embed'].fields[0].value)

    async def test_account_is_private_and_component_checks_original_admin(self):
        with patch.object(ui.asyncio, 'to_thread', new=AsyncMock(return_value=self.snapshot)):
            inter = interaction()
            await self.bot.tree.get_command('admin_audit').get_command('account').callback(inter, user_id='1')
        sent = inter.followup.send.call_args.kwargs
        self.assertTrue(inter.response.defer.call_args.kwargs['ephemeral'])
        self.assertTrue(sent['ephemeral'])
        self.assertIn('總帳 **#25**｜短期流水 #1025', sent['embed'].description)
        view = sent['view']
        self.addCleanup(view.stop)
        self.assertFalse(await view.interaction_check(interaction(uid=3)))
        self.assertTrue(await view.interaction_check(interaction(uid=2)))
        self.ctx['ALLOWED_HOST_IDS'].remove(2)
        self.assertFalse(await view.interaction_check(interaction(uid=2)))

    async def test_failed_page_query_preserves_cursor_and_can_retry(self):
        view = ui.AccountLedgerView(2, {2}, 1, self.snapshot, Mock(), Mock())
        self.addCleanup(view.stop)
        next_page = repo.LedgerPage([(i,10,'test','today',None) for i in range(15,5,-1)],25,True)
        with patch.object(ui.asyncio, 'to_thread', new=AsyncMock(side_effect=[RuntimeError('offline'), next_page])):
            await view.move(interaction(), 1)
            self.assertEqual((view.index, view.cursors), (0, [None]))
            await view.move(interaction(), 1)
            self.assertEqual((view.index, view.cursors), (1, [None, 16]))
        await view.on_timeout()
        self.assertTrue(all(child.disabled for child in view.children))

    async def test_audit_does_not_acquire_mutation_lock(self):
        app_lock.register_app_command_lock(self.bot, Mock())
        self.assertTrue(await self.bot.tree.interaction_check(interaction()))
        for cmd in self.bot.tree.get_command('admin_audit').commands:
            self.assertIn(cmd.qualified_name, policy.READ_COMMANDS)
            inter = interaction(iid=101)
            inter.command = cmd
            self.assertTrue(await self.bot.tree.interaction_check(inter))
        self.assertEqual(len(self.bot._active_mutating_commands), 1)

    async def test_query_error_returns_private_notice_without_raw_exception(self):
        inter = interaction()
        await inter.response.defer(ephemeral=True)
        group = self.bot.tree.get_command('admin_audit')
        await group.on_error(inter, RuntimeError('private internal detail'))
        self.assertTrue(inter.followup.send.call_args.kwargs['ephemeral'])
        self.assertNotIn('private internal detail', inter.followup.send.call_args.args[0])
