"""Serialize view actions, including callbacks already queued before a view stopped."""
import contextlib
from bot_modules.maintenance import active_game_action
from functools import wraps


def serialized_action(callback):
    @wraps(callback)
    async def guarded(self, *args, **kwargs):
        bot = getattr(args[0], "client", None) if args else None
        scope = active_game_action(bot) if bot is not None else contextlib.nullcontext()
        with scope:
            async with self._callback_lock:
                return await callback(self, *args, **kwargs)
    return guarded
