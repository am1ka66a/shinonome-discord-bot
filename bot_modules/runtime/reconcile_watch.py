"""Daily balance-vs-ledger check that messages the admins only when something is off.

The reconcile baseline absorbs history, so any drift reported here is new: money
moved without a matching ledger row. The same finding is reported once; a new
or changed finding is reported again, and a return to normal is only logged.
"""
import asyncio
import functools
import typing


def has_problem(report) -> bool:
    return bool(report.drifted) or bool(report.orphan_users)


def finding_signature(report) -> tuple:
    return (tuple((uid, drift) for uid, _, _, _, drift in report.drifted),
            int(report.orphan_users), int(report.orphan_amount))


def alert_text(report, limit: int = 10) -> str:
    lines = [
        "⚠️ **每日對帳發現異常**",
        f"{report.accounts - report.matched:,} 個帳戶不符｜差額合計 {report.total_drift:+,}",
    ]
    if report.orphan_users:
        lines.append(f"總帳中有 {report.orphan_users} 個不存在的帳戶，合計 {report.orphan_amount:+,}")
    for uid, balance, ledger_sum, _, drift in report.drifted[:limit]:
        lines.append(f"<@{uid}>（`{uid}`）差額 **{drift:+,}**｜餘額 {balance:,}｜總帳 {ledger_sum:,}")
    if len(report.drifted) > limit:
        lines.append(f"…另有 {len(report.drifted) - limit:,} 個帳戶")
    lines.append("詳細請用 `/admin_audit reconcile` 查看。")
    return "\n".join(lines)[:1900]


async def dm_admins(bot, admin_ids: typing.Iterable[int], text: str, logger) -> int:
    sent = 0
    for admin_id in admin_ids:
        try:
            user = bot.get_user(int(admin_id)) or await bot.fetch_user(int(admin_id))
            await user.send(text)
            sent += 1
        except Exception:
            logger.exception("每日對帳通知無法私訊管理員 %s", admin_id)
    return sent


async def run_reconcile_watch(
    bot,
    *,
    logger,
    connect,
    admin_ids: typing.Iterable[int],
    interval_seconds: float,
    first_delay_seconds: float,
    report_func=None,
    notify=dm_admins,
) -> None:
    if report_func is None:
        from bot_modules import admin_audit_repo
        report_func = admin_audit_repo.reconcile_report
    reported = None
    await asyncio.sleep(first_delay_seconds)
    while not bot.is_closed():
        try:
            report = await asyncio.to_thread(functools.partial(report_func, connect=connect))
            if has_problem(report):
                signature = finding_signature(report)
                logger.warning("每日對帳：%s 個帳戶不符，差額合計 %+d，不存在帳戶 %s 個",
                               report.accounts - report.matched, report.total_drift, report.orphan_users)
                if signature != reported:
                    await notify(bot, admin_ids, alert_text(report), logger)
                    reported = signature
            else:
                if reported is not None:
                    logger.info("每日對帳：已恢復全部相符")
                reported = None
        except Exception:
            logger.exception("每日對帳失敗，下次排程再試")
        await asyncio.sleep(interval_seconds)
