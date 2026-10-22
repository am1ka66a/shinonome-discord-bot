"""Serialize view actions, including callbacks already queued before a view stopped."""
from functools import wraps


def serialized_action(callback):
    @wraps(callback)
    async def guarded(self, *args, **kwargs):
        async with self._callback_lock:
            return await callback(self, *args, **kwargs)
    return guarded
