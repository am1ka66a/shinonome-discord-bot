"""Per-key async locks that disappear once their last waiter leaves."""
import asyncio
from contextlib import asynccontextmanager


class KeyedLocks:
    def __init__(self):
        self._entries = {}

    @asynccontextmanager
    async def hold(self, key):
        entry = self._entries.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                del self._entries[key]
