"""Restart failed background workers and expose bounded status for admins."""
import asyncio
import time


async def supervise_task(bot, name, factory, logger, statuses):
    status = statuses.setdefault(name, {"state": "starting", "restarts": 0, "last_failure": None})
    delay = 5
    try:
        while not bot.is_closed():
            status["state"] = "running"
            try:
                await factory()
                if bot.is_closed():
                    break
                logger.warning("背景任務 %s 提前結束，將自動恢復", name)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("背景任務 %s 失敗，將自動恢復", name)
            status.update(state="waiting", restarts=status["restarts"] + 1, last_failure=time.time())
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    finally:
        status["state"] = "stopped"
