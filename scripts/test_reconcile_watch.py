"""Daily reconcile watch: alert admins once per new finding, stay quiet when clean."""
import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_modules.admin_audit_repo import ReconcileReport
from bot_modules.runtime import reconcile_watch


def report(drifted=(), orphans=0, orphan_amount=0, accounts=10):
    drifted = list(drifted)
    return ReconcileReport(accounts=accounts, matched=accounts - len(drifted), drifted=drifted,
                           total_drift=sum(r[4] for r in drifted), orphan_users=orphans,
                           orphan_amount=orphan_amount, baseline=None, checked_at=0)


class FakeBot:
    def __init__(self, rounds):
        self.rounds = rounds

    def is_closed(self):
        self.rounds -= 1
        return self.rounds < 0


class ReconcileWatchTests(unittest.IsolatedAsyncioTestCase):
    async def run_watch(self, reports):
        queue = list(reports)
        notify = AsyncMock()
        with patch.object(reconcile_watch.asyncio, "sleep", new=AsyncMock()):
            await reconcile_watch.run_reconcile_watch(
                FakeBot(len(queue)), logger=logging.getLogger("test"), connect=None, admin_ids=[1, 2],
                interval_seconds=1, first_delay_seconds=0,
                report_func=lambda connect=None: queue.pop(0), notify=notify)
        return notify

    async def test_clean_reports_send_nothing(self):
        notify = await self.run_watch([report(), report()])
        notify.assert_not_awaited()

    async def test_same_finding_is_reported_once_and_change_reported_again(self):
        drift = ("5", 100, 150, 0, -50)
        notify = await self.run_watch([
            report([drift]), report([drift]),            # same finding: one alert
            report([drift, ("6", 0, 1, 0, -1)]),         # new account drifts: alert again
            report(), report([drift]),                   # recovered, then back: alert again
        ])
        self.assertEqual(notify.await_count, 3)
        text = notify.await_args_list[0].args[2]
        self.assertIn("1 個帳戶不符", text)
        self.assertIn("-50", text)

    async def test_orphan_accounts_alone_trigger_an_alert(self):
        notify = await self.run_watch([report(orphans=2, orphan_amount=300)])
        self.assertIn("2 個不存在的帳戶", notify.await_args.args[2])

    async def test_a_failing_check_keeps_the_watch_alive(self):
        calls = []

        def flaky(connect=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("db down")
            return report([("5", 1, 2, 0, -1)])

        notify = AsyncMock()
        with patch.object(reconcile_watch.asyncio, "sleep", new=AsyncMock()):
            await reconcile_watch.run_reconcile_watch(
                FakeBot(2), logger=logging.getLogger("test"), connect=None, admin_ids=[1],
                interval_seconds=1, first_delay_seconds=0, report_func=flaky, notify=notify)
        notify.assert_awaited_once()

    async def test_dm_admins_skips_unreachable_admin(self):
        good = SimpleNamespace(send=AsyncMock())
        bot = SimpleNamespace(get_user=lambda uid: good if uid == 1 else None,
                              fetch_user=AsyncMock(side_effect=RuntimeError("unknown user")))
        sent = await reconcile_watch.dm_admins(bot, [1, 2], "hi", logging.getLogger("test"))
        self.assertEqual(sent, 1)
        good.send.assert_awaited_once_with("hi")

    def test_long_alert_is_truncated_for_discord(self):
        many = [(str(i), 0, 1, 0, -1) for i in range(500)]
        text = reconcile_watch.alert_text(report(many, accounts=600))
        self.assertLessEqual(len(text), 1900)
        self.assertIn("另有 5 個帳戶", reconcile_watch.alert_text(report(many[:15], accounts=600)))


if __name__ == "__main__":
    unittest.main()
